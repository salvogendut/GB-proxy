"""Image download, conversion, and cache helpers."""

import hashlib
import io
import mimetypes
import os
import struct
import tempfile

import requests
from PIL import Image, UnidentifiedImageError
from PILSVG import SVG


CACHE_DIR = os.path.join(os.path.dirname(__file__), "cached_images")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

GBPC_PALETTE = (
	(0x00, 0x00, 0x80),
	(0xFF, 0xFF, 0xFF),
	(0x00, 0x00, 0x00),
	(0xFF, 0x00, 0x00),
)
GBPC_INKS = bytes((1, 26, 0, 6))
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


def get_svg_renderer():
	"""Prefer Inkscape when available and otherwise use pillow-svg's Skia path."""
	renderer = "skia"
	for path in os.environ.get("PATH", "").split(os.pathsep):
		if os.path.exists(os.path.expandvars(os.path.join(path, "inkscape"))):
			renderer = "inkscape"
			break
	return renderer


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


def _nearest_pen(red, green, blue):
	best_pen = 0
	best_distance = None
	for pen, (pal_red, pal_green, pal_blue) in enumerate(GBPC_PALETTE):
		distance = (pal_red - red) ** 2 + (pal_green - green) ** 2 + (pal_blue - blue) ** 2
		if best_distance is None or distance < best_distance:
			best_pen = pen
			best_distance = distance
	return best_pen


def _quantize_gbpc(image, dithering):
	width, height = image.size
	pixels = image.load()
	method = (dithering or "none").lower().replace("-", "").replace("_", "")

	if method in ("none", "off"):
		return [[_nearest_pen(*pixels[x, y]) for x in range(width)] for y in range(height)]

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
			pen = _nearest_pen(rv, gv, bv)
			pens[y][x] = pen
			error_red = rv - GBPC_PALETTE[pen][0]
			error_green = gv - GBPC_PALETTE[pen][1]
			error_blue = bv - GBPC_PALETTE[pen][2]
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


def encode_gbpc(image, dithering="FLOYDSTEINBERG"):
	"""Encode an RGB Pillow image as canonical portable GBPC v2 Mode-1 data."""
	width, height = image.size
	if width > 0xFFFF or height > 0xFFFF:
		raise ValueError("GBPC dimensions exceed the v2 header limits")
	pens = _quantize_gbpc(image, dithering)
	header = b"GBPC" + bytes((2, 1)) + struct.pack("<HH", width, height) + GBPC_INKS
	return header + _pack_gbpc(pens)


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
		target_height = max_height
		target_width = max(1, int(round(width * target_height / height)))
		if width_multiple > 1:
			target_width = max(width_multiple, (target_width // width_multiple) * width_multiple)
		target_height = max(1, int(round(height * target_width / width)))

	if (target_width, target_height) == image.size:
		return image
	return image.resize((target_width, target_height), Image.Resampling.LANCZOS)


def _open_image(image_data):
	try:
		return Image.open(io.BytesIO(image_data))
	except UnidentifiedImageError:
		with tempfile.NamedTemporaryFile(delete=False) as temp_file:
			temp_file.write(image_data)
			temp_path = temp_file.name
		try:
			return SVG(temp_path).im(renderer=get_svg_renderer())
		finally:
			os.unlink(temp_path)


def optimize_image(image_data, resize=True, max_width=512, max_height=342,
				  convert=True, convert_to="gif", dithering="FLOYDSTEINBERG"):
	"""Resize and convert image bytes, preserving legacy behavior on failure."""
	target_format = (convert_to or "").lower()
	try:
		image = _open_image(image_data)
		source_format = image.format or "PNG"
		image = _as_rgb(image)

		if resize or target_format == "pic":
			width_multiple = 4 if target_format == "pic" else 1
			fit_width = max_width if resize else None
			fit_height = max_height if resize else None
			image = _resize_to_fit(image, fit_width, fit_height, width_multiple)

		if convert and target_format == "pic":
			return encode_gbpc(image, dithering)

		if convert and target_format == "gif":
			image = image.convert("L")
			dither_method = Image.Dither.FLOYDSTEINBERG if (dithering or "").upper() == "FLOYDSTEINBERG" else Image.Dither.NONE
			image = image.convert("1", dither=dither_method)

		output = io.BytesIO()
		if convert and target_format:
			save_format = {"jpg": "JPEG"}.get(target_format, target_format.upper())
		else:
			save_format = source_format
		image.save(output, format=save_format, optimize=True)
		return output.getvalue()
	except Exception as error:
		print(f"Error optimizing image: {error}")
		if convert and target_format == "pic":
			return None
		return image_data


def fetch_and_cache_image(url, content=None, resize=True, max_width=512, max_height=342,
						 convert=True, convert_to="gif", dithering="FLOYDSTEINBERG",
						 hash_url=True):
	try:
		print(f"Processing image: {url}")
		extension = image_extension(convert, convert_to, url)
		cache_key = hashlib.md5(url.encode()).hexdigest() if hash_url else url
		file_name = f"{cache_key}.{extension}"
		file_path = os.path.join(CACHE_DIR, file_name)

		if not os.path.exists(file_path):
			print(f"Optimizing and caching image: {url}")
			if content is None:
				response = requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=30)
				response.raise_for_status()
				content = response.content

			if convert or resize:
				optimized_image = optimize_image(
					content,
					resize=resize,
					max_width=max_width,
					max_height=max_height,
					convert=convert,
					convert_to=convert_to,
					dithering=dithering,
				)
			else:
				optimized_image = content

			if optimized_image is None:
				raise ValueError("Image conversion produced no output")
			os.makedirs(CACHE_DIR, exist_ok=True)
			with open(file_path, "wb") as cache_file:
				cache_file.write(optimized_image)
		else:
			print(f"Image already cached: {url}")

		cached_url = f"/cached_image/{file_name}"
		print(f"Cached URL: {cached_url}")
		return cached_url
	except Exception as error:
		print(f"Error processing image: {url}, Error: {error}")
		return None


os.makedirs(CACHE_DIR, exist_ok=True)
