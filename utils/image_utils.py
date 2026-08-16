"""Image download, conversion, and cache helpers."""

import hashlib
import io
import logging
import math
import mimetypes
import os
import re
import struct
import subprocess
import tempfile
import threading

import requests
from PIL import Image, UnidentifiedImageError

LOGGER = logging.getLogger(__name__)
_RESAMPLING = getattr(Image, "Resampling", Image)
_DITHER = getattr(Image, "Dither", Image)


def default_cache_dir():
	configured = os.environ.get("GB_PROXY_CACHE_DIR")
	if configured:
		return os.path.abspath(os.path.expanduser(configured))
	cache_home = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
	return os.path.join(cache_home, "gb-proxy", "images")


CACHE_DIR = default_cache_dir()
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
_cache_locks = tuple(threading.Lock() for _ in range(64))
_CACHE_FORMAT_VERSION = "4"
_RSVG_CONVERT = "/usr/bin/rsvg-convert"
_SVG_CONVERSION_TIMEOUT = 10
_SVG_SNIFF_BYTES = 64 * 1024
_SVG_MAX_DIMENSION = 32767

GBPC_PALETTE = (
	(0x00, 0x00, 0x80),
	(0xFF, 0xFF, 0xFF),
	(0x00, 0x00, 0x00),
	(0xFF, 0x00, 0x00),
)
GBPC_INKS = bytes((1, 26, 0, 6))
GBPC_MODE_1 = 1
GBPC_MODE_7 = 7
_GBPC_MODE7_MAX_WIDTH = 512
_GBPC_MODE7_MAX_HEIGHT = 255

# Fixed SymbOS desktop palette used by the reference gfx2sgx converter. SGX
# files carry palette indexes rather than palette data, so using these exact
# colours for quantisation is important for consistent output on all targets.
SYMBOS_PALETTE = (
	(0xF7, 0xF7, 0x90),
	(0x06, 0x06, 0x06),
	(0xF7, 0x90, 0x06),
	(0x90, 0x06, 0x06),
	(0x06, 0xF7, 0xF7),
	(0x06, 0x06, 0x90),
	(0x90, 0x90, 0xF7),
	(0x06, 0x06, 0xF7),
	(0xF7, 0xF7, 0xF7),
	(0x06, 0x90, 0x06),
	(0x06, 0xF7, 0x06),
	(0xF7, 0x06, 0xF7),
	(0xF7, 0xF7, 0x06),
	(0x90, 0x90, 0x90),
	(0xF9, 0x90, 0x90),
	(0xF7, 0x06, 0x06),
)
SGX_MODE_0 = 0
SGX_MODE_5 = 5
_SGX_MAX_DIMENSION = 255
_SGX_MAX_PAYLOAD_BYTES = 16374

# MSX Screen-7 pens 4..15 are fixed by GEOBENCH. Pens 0..3 remain the
# configurable UI pens stored in the common four-byte GBPC header.
_MSX_CHANNEL_MID = 146
_CPC_RGB = (
	(0, 0, 0),
	(0, 0, _MSX_CHANNEL_MID),
	(0, 0, 255),
	(_MSX_CHANNEL_MID, 0, 0),
	(_MSX_CHANNEL_MID, 0, _MSX_CHANNEL_MID),
	(_MSX_CHANNEL_MID, 0, 255),
	(255, 0, 0),
	(255, 0, _MSX_CHANNEL_MID),
	(255, 0, 255),
	(0, _MSX_CHANNEL_MID, 0),
	(0, _MSX_CHANNEL_MID, _MSX_CHANNEL_MID),
	(0, _MSX_CHANNEL_MID, 255),
	(_MSX_CHANNEL_MID, _MSX_CHANNEL_MID, 0),
	(_MSX_CHANNEL_MID, _MSX_CHANNEL_MID, _MSX_CHANNEL_MID),
	(_MSX_CHANNEL_MID, _MSX_CHANNEL_MID, 255),
	(255, _MSX_CHANNEL_MID, 0),
	(255, _MSX_CHANNEL_MID, _MSX_CHANNEL_MID),
	(255, _MSX_CHANNEL_MID, 255),
	(0, 255, 0),
	(0, 255, _MSX_CHANNEL_MID),
	(0, 255, 255),
	(_MSX_CHANNEL_MID, 255, 0),
	(_MSX_CHANNEL_MID, 255, _MSX_CHANNEL_MID),
	(_MSX_CHANNEL_MID, 255, 255),
	(255, 255, 0),
	(255, 255, _MSX_CHANNEL_MID),
	(255, 255, 255),
)
GBPC_MODE7_INKS = GBPC_INKS + bytes(
	(18, 2, 24, 8, 20, 15, 16, 11, 21, 5, 13, 22)
)
GBPC_MODE7_PALETTE = tuple(_CPC_RGB[ink] for ink in GBPC_MODE7_INKS)
_BIT0_FOR_PIXEL = (7, 6, 5, 4)
_BIT1_FOR_PIXEL = (3, 2, 1, 0)
_BAYER4 = (
	(0, 8, 2, 10),
	(12, 4, 14, 6),
	(3, 11, 1, 9),
	(15, 7, 13, 5),
)

_IMAGE_MIME_TYPES = {
	"gif": "image/gif",
	"jpg": "image/jpeg",
	"jpeg": "image/jpeg",
	"pic": "image/x-geobench-pic",
	"png": "image/png",
}


def _is_svg(image_data):
	"""Recognize an SVG root element without parsing untrusted XML in-process."""
	sample = image_data[:_SVG_SNIFF_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n")
	return re.search(
		br"<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|>)",
		sample,
		re.IGNORECASE,
	) is not None


def _svg_render_limits(resize, max_width, max_height, max_image_pixels):
	if max_image_pixels < 1:
		raise ValueError("The decoded image pixel limit must be positive")
	if not resize:
		dimension = min(_SVG_MAX_DIMENSION, max(1, math.isqrt(max_image_pixels)))
		return dimension, dimension

	width = min(_SVG_MAX_DIMENSION, int(max_width) if max_width else max_image_pixels)
	height = min(_SVG_MAX_DIMENSION, int(max_height) if max_height else max_image_pixels)
	if width < 1 or height < 1:
		raise ValueError("SVG render dimensions must be positive")
	if width * height > max_image_pixels:
		scale = math.sqrt(max_image_pixels / (width * height))
		width = max(1, int(width * scale))
		height = max(1, int(height * scale))
		height = min(height, max_image_pixels // width)
	return width, height


def _converter_error_detail(stderr):
	stderr.seek(0)
	detail = stderr.read(1024).decode("utf-8", errors="replace")
	detail = re.sub(r"[\x00-\x1f\x7f]+", " ", detail)
	detail = " ".join(detail.split())
	return detail[:240]


def _render_svg(
	image_data,
	*,
	resize,
	max_width,
	max_height,
	max_image_pixels,
	timeout,
	max_output_bytes,
):
	width_limit, height_limit = _svg_render_limits(
		resize,
		max_width,
		max_height,
		max_image_pixels,
	)
	command = [
		_RSVG_CONVERT,
		"-f",
		"png",
		"-a",
		"-z",
		"1",
		"-w",
		str(width_limit),
		"-h",
		str(height_limit),
	]
	with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
		try:
			completed = subprocess.run(
				command,
				input=image_data,
				stdout=output,
				stderr=errors,
				check=False,
				timeout=timeout,
				start_new_session=True,
			)
		except FileNotFoundError as error:
			raise UnidentifiedImageError(
				"SVG conversion requires /usr/bin/rsvg-convert; install librsvg2-tools"
			) from error
		except subprocess.TimeoutExpired as error:
			raise UnidentifiedImageError(
				f"SVG conversion timed out after {timeout:g} seconds"
			) from error
		except OSError as error:
			raise UnidentifiedImageError(f"Could not run rsvg-convert: {error}") from error

		if completed.returncode:
			detail = _converter_error_detail(errors)
			message = f"rsvg-convert failed with status {completed.returncode}"
			if detail:
				message += f": {detail}"
			raise UnidentifiedImageError(message)

		output.seek(0, os.SEEK_END)
		output_size = output.tell()
		if output_size < 1:
			raise UnidentifiedImageError("rsvg-convert produced no image data")
		if output_size > max_output_bytes:
			raise UnidentifiedImageError(
				f"SVG render exceeds the {max_output_bytes}-byte intermediate limit"
			)
		output.seek(0)
		try:
			image = Image.open(io.BytesIO(output.read()))
			image.load()
			return image
		except (OSError, UnidentifiedImageError) as error:
			raise UnidentifiedImageError("rsvg-convert produced an invalid image") from error


def _cache_key_lock(file_path):
	digest = hashlib.sha256(file_path.encode("utf-8")).digest()
	return _cache_locks[digest[0] % len(_cache_locks)]


def _prune_cache(cache_dir, max_cache_bytes, max_cache_files):
	entries = []
	total_bytes = 0
	try:
		for entry in os.scandir(cache_dir):
			if not entry.is_file(follow_symlinks=False):
				continue
			stat = entry.stat(follow_symlinks=False)
			entries.append((stat.st_mtime_ns, entry.path, stat.st_size))
			total_bytes += stat.st_size
	except OSError as error:
		LOGGER.warning("Could not inspect image cache: %s", error)
		return

	entries.sort()
	while entries and (len(entries) > max_cache_files or total_bytes > max_cache_bytes):
		_, path, size = entries.pop(0)
		try:
			os.unlink(path)
			total_bytes -= size
		except FileNotFoundError:
			continue
		except OSError as error:
			LOGGER.warning("Could not evict cached image %s: %s", path, error)
			break


def is_image_url(url):
	mime_type, _ = mimetypes.guess_type(url)
	return bool(mime_type and mime_type.startswith("image/"))


def image_extension(convert=True, convert_to="gif", source_url=None):
	if convert and convert_to:
		return convert_to.lower().lstrip(".")
	if source_url:
		path = source_url.split("?", 1)[0]
		extension = os.path.splitext(path)[1].lower().lstrip(".")
		if extension:
			return extension
	return "gif"


def image_mimetype(filename_or_extension):
	extension = os.path.splitext(filename_or_extension)[1].lower().lstrip(".")
	if not extension:
		extension = filename_or_extension.lower().lstrip(".")
	return _IMAGE_MIME_TYPES.get(extension, mimetypes.guess_type(filename_or_extension)[0] or "application/octet-stream")


def _clamp(value):
	return 0 if value < 0 else 255 if value > 255 else value


def _nearest_pen(red, green, blue, palette=GBPC_PALETTE):
	best_pen = 0
	best_distance = None
	for pen, (pal_red, pal_green, pal_blue) in enumerate(palette):
		distance = (pal_red - red) ** 2 + (pal_green - green) ** 2 + (pal_blue - blue) ** 2
		if best_distance is None or distance < best_distance:
			best_pen = pen
			best_distance = distance
	return best_pen


def _quantize_gbpc(image, dithering, palette=GBPC_PALETTE):
	width, height = image.size
	pixels = image.load()
	method = (dithering or "none").lower().replace("-", "").replace("_", "")

	if method in ("none", "off"):
		return [
			[_nearest_pen(*pixels[x, y], palette) for x in range(width)]
			for y in range(height)
		]

	if method == "ordered":
		pens = [[0] * width for _ in range(height)]
		for y in range(height):
			for x in range(width):
				offset = (_BAYER4[y & 3][x & 3] / 15.0 - 0.5) * 64
				red, green, blue = pixels[x, y]
				pens[y][x] = _nearest_pen(
					_clamp(red + offset),
					_clamp(green + offset),
					_clamp(blue + offset),
					palette,
				)
		return pens

	red = [[float(pixels[x, y][0]) for x in range(width)] for y in range(height)]
	green = [[float(pixels[x, y][1]) for x in range(width)] for y in range(height)]
	blue = [[float(pixels[x, y][2]) for x in range(width)] for y in range(height)]
	pens = [[0] * width for _ in range(height)]

	if method == "atkinson":
		taps = ((1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1))
		denominator = 8
	else:
		taps = ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1))
		denominator = 16

	for y in range(height):
		for x in range(width):
			rv = _clamp(red[y][x])
			gv = _clamp(green[y][x])
			bv = _clamp(blue[y][x])
			pen = _nearest_pen(rv, gv, bv, palette)
			pens[y][x] = pen
			error_red = rv - palette[pen][0]
			error_green = gv - palette[pen][1]
			error_blue = bv - palette[pen][2]
			for dx, dy, weight in taps:
				next_x = x + dx
				next_y = y + dy
				if 0 <= next_x < width and 0 <= next_y < height:
					factor = weight / denominator
					red[next_y][next_x] += error_red * factor
					green[next_y][next_x] += error_green * factor
					blue[next_y][next_x] += error_blue * factor
	return pens


def _pack_gbpc(pens):
	height = len(pens)
	width = len(pens[0]) if height else 0
	if width == 0 or width % 4:
		raise ValueError("GBPC image width must be a non-zero multiple of four")

	packed = bytearray()
	for row in pens:
		for byte_x in range(width // 4):
			value = 0
			for pixel in range(4):
				pen = row[byte_x * 4 + pixel]
				if pen & 1:
					value |= 1 << _BIT0_FOR_PIXEL[pixel]
				if pen & 2:
					value |= 1 << _BIT1_FOR_PIXEL[pixel]
			packed.append(value)
	return bytes(packed)


def _pack_gbpc_mode7(pens):
	height = len(pens)
	width = len(pens[0]) if height else 0
	if width == 0 or width % 4:
		raise ValueError("GBPC Mode-7 image width must be a non-zero multiple of four")

	packed = bytearray()
	for row in pens:
		for x in range(0, width, 2):
			packed.append((row[x] << 4) | row[x + 1])
	return bytes(packed)


def _gbpc_codec_identity(mode):
	if mode == GBPC_MODE_1:
		return "gbpc-v2-mode1-cpc-palette-v1"
	if mode == GBPC_MODE_7:
		return "gbpc-v2-mode7-msx-pal16-v1"
	raise ValueError(f"Unsupported GBPC mode: {mode}")


def encode_gbpc(image, dithering="FLOYDSTEINBERG", mode=GBPC_MODE_1):
	"""Encode an RGB Pillow image as portable GBPC v2 Mode-1 or Mode-7 data."""
	width, height = image.size
	if width > 0xFFFF or height > 0xFFFF:
		raise ValueError("GBPC dimensions exceed the v2 header limits")
	_gbpc_codec_identity(mode)
	if mode == GBPC_MODE_7 and (
		width > _GBPC_MODE7_MAX_WIDTH or height > _GBPC_MODE7_MAX_HEIGHT
	):
		raise ValueError(
			"GBPC Mode-7 dimensions exceed the GEOBENCH 512x255 limit"
		)
	palette = GBPC_MODE7_PALETTE if mode == GBPC_MODE_7 else GBPC_PALETTE
	pens = _quantize_gbpc(image, dithering, palette)
	header = b"GBPC" + bytes((2, mode)) + struct.pack("<HH", width, height) + GBPC_INKS
	bitmap = _pack_gbpc_mode7(pens) if mode == GBPC_MODE_7 else _pack_gbpc(pens)
	return header + bitmap


def _sgx_colour_count(mode, colours):
	if colours is None:
		colours = 4 if mode == SGX_MODE_0 else 16
	if (mode, colours) not in ((SGX_MODE_0, 2), (SGX_MODE_0, 4), (SGX_MODE_5, 16)):
		raise ValueError(f"Unsupported SGX mode/colour depth: {mode},{colours}")
	return colours


def encode_sgx_pixels(pens, mode=SGX_MODE_0, colours=None):
	"""Encode palette indexes as one uncompressed extended SymbOS SGX object.

	DOX ``GRPH`` records always use the extended on-disc wrapper, including for
	four-colour graphics: marker 64, encoding type, three little-endian size
	words, then the packed bitmap.
	"""
	colours = _sgx_colour_count(mode, colours)
	if not pens or not pens[0]:
		raise ValueError("Cannot encode an empty SGX image")
	height = len(pens)
	width = len(pens[0])
	if width > _SGX_MAX_DIMENSION or height > _SGX_MAX_DIMENSION:
		raise ValueError("SGX dimensions exceed SymZilla's 255x255 limit")
	width_multiple = 8 if mode == SGX_MODE_0 else 4
	if width % width_multiple:
		raise ValueError(
			f"SGX mode {mode} image width must be a multiple of {width_multiple}"
		)
	max_pen = colours - 1
	for row in pens:
		if len(row) != width:
			raise ValueError("SGX rows must all have the same width")
		if any(not isinstance(pen, int) or pen < 0 or pen > max_pen for pen in row):
			raise ValueError(f"SGX mode {mode} palette indexes must be 0-{max_pen}")

	if mode == SGX_MODE_0:
		bitmap = _pack_gbpc(pens)
		width_bytes = width // 4
	else:
		bitmap = _pack_gbpc_mode7(pens)
		width_bytes = width // 2
	if len(bitmap) > _SGX_MAX_PAYLOAD_BYTES:
		raise ValueError("SGX bitmap exceeds the SymZilla bank limit")
	return bytes((0x40, mode)) + struct.pack(
		"<HHH", width_bytes, width, height
	) + bitmap


def encode_sgx(
	image,
	dithering="FLOYDSTEINBERG",
	mode=SGX_MODE_0,
	colours=None,
):
	"""Encode a Pillow image using the fixed SymbOS 4- or 16-colour palette."""
	colours = _sgx_colour_count(mode, colours)
	image = _as_rgb(image)
	width, height = image.size
	if width > _SGX_MAX_DIMENSION or height > _SGX_MAX_DIMENSION:
		raise ValueError("SGX dimensions exceed SymZilla's 255x255 limit")
	palette = SYMBOS_PALETTE[:colours]
	pens = _quantize_gbpc(image, dithering, palette)
	return encode_sgx_pixels(pens, mode=mode, colours=colours)


def convert_to_sgx(
	image_data,
	*,
	mode=SGX_MODE_0,
	colours=None,
	max_width=160,
	max_height=96,
	dithering="FLOYDSTEINBERG",
	max_image_pixels=16 * 1024 * 1024,
	svg_timeout=_SVG_CONVERSION_TIMEOUT,
	max_intermediate_bytes=None,
):
	"""Decode, bound, resize, and encode untrusted image bytes as SGX."""
	colours = _sgx_colour_count(mode, colours)
	max_width = min(_SGX_MAX_DIMENSION, int(max_width))
	max_height = min(_SGX_MAX_DIMENSION, int(max_height))
	if min(max_width, max_height, max_image_pixels) < 1:
		raise ValueError("SGX image and pixel limits must be positive")
	if max_intermediate_bytes is None:
		max_intermediate_bytes = max(1024 * 1024, max_image_pixels * 5)
	image = _open_image(
		image_data,
		resize=True,
		max_width=max_width,
		max_height=max_height,
		max_image_pixels=max_image_pixels,
		svg_timeout=svg_timeout,
		max_intermediate_bytes=max_intermediate_bytes,
	)
	if image.width * image.height > max_image_pixels:
		raise ValueError(f"Decoded image exceeds the {max_image_pixels}-pixel limit")
	image = _as_rgb(image)
	width_multiple = 8 if mode == SGX_MODE_0 else 4
	image = _resize_to_fit(
		image, max_width, max_height, width_multiple=width_multiple
	)
	return encode_sgx(image, dithering=dithering, mode=mode, colours=colours)


def _as_rgb(image):
	image.load()
	if "A" in image.getbands() or "transparency" in image.info:
		rgba = image.convert("RGBA")
		background = Image.new("RGB", rgba.size, (255, 255, 255))
		background.paste(rgba, mask=rgba.getchannel("A"))
		return background
	return image.convert("RGB")


def _resize_to_fit(image, max_width, max_height, width_multiple=1):
	width, height = image.size
	if width < 1 or height < 1:
		raise ValueError("Cannot convert an empty image")

	ratio = 1.0
	if max_width:
		ratio = min(ratio, max_width / width)
	if max_height:
		ratio = min(ratio, max_height / height)
	target_width = max(1, int(round(width * ratio)))

	if width_multiple > 1:
		target_width = max(width_multiple, (target_width // width_multiple) * width_multiple)
		if max_width and target_width > max_width:
			target_width = max(width_multiple, (max_width // width_multiple) * width_multiple)

	target_height = max(1, int(round(height * target_width / width)))
	if max_height and target_height > max_height:
		# Extremely narrow inputs may require the minimum encodable row width
		# while their aspect-ratio height would exceed the target. Keep the
		# wire-format alignment and both hard bounds; a small amount of
		# distortion is preferable to emitting dimensions the client cannot load.
		target_height = max_height

	if (target_width, target_height) == image.size:
		return image
	return image.resize((target_width, target_height), _RESAMPLING.LANCZOS)


def _open_image(
	image_data,
	*,
	resize,
	max_width,
	max_height,
	max_image_pixels,
	svg_timeout,
	max_intermediate_bytes,
):
	try:
		return Image.open(io.BytesIO(image_data))
	except UnidentifiedImageError:
		if not _is_svg(image_data):
			raise
		return _render_svg(
			image_data,
			resize=resize,
			max_width=max_width,
			max_height=max_height,
			max_image_pixels=max_image_pixels,
			timeout=svg_timeout,
			max_output_bytes=max_intermediate_bytes,
		)


def _optimize_image(
	image_data,
	resize,
	max_width,
	max_height,
	convert,
	convert_to,
	dithering,
	max_image_pixels,
	svg_timeout,
	max_intermediate_bytes,
	gbpc_mode,
):
	target_format = (convert_to or "").lower()
	image = _open_image(
		image_data,
		resize=resize,
		max_width=max_width,
		max_height=max_height,
		max_image_pixels=max_image_pixels,
		svg_timeout=svg_timeout,
		max_intermediate_bytes=max_intermediate_bytes,
	)
	if image.width * image.height > max_image_pixels:
		raise ValueError(
			f"Decoded image exceeds the {max_image_pixels}-pixel limit"
		)
	source_format = image.format or "PNG"
	image = _as_rgb(image)

	if resize or target_format == "pic":
		width_multiple = 4 if target_format == "pic" else 1
		fit_width = max_width if resize else None
		fit_height = max_height if resize else None
		image = _resize_to_fit(image, fit_width, fit_height, width_multiple)

	if convert and target_format == "pic":
		return encode_gbpc(image, dithering, mode=gbpc_mode)

	if convert and target_format == "gif":
		image = image.convert("L")
		dither_method = _DITHER.FLOYDSTEINBERG if (dithering or "").upper() == "FLOYDSTEINBERG" else _DITHER.NONE
		image = image.convert("1", dither=dither_method)

	output = io.BytesIO()
	if convert and target_format:
		save_format = {"jpg": "JPEG"}.get(target_format, target_format.upper())
	else:
		save_format = source_format
	image.save(output, format=save_format, optimize=True)
	return output.getvalue()


def optimize_image(image_data, resize=True, max_width=512, max_height=342,
				  convert=True, convert_to="gif", dithering="FLOYDSTEINBERG",
				  max_image_pixels=16 * 1024 * 1024,
				  svg_timeout=_SVG_CONVERSION_TIMEOUT,
				  max_intermediate_bytes=None, gbpc_mode=GBPC_MODE_1):
	"""Resize and convert image bytes, preserving legacy behavior on failure."""
	if max_intermediate_bytes is None:
		max_intermediate_bytes = max(1024 * 1024, max_image_pixels * 5)
	try:
		return _optimize_image(
			image_data,
			resize,
			max_width,
			max_height,
			convert,
			convert_to,
			dithering,
			max_image_pixels,
			svg_timeout,
			max_intermediate_bytes,
			gbpc_mode,
		)
	except Exception as error:
		LOGGER.warning("Could not optimize image: %s", error)
		if convert or resize:
			return None
		return image_data


def fetch_and_cache_image(url, content=None, resize=True, max_width=512, max_height=342,
						 convert=True, convert_to="gif", dithering="FLOYDSTEINBERG",
						 hash_url=True, cache_dir=None, timeout=30,
						 svg_timeout=_SVG_CONVERSION_TIMEOUT,
						 max_download_bytes=16 * 1024 * 1024,
						 max_cache_bytes=512 * 1024 * 1024, max_cache_files=4096,
						 max_image_pixels=16 * 1024 * 1024,
						 gbpc_mode=GBPC_MODE_1):
	try:
		LOGGER.info("Processing image from %s", url.split("?", 1)[0])
		if min(max_download_bytes, max_cache_bytes, max_cache_files, max_image_pixels) < 1:
			raise ValueError("Image download, cache, and pixel limits must be positive")
		cache_dir = os.path.abspath(cache_dir or CACHE_DIR)
		extension = image_extension(convert, convert_to, url)
		target_format = (convert_to or "").lower().lstrip(".")
		codec_identity = (
			_gbpc_codec_identity(gbpc_mode)
			if convert and target_format == "pic"
			else "not-gbpc"
		)
		cache_material = "\0".join((
			_CACHE_FORMAT_VERSION,
			url,
			str(bool(resize)),
			str(max_width),
			str(max_height),
			str(bool(convert)),
			str(convert_to),
			str(dithering),
			str(max_image_pixels),
			codec_identity,
		)).encode("utf-8")
		if content is not None:
			cache_material += b"\0" + hashlib.sha256(content).digest()
		cache_key = hashlib.sha256(cache_material).hexdigest()
		file_name = f"{cache_key}.{extension}"
		file_path = os.path.join(cache_dir, file_name)

		os.makedirs(cache_dir, exist_ok=True)
		with _cache_key_lock(file_path):
			if not os.path.exists(file_path):
				LOGGER.debug("Converting image into cache file %s", file_name)
				if content is None:
					response = None
					try:
						response = requests.get(
							url,
							stream=True,
							headers={"User-Agent": USER_AGENT},
							timeout=timeout,
						)
						response.raise_for_status()
						chunks = []
						total = 0
						for chunk in response.iter_content(chunk_size=64 * 1024):
							if not chunk:
								continue
							total += len(chunk)
							if total > max_download_bytes:
								raise ValueError(
									f"Image exceeds the {max_download_bytes}-byte download limit"
								)
							chunks.append(chunk)
						content = b"".join(chunks)
					finally:
						if response is not None:
							response.close()
				elif len(content) > max_download_bytes:
					raise ValueError(
						f"Image exceeds the {max_download_bytes}-byte download limit"
					)

				if convert or resize:
					try:
						optimized_image = _optimize_image(
							content,
							resize,
							max_width,
							max_height,
							convert,
							convert_to,
							dithering,
							max_image_pixels,
							svg_timeout,
							min(max_cache_bytes, max(1024 * 1024, max_image_pixels * 5)),
							gbpc_mode,
						)
					except Exception as error:
						raise ValueError(f"Image conversion failed: {error}") from error
				else:
					optimized_image = content

				if len(optimized_image) > max_cache_bytes:
					raise ValueError(
						f"Converted image exceeds the {max_cache_bytes}-byte cache limit"
					)
				temp_path = None
				try:
					with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as cache_file:
						temp_path = cache_file.name
						cache_file.write(optimized_image)
					os.replace(temp_path, file_path)
				finally:
					if temp_path and os.path.exists(temp_path):
						os.unlink(temp_path)
				_prune_cache(cache_dir, max_cache_bytes, max_cache_files)
			else:
				LOGGER.debug("Using cached image %s", file_name)

		cached_url = f"/cached_image/{file_name}"
		return cached_url
	except Exception as error:
		LOGGER.warning("Could not process image from %s: %s", url.split("?", 1)[0], error)
		return None
