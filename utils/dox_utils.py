"""Bounded HTML/image to SymZilla DOX conversion and validation helpers."""

import base64
import binascii
import hashlib
import logging
import re
import struct
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag

from utils.image_utils import (
	GBPC_MODE_1,
	GBPC_MODE_7,
	SGX_MODE_0,
	SGX_MODE_5,
	convert_to_gbpc,
	convert_to_sgx,
	encode_gbpc_pixels,
	encode_sgx_pixels,
)


LOGGER = logging.getLogger(__name__)
DOX_MIMETYPE = "application/x-symbos-dox"
DOX_MAX_GRAPHIC_ENTRY_BYTES = 16382
DOX_MAX_DOCUMENT_BYTES = 96 * 1024
DOX_MAX_TEXT_BYTES = 11764
DOX_MAX_CONTROLS = 16
DOX_MAX_CONTROL_WORKING_BYTES = 2 * 1024
DOX_CONTROL_EXTENSION_BYTES = 15
DOX_MAX_FORM_ACTION_BYTES = 2048
DOX_MAX_CONTROL_NAME_BYTES = 31
DOX_MAX_CONTROL_VALUE_BYTES = 63
DOX_MAX_CONTROL_LABEL_BYTES = 31
DOX_MAX_TABLE_IMAGE_ALT_BYTES = 63
DOX_MAX_IMAGE_SOURCE_CACHE_BYTES = 16 * 1024 * 1024
DOX_DOCUMENT_OVERHEAD_RESERVE = 192
DOX_MIN_DOCUMENT_WIDTH = 200
DOX_MAX_DOCUMENT_WIDTH = 600
_TEXT_TRAILER = b"\x04\x02\x01\x01\x00\xff"
_TEXT_FORMAT_RESET = _TEXT_TRAILER[:-2]
_FORM_MARKER_SUFFIX = b"\x80\x00\x01\x05\x01"
_CHUNK_NAMES = (b"INFO", b"HEAD", b"TEXT", b"GRPH", b"LINK", b"CTRL", b"ENDF")
_REQUIRED_CHUNKS = frozenset((b"INFO", b"HEAD", b"TEXT", b"GRPH", b"LINK", b"ENDF"))
_DIRECT_URL_BYTES = frozenset(
	b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
	b"-._~:/?#[]@!$&'()*+,;=%"
)
_BLOCK_TAGS = frozenset((
	"address", "article", "aside", "blockquote", "caption", "dd", "div", "dl",
	"dt", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
	"h5", "h6", "header", "li", "main", "nav", "ol", "p", "section", "table",
	"tbody", "td", "tfoot", "th", "thead", "tr", "ul",
))
_REMOVED_TAGS = frozenset((
	"applet", "audio", "canvas", "embed", "iframe", "link", "noscript", "object",
	"script", "source", "style", "template", "video",
))
_TABLE_INLINE_TAGS = frozenset((
	"a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "data", "del",
	"center", "dfn", "em", "i", "img", "ins", "kbd", "mark", "q", "s", "samp", "small",
	"span", "strong", "sub", "sup", "time", "u", "var", "wbr",
))
_TABLE_COLUMN_HEADER_BYTES = 14
_TABLE_CELL_STYLE = b"\x33\x23\x13"
_TABLE_CELL_HORIZONTAL_OVERHEAD = 5
_TABLE_CENTER_FORMAT = b"\x09\x01\x03\x01"
_GEOBENCH_EXTERNAL_GRAPHIC = 1


class DoxError(ValueError):
	"""Base class for malformed or unrepresentable DOX data."""


class DoxValidationError(DoxError):
	"""Raised when a serialized document violates the supported DOX subset."""


@dataclass(frozen=True)
class SgxProfile:
	"""The SGX encoding and actual colour depth advertised by SymZilla."""

	mode: int = SGX_MODE_0
	colours: int = 2

	def __post_init__(self):
		if (self.mode, self.colours) not in (
			(SGX_MODE_0, 2),
			(SGX_MODE_0, 4),
			(SGX_MODE_5, 16),
		):
			raise ValueError("Unsupported SGX profile")

	@property
	def header_value(self):
		return f"{self.mode},{self.colours}"


SAFE_SGX_PROFILE = SgxProfile()


@dataclass(frozen=True)
class GbpcProfile:
	"""The GBPC v2 graphic codec requested by GEOBENCH BROWSER.APP."""

	mode: int = GBPC_MODE_1

	def __post_init__(self):
		if self.mode not in (GBPC_MODE_1, GBPC_MODE_7):
			raise ValueError("Unsupported GBPC DOX profile")

	@property
	def header_value(self):
		return str(self.mode)


def parse_sgx_profile(value):
	"""Parse the strict capability grammar, defaulting to two-colour SGX0."""
	profiles = {
		"0,2": SgxProfile(SGX_MODE_0, 2),
		"0,4": SgxProfile(SGX_MODE_0, 4),
		"5,16": SgxProfile(SGX_MODE_5, 16),
	}
	if not isinstance(value, str):
		return SAFE_SGX_PROFILE
	return profiles.get(value.strip(), SAFE_SGX_PROFILE)


@dataclass(frozen=True)
class DoxLimits:
	"""Hard bounds chosen to fit SymZilla's text and banked-memory loaders."""

	max_text_bytes: int = 11500
	max_links: int = 64
	max_graphics: int = 8
	max_graphics_bytes: int = 64 * 1024
	max_controls: int = 16
	max_control_bytes: int = 2 * 1024
	max_document_bytes: int = 96 * 1024
	max_image_width: int = 160
	max_image_height: int = 96
	max_image_source_bytes: int = 2 * 1024 * 1024
	max_image_pixels: int = 16 * 1024 * 1024
	max_url_bytes: int = 127
	max_table_columns: int = 4
	max_table_rows: int = 64
	max_table_cells: int = 256

	def __post_init__(self):
		for name, value in self.__dict__.items():
			if not isinstance(value, int) or value < 1:
				raise ValueError(f"{name} must be a positive integer")
		if self.max_text_bytes < len(_TEXT_TRAILER) or self.max_text_bytes > DOX_MAX_TEXT_BYTES:
			raise ValueError(
				f"SymZilla TEXT chunks must be {len(_TEXT_TRAILER)}-{DOX_MAX_TEXT_BYTES} bytes"
			)
		if self.max_links > 254:
			raise ValueError("SymZilla supports at most 254 links")
		if self.max_controls > DOX_MAX_CONTROLS:
			raise ValueError(f"SymZilla supports at most {DOX_MAX_CONTROLS} controls")
		if self.max_control_bytes < 7:
			raise ValueError("SymZilla CTRL limit must fit its empty canonical payload")
		if self.max_control_bytes > DOX_MAX_CONTROL_WORKING_BYTES:
			raise ValueError("SymZilla CTRL working allocation is limited to 2 KiB")
		if self.max_graphics > 127:
			raise ValueError("SymZilla safely supports at most 127 graphics")
		if self.max_graphics_bytes < 40:
			raise ValueError("DOX graphics limit must fit its link icon")
		if not 8 <= self.max_image_width <= 248 or self.max_image_height > 255:
			raise ValueError("SymZilla images must fit its byte-sized dimensions")
		aligned_width = (self.max_image_width // 4) * 4
		if aligned_width // 2 * self.max_image_height > DOX_MAX_GRAPHIC_ENTRY_BYTES - 8:
			raise ValueError("SymZilla SGX5 images must fit one 16K memory area")
		if self.max_url_bytes > 127:
			raise ValueError("SymZilla history holds at most 127 URL bytes")
		if not 2 <= self.max_table_columns <= 15:
			raise ValueError("SymZilla tables must allow between 2 and 15 columns")
		if (
			self.max_document_bytes
			< self.max_text_bytes + self.max_control_bytes + DOX_DOCUMENT_OVERHEAD_RESERVE
		):
			raise ValueError("SymZilla document limit is too small for its TEXT and CTRL limits")
		if self.max_document_bytes > DOX_MAX_DOCUMENT_BYTES:
			raise ValueError("SymZilla accepts DOX documents up to 96 KiB")


@dataclass(frozen=True)
class _FittedTableLayout:
	"""A bounded legacy pixel table which SymZilla can center at any width."""

	table_width: int
	cell_widths: tuple
	image_bounds: tuple


def _chunk(name, payload):
	if name not in _CHUNK_NAMES:
		raise ValueError(f"Unsupported DOX chunk {name!r}")
	return name + struct.pack("<I", len(payload)) + payload


def _dox_fixed(value):
	"""Encode a signed 14-bit DOX column constant using marked 7-bit bytes."""
	if not -8192 <= value <= 8191:
		raise ValueError("DOX column constant exceeds its signed 14-bit range")
	value &= 0x3fff
	return bytes((
		((value & 0x7f) << 1) | 1,
		(((value >> 7) & 0x7f) << 1) | 1,
	))


def _decode_dox_fixed(data):
	if len(data) != 2 or not data[0] & 1 or not data[1] & 1:
		raise DoxValidationError("Invalid marked DOX column constant")
	value = (data[0] >> 1) | ((data[1] >> 1) << 7)
	return value - 0x4000 if value & 0x2000 else value


def _table_column_header(column, count):
	return (
		bytes((_TABLE_COLUMN_HEADER_BYTES, 0, 0))
		+ _dox_fixed(2 - column) + bytes((column + 1, count))
		+ _dox_fixed(-1) + bytes((2, count))
		+ _TABLE_CELL_STYLE
	)


def _fitted_table_column_header(column, cell_widths):
	column_sizes = tuple(width - 1 for width in cell_widths)
	position = -(sum(column_sizes) // 2) + sum(column_sizes[:column])
	return (
		bytes((_TABLE_COLUMN_HEADER_BYTES, 0, 0))
		+ _dox_fixed(position) + b"\x02\x02"
		+ _dox_fixed(cell_widths[column] - 1) + b"\x01\x01"
		+ _TABLE_CELL_STYLE
	)


def _validate_table_geometry(headers, column_count, minimum_width, maximum_width):
	canonical = all(
		header[3:] == _table_column_header(column, column_count)[3:]
		for column, header in enumerate(headers)
	)
	if canonical:
		return
	if column_count > 4:
		raise DoxValidationError("Fitted DOX tables support at most four columns")
	if any(
		header[5:7] != b"\x02\x02"
		or header[9:11] != b"\x01\x01"
		or header[11:] != _TABLE_CELL_STYLE
		for header in headers
	):
		raise DoxValidationError("Non-canonical DOX table geometry or frame style")

	positions = [_decode_dox_fixed(header[3:5]) for header in headers]
	sizes = [_decode_dox_fixed(header[7:9]) for header in headers]
	if any(size < _TABLE_CELL_HORIZONTAL_OVERHEAD for size in sizes):
		raise DoxValidationError("Fitted DOX table column is too narrow")
	for width in (minimum_width, maximum_width):
		evaluated = [constant + width // 2 for constant in positions]
		if evaluated[0] < 0 or evaluated[-1] + sizes[-1] > width:
			raise DoxValidationError("Fitted DOX table extends outside the document")
		if any(
			evaluated[column] + sizes[column] != evaluated[column + 1]
			for column in range(column_count - 1)
		):
			raise DoxValidationError("Fitted DOX table columns are not contiguous")
		left_margin = evaluated[0]
		right_margin = width - evaluated[-1] - sizes[-1]
		if abs(left_margin - right_margin) > 1:
			raise DoxValidationError("Fitted DOX table is not centered")


def _ascii(value, *, limit=None):
	value = unicodedata.normalize("NFKD", str(value))
	value = value.encode("ascii", errors="ignore").decode("ascii")
	value = "".join(
		character if 32 <= ord(character) <= 126 else " " for character in value
	)
	data = value.encode("ascii")
	return data if limit is None else data[:limit]


def _direct_url_bytes(value, limit):
	"""Return an exact printable ASCII URL when SymZilla can retain it."""
	try:
		data = str(value).encode("ascii")
	except UnicodeEncodeError:
		return None
	if not data or len(data) > limit:
		return None
	if any(byte not in _DIRECT_URL_BYTES for byte in data):
		return None
	if not data.lower().startswith((b"http://", b"https://")):
		return None
	return data


def _absolute_http_url(base_url, value):
	if not value:
		return None
	value = str(value).strip()
	if value.startswith("//"):
		value = f"{urlparse(base_url).scheme or 'http'}:{value}"
	url = urljoin(base_url, value)
	parsed = urlparse(url)
	if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
		return None
	return url


def _data_uri(value, limit):
	try:
		header, payload = value.split(",", 1)
		if ";base64" in header.lower():
			data = base64.b64decode(payload, validate=True)
		else:
			data = unquote_to_bytes(payload)
	except (ValueError, binascii.Error):
		return None
	return data if len(data) <= limit else None


def _link_icon(profile):
	# Eight pixels wide so SGX0's row-byte count remains even. Index 0 is the
	# desktop background and index 1 is near-black in every supported profile.
	pixels = (
		(0, 0, 1, 1, 1, 0, 0, 0),
		(0, 1, 0, 0, 0, 1, 0, 0),
		(1, 0, 0, 1, 0, 0, 1, 0),
		(1, 0, 1, 1, 1, 0, 1, 0),
		(1, 0, 0, 1, 0, 0, 1, 0),
		(0, 1, 0, 0, 0, 1, 0, 0),
		(0, 0, 1, 1, 1, 0, 0, 0),
		(0, 0, 0, 0, 0, 0, 0, 0),
	)
	if isinstance(profile, GbpcProfile):
		return encode_gbpc_pixels(pixels, mode=profile.mode)
	return encode_sgx_pixels(pixels, mode=profile.mode, colours=profile.colours)


class _DoxBuilder:
	def __init__(
		self,
		base_url,
		profile,
		limits,
		image_fetcher,
		*,
		image_shortener,
		link_shortener,
		dithering,
		svg_timeout,
	):
		self.base_url = base_url
		self.profile = profile
		self.limits = limits
		self.image_fetcher = image_fetcher
		self.image_shortener = image_shortener
		self.link_shortener = link_shortener
		self.dithering = dithering
		self.svg_timeout = svg_timeout
		self.text = bytearray()
		self.graphics = []
		self.links = []
		self.controls = []
		self.control_strings = []
		self._link_ids = {}
		self._link_targets = {}
		self._control_string_ids = {}
		self._control_ids = {}
		self._image_ids = {}
		self._image_contents = {}
		self._image_content_bytes = 0
		self._graphic_ids = {}
		self._graphics_bytes = 0
		self._reserved_text_bytes = 0
		self._link_icon_id = None
		self._linked_graphic_insertions = 0
		self._image_fetches = 0
		self._image_conversions = 0
		self._standard_paragraph_open = False
		self._table_fallback_depth = 0
		self._table_rows = 0
		self._table_cells = 0
		self._table_image_width = None
		self._table_image_height = None
		self._document_min_width = DOX_MIN_DOCUMENT_WIDTH

	def _append_control(self, data):
		if (
			len(self.text) + self._reserved_text_bytes + len(data) + len(_TEXT_TRAILER)
			> self.limits.max_text_bytes
		):
			return False
		self.text.extend(data)
		self._standard_paragraph_open = True
		return True

	def append_text(self, value, *, preserve=False):
		value = str(value)
		if preserve:
			parts = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
			for number, part in enumerate(parts):
				if number:
					self.line_break()
				self._append_ascii(part)
			return
		value = re.sub(r"\s+", " ", value)
		if not value:
			return
		if value.startswith(" ") and (
			not self._standard_paragraph_open or self.text[-1] in (3, 32)
		):
			value = value.lstrip(" ")
		self._append_ascii(value)

	def _append_ascii(self, value):
		data = _ascii(value)
		remaining = (
			self.limits.max_text_bytes - len(self.text)
			- self._reserved_text_bytes - len(_TEXT_TRAILER)
		)
		if remaining > 0:
			data = data[:remaining]
			self.text.extend(data)
			if data:
				self._standard_paragraph_open = True

	def line_break(self):
		if self.text.endswith(b"\x08\x03"):
			return
		self._append_control(b"\x08\x03")

	def _add_graphic(self, graphic):
		graphic_id = self._graphic_ids.get(graphic)
		if graphic_id is not None:
			return graphic_id
		if len(self.graphics) >= self.limits.max_graphics:
			return None
		if len(graphic) > DOX_MAX_GRAPHIC_ENTRY_BYTES:
			return None
		if self._graphics_bytes + len(graphic) > self.limits.max_graphics_bytes:
			return None
		self.graphics.append(graphic)
		self._graphics_bytes += len(graphic)
		graphic_id = len(self.graphics)
		self._graphic_ids[graphic] = graphic_id
		return graphic_id

	def _add_link(self, value, *, unique=False, always_shorten=False):
		url = _absolute_http_url(self.base_url, value)
		if url is None:
			return None
		if not unique and url in self._link_targets:
			return self._link_targets[url]
		# Shortening registers a proxy-local token. Check the document limit
		# first so links we cannot emit do not evict tokens that we did emit.
		if len(self.links) >= self.limits.max_links:
			return None
		target = url
		direct_data = _direct_url_bytes(url, self.limits.max_url_bytes)
		if self.link_shortener is not None and (always_shorten or direct_data is None):
			try:
				url = self.link_shortener(url)
			except Exception as error:
				LOGGER.warning("Could not shorten DOX link %s: %s", url, error)
				return None
		if _absolute_http_url(self.base_url, url) is None:
			return None
		data = _ascii(url)
		if (
			not data
			or len(data) > self.limits.max_url_bytes
			or not data.lower().startswith((b"http://", b"https://"))
		):
			return None
		url = data.decode("ascii")
		if not unique and url in self._link_ids:
			link_id = self._link_ids[url]
			self._link_targets[target] = link_id
			return link_id
		entry = b"\x00" + data + b"\x00"
		self.links.append(entry)
		link_id = len(self.links)
		if not unique:
			self._link_ids[url] = link_id
			self._link_targets[target] = link_id
		return link_id

	def _insert_graphic(self, graphic_id, link_id=None):
		# 10,2 is SymZilla's inline-graphic command. It must be followed by a
		# 5,x spacing command, which the renderer updates and consumes.
		link_id = link_id if link_id is not None else 0
		inserted = self._append_control(
			bytes((10, 2, graphic_id, 0x80, link_id, 1, 5, 1))
		)
		if inserted and link_id:
			self._linked_graphic_insertions += 1
		return inserted

	def _ensure_link_icon(self):
		if self._link_icon_id is None:
			self._link_icon_id = self._add_graphic(_link_icon(self.profile))
		return self._link_icon_id

	def append_link_icon(self, link_id):
		graphic_id = self._ensure_link_icon()
		if graphic_id is not None:
			self._insert_graphic(graphic_id, link_id)

	def _image_fallback(self, alt):
		if alt:
			limit = (
				DOX_MAX_TABLE_IMAGE_ALT_BYTES
				if self._table_image_width is not None else None
			)
			alt = _ascii(alt, limit=limit).decode("ascii")
			if alt:
				self.append_text(f"[{alt}]")
		return False

	@staticmethod
	def _image_source(node):
		source = node.get("data-src") or node.get("data-original") or node.get("src")
		if not source and node.get("srcset"):
			source = str(node["srcset"]).split(",", 1)[0].strip().split(" ", 1)[0]
		return source

	def append_image(self, source, alt="", link_id=None, content=None):
		max_width = min(
			self.limits.max_image_width,
			self._table_image_width or self.limits.max_image_width,
		)
		max_height = min(
			self.limits.max_image_height,
			self._table_image_height or self.limits.max_image_height,
		)
		if isinstance(self.profile, GbpcProfile) and self.image_shortener is not None:
			target = _absolute_http_url(self.base_url, source)
			if content is None and str(source).startswith("data:"):
				content = _data_uri(str(source), self.limits.max_image_source_bytes)
				if content is not None:
					target = (
						self.base_url + "#inline-image-"
						+ hashlib.sha256(content).hexdigest()
					)
			if target is None or (
				content is not None and len(content) > self.limits.max_image_source_bytes
			):
				return self._image_fallback(alt)
			try:
				url = self.image_shortener(target, content, max_width, max_height)
			except Exception as error:
				LOGGER.warning("Could not shorten DOX image %s: %s", target, error)
				return self._image_fallback(alt)
			data = _direct_url_bytes(url, self.limits.max_url_bytes)
			if data is None or not data.lower().startswith(b"http://"):
				return self._image_fallback(alt)
			graphic_id = self._add_graphic(
				bytes((_GEOBENCH_EXTERNAL_GRAPHIC,)) + data + b"\x00"
			)
			if graphic_id is None:
				return self._image_fallback(alt)
			return self._insert_graphic(graphic_id, link_id)
		source_key = None
		if content is None:
			if str(source).startswith("data:"):
				content = _data_uri(str(source), self.limits.max_image_source_bytes)
				if content is not None:
					source_key = "data:" + hashlib.sha256(content).hexdigest()
			else:
				target = _absolute_http_url(self.base_url, source)
				if target is not None:
					source_key = target
					variant_key = (source_key, max_width, max_height)
					if variant_key in self._image_ids:
						graphic_id = self._image_ids[variant_key]
						if graphic_id is None:
							return self._image_fallback(alt)
						return self._insert_graphic(graphic_id, link_id)
					if source_key in self._image_contents:
						content = self._image_contents[source_key]
					else:
						if len(self.graphics) >= self.limits.max_graphics:
							return self._image_fallback(alt)
						if self._image_fetches >= self.limits.max_graphics:
							return self._image_fallback(alt)
						self._image_fetches += 1
						try:
							content = self.image_fetcher(target) if self.image_fetcher else None
						except Exception as error:
							LOGGER.warning("Could not fetch DOX image %s: %s", target, error)
							content = None
						if (
							content is not None
							and len(content) <= self.limits.max_image_source_bytes
							and self._image_content_bytes + len(content)
							<= DOX_MAX_IMAGE_SOURCE_CACHE_BYTES
						):
							self._image_contents[source_key] = content
							self._image_content_bytes += len(content)
						elif content is None or len(content) > self.limits.max_image_source_bytes:
							self._image_contents[source_key] = None
		if content is None or len(content) > self.limits.max_image_source_bytes:
			return self._image_fallback(alt)
		if source_key is None:
			source_key = "content:" + hashlib.sha256(content).hexdigest()
		variant_key = (source_key, max_width, max_height)
		if variant_key in self._image_ids:
			graphic_id = self._image_ids[variant_key]
			if graphic_id is None:
				return self._image_fallback(alt)
			return self._insert_graphic(graphic_id, link_id)
		if len(self.graphics) >= self.limits.max_graphics:
			return self._image_fallback(alt)
		if self._image_conversions >= self.limits.max_graphics:
			return self._image_fallback(alt)
		self._image_conversions += 1
		try:
			if isinstance(self.profile, GbpcProfile):
				graphic = convert_to_gbpc(
					content,
					mode=self.profile.mode,
					max_width=max_width,
					max_height=max_height,
					dithering=self.dithering,
					max_image_pixels=self.limits.max_image_pixels,
					svg_timeout=self.svg_timeout,
					max_intermediate_bytes=self.limits.max_image_source_bytes,
				)
			else:
				graphic = convert_to_sgx(
					content,
					mode=self.profile.mode,
					colours=self.profile.colours,
					max_width=max_width,
					max_height=max_height,
					dithering=self.dithering,
					max_image_pixels=self.limits.max_image_pixels,
					svg_timeout=self.svg_timeout,
					max_intermediate_bytes=self.limits.max_image_source_bytes,
				)
		except Exception as error:
			LOGGER.warning("Could not convert DOX image %s: %s", source_key, error)
			graphic = None
		graphic_id = None
		if graphic is not None:
			graphic_id = self._add_graphic(graphic)
		if graphic_id is None:
			self._image_ids[variant_key] = None
			return self._image_fallback(alt)
		self._image_ids[variant_key] = graphic_id
		return self._insert_graphic(graphic_id, link_id)

	@staticmethod
	def _intern_control_string(strings, string_ids, payload):
		string_id = string_ids.get(payload)
		if string_id is None:
			strings.append(payload)
			string_id = len(strings)
			string_ids[payload] = string_id
		return string_id

	@staticmethod
	def _serialize_ctrl_payload(controls, strings):
		control_section = bytes((len(controls),))
		control_section += b"".join(struct.pack("<H", len(item)) for item in controls)
		control_section += b"".join(controls)
		string_section = b"".join(
			struct.pack("<H", len(item) + 2) + item for item in strings
		) + b"\x00\x00"
		if len(control_section) > 0xffff or len(string_section) > 0xffff:
			raise DoxError("CTRL section exceeds its 16-bit length field")
		return (
			struct.pack("<HH", len(control_section), len(string_section))
			+ control_section + string_section
		)

	def _register_form(self, form):
		method = str(form.get("method", "get")).strip().lower()
		if method != "get":
			return
		action_value = str(form.get("action", "")).strip() or self.base_url
		action = _absolute_http_url(self.base_url, action_value)
		if action is None:
			return
		action = urlparse(action)._replace(fragment="").geturl()

		planned = []
		static_fields = []
		unsupported = False
		for element in form.find_all(("input", "button", "textarea", "select")):
			if element.find_parent("form") is not form or element.has_attr("disabled"):
				continue
			parent = element.parent
			removed_ancestor = False
			while parent is not None and parent is not form:
				if isinstance(parent, Tag) and parent.name.lower() in _REMOVED_TAGS:
					removed_ancestor = True
					break
				parent = parent.parent
			if removed_ancestor:
				continue
			name = element.name.lower()
			field_name_value = str(element.get("name", ""))
			if name in ("textarea", "select"):
				if field_name_value:
					unsupported = True
					break
				continue
			control_type = str(element.get("type", "submit" if name == "button" else "text"))
			control_type = control_type.strip().lower()
			if name == "button" and control_type not in ("button", "reset", "submit"):
				control_type = "submit"
			elif name == "input" and not control_type:
				control_type = "text"
			if name == "input" and control_type in ("hidden", "radio", "checkbox"):
				if control_type != "hidden" and not element.has_attr("checked"):
					continue
				if not field_name_value:
					continue
				default = "on" if control_type in ("radio", "checkbox") else ""
				static_fields.append((
					field_name_value, str(element.get("value", default))
				))
				continue
			if name == "input" and control_type in ("text", "search"):
				if not field_name_value:
					continue
				if element.has_attr("readonly"):
					static_fields.append((field_name_value, str(element.get("value", ""))))
					continue
				field_name = _ascii(element.get("name", ""))
				if not field_name or len(field_name) > DOX_MAX_CONTROL_NAME_BYTES:
					unsupported = True
					break
				max_length = DOX_MAX_CONTROL_VALUE_BYTES
				try:
					declared_max_length = int(str(element.get("maxlength", "")).strip())
				except ValueError:
					declared_max_length = None
				if declared_max_length == 0:
					static_fields.append((field_name_value, ""))
					continue
				if declared_max_length is not None and declared_max_length > 0:
					max_length = min(declared_max_length, DOX_MAX_CONTROL_VALUE_BYTES)
				try:
					size = int(str(element.get("size", "20")).strip())
				except ValueError:
					size = 20
				width = max(40, min(160, max(1, size) * 8))
				default = _ascii(element.get("value", ""), limit=max_length)
				planned.append((element, 32, width, field_name, default, max_length))
				continue
			is_submit = (
				(name == "input" and control_type == "submit")
				or (name == "button" and control_type == "submit")
			)
			if is_submit:
				if field_name_value or any(
					element.has_attr(attribute)
					for attribute in ("formaction", "formmethod", "formenctype")
				):
					unsupported = True
					break
				label_value = (
					element.get_text(" ", strip=True) if name == "button"
					else element.get("value", "")
				)
				label = _ascii(label_value or "Submit", limit=DOX_MAX_CONTROL_LABEL_BYTES)
				if not label:
					label = b"Submit"
				width = max(40, min(160, (len(label) + 2) * 8))
				planned.append((element, 16, width, label))
				continue
			if name == "input" and control_type not in ("button", "reset") and field_name_value:
				unsupported = True
				break

		if unsupported or not planned:
			return
		if static_fields:
			query = urlencode(static_fields)
			action += ("&" if urlparse(action).query else "?") + query
		if len(action.encode("utf-8")) > DOX_MAX_FORM_ACTION_BYTES:
			return
		if len(self.links) >= self.limits.max_links:
			return
		if len(self.controls) + len(planned) > self.limits.max_controls:
			return
		marker_bytes = 8 * len(planned)
		if (
			len(self.text) + self._reserved_text_bytes + marker_bytes + len(_TEXT_TRAILER)
			> self.limits.max_text_bytes
		):
			return

		# Build a complete candidate first. Link shortening registers a proxy token,
		# so every locally knowable limit is checked before that side effect occurs.
		action_link_id = len(self.links) + 1
		controls = list(self.controls)
		strings = list(self.control_strings)
		string_ids = dict(self._control_string_ids)
		assignments = []
		for item in planned:
			element, control_type, width = item[:3]
			if control_type == 32:
				field_name, default, max_length = item[3:]
				name_id = self._intern_control_string(
					strings, string_ids, field_name + b"\x00"
				)
				# This record is deliberately never interned: SymZilla edits it in
				# place, so every text control needs an independent mutable buffer.
				value_payload = default + b"\x00" * (max_length + 1 - len(default))
				strings.append(value_payload)
				value_id = len(strings)
				record = bytes((action_link_id, 32, width, 12))
				record += struct.pack("<HHB", name_id, value_id, max_length)
			else:
				label_id = self._intern_control_string(
					strings, string_ids, item[3] + b"\x00"
				)
				record = bytes((action_link_id, 16, width, 12))
				record += struct.pack("<HH", 0xffff, label_id)
			controls.append(record)
			assignments.append((id(element), len(controls)))
		ctrl_payload = self._serialize_ctrl_payload(controls, strings)
		working_bytes = len(ctrl_payload) + len(controls) * DOX_CONTROL_EXTENSION_BYTES
		if working_bytes > self.limits.max_control_bytes:
			return

		link_id = self._add_link(action, unique=True, always_shorten=True)
		if link_id != action_link_id:
			return
		self.controls = controls
		self.control_strings = strings
		self._control_string_ids = string_ids
		self._control_ids.update(assignments)
		self._reserved_text_bytes += marker_bytes

	def _append_form_marker(self, node):
		control_id = self._control_ids.pop(id(node), None)
		if control_id is None:
			return False
		marker = bytes((10, 7, control_id)) + _FORM_MARKER_SUFFIX
		self._reserved_text_bytes -= len(marker)
		if not self._append_control(marker):
			raise DoxError("Reserved form marker no longer fits the TEXT chunk")
		return True

	@staticmethod
	def _unit_table_span(cell, attribute):
		value = cell.get(attribute)
		return value is None or str(value).strip() == "1"

	@staticmethod
	def _legacy_pixel_dimension(node, attribute, maximum):
		value = node.get(attribute)
		if value is None:
			return None
		value = str(value).strip()
		if len(value) > 4 or re.fullmatch(r"[0-9]+", value) is None:
			return None
		value = int(value)
		return value if 1 <= value <= maximum else None

	@staticmethod
	def _legacy_centered(node):
		if node.has_attr("align"):
			return str(node.get("align", "")).strip().lower() == "center"
		return node.find_parent("center") is not None

	@staticmethod
	def _legacy_cell_centered(cell, image):
		if cell.has_attr("align"):
			return str(cell.get("align", "")).strip().lower() == "center"
		parent = image.parent
		while parent is not None and parent is not cell:
			if isinstance(parent, Tag) and parent.name.lower() == "center":
				return True
			parent = parent.parent
		return False

	@staticmethod
	def _image_only_cell(cell):
		images = cell.find_all("img")
		if len(images) != 1:
			return None
		for descendant in cell.descendants:
			if isinstance(descendant, (Comment, Doctype)):
				continue
			if isinstance(descendant, NavigableString):
				if str(descendant).strip():
					return None
				continue
			if (
				not isinstance(descendant, Tag)
				or descendant.name.lower() not in ("a", "center", "img")
			):
				return None
		return images[0]

	def _fitted_table_layout(self, table, rows):
		"""Return strict legacy-pixel geometry for a centered image-only grid."""
		if (
			len(rows[0]) > 4
			or table.has_attr("style")
			or table.find(style=True) is not None
		):
			return None
		table_width = self._legacy_pixel_dimension(
			table, "width", DOX_MAX_DOCUMENT_WIDTH
		)
		if table_width is None or not self._legacy_centered(table):
			return None

		cell_widths = tuple(
			self._legacy_pixel_dimension(cell, "width", DOX_MAX_DOCUMENT_WIDTH)
			for cell in rows[0]
		)
		if None in cell_widths or sum(cell_widths) != table_width:
			return None
		for row in rows[1:]:
			if tuple(
				self._legacy_pixel_dimension(cell, "width", DOX_MAX_DOCUMENT_WIDTH)
				for cell in row
			) != cell_widths:
				return None

		image_bounds = []
		for row in rows:
			bounds = []
			for column, cell in enumerate(row):
				image = self._image_only_cell(cell)
				if (
					image is None or not self._legacy_cell_centered(cell, image)
					or cell.has_attr("style")
					or any(tag.has_attr("style") for tag in cell.find_all(True))
				):
					return None
				width = self._legacy_pixel_dimension(
					image, "width", self.limits.max_image_width
				)
				height = self._legacy_pixel_dimension(
					image, "height", self.limits.max_image_height
				)
				if (
					width is None or height is None or width < 8
					or width > cell_widths[column] - _TABLE_CELL_HORIZONTAL_OVERHEAD
				):
					return None
				bounds.append((width, height))
			image_bounds.append(tuple(bounds))
		return _FittedTableLayout(
			table_width,
			cell_widths,
			tuple(image_bounds),
		)

	def _simple_table_rows(self, table):
		if table.find("table") is not None:
			return None

		rows = []

		def add_row(row):
			cells = []
			for child in row.children:
				if isinstance(child, (Comment, Doctype)):
					continue
				if isinstance(child, NavigableString):
					if str(child).strip():
						return False
					continue
				if not isinstance(child, Tag) or child.name.lower() not in ("td", "th"):
					return False
				if not self._unit_table_span(child, "colspan"):
					return False
				if not self._unit_table_span(child, "rowspan"):
					return False
				if any(
					descendant.name.lower() not in _TABLE_INLINE_TAGS
					for descendant in child.find_all(True)
				):
					return False
				cells.append(child)
			if not cells:
				return False
			rows.append(cells)
			return True

		def add_section(section):
			for child in section.children:
				if isinstance(child, (Comment, Doctype)):
					continue
				if isinstance(child, NavigableString):
					if str(child).strip():
						return False
					continue
				if not isinstance(child, Tag) or child.name.lower() != "tr":
					return False
				if not add_row(child):
					return False
			return True

		for child in table.children:
			if isinstance(child, (Comment, Doctype)):
				continue
			if isinstance(child, NavigableString):
				if str(child).strip():
					return None
				continue
			if not isinstance(child, Tag):
				return None
			name = child.name.lower()
			if name == "tr":
				if not add_row(child):
					return None
			elif name in ("thead", "tbody", "tfoot"):
				if not add_section(child):
					return None
			else:
				return None

		if not rows or len(rows) > self.limits.max_table_rows:
			return None
		column_count = len(rows[0])
		if not 2 <= column_count <= self.limits.max_table_columns:
			return None
		if any(len(row) != column_count for row in rows):
			return None
		if len(rows) * column_count > self.limits.max_table_cells:
			return None
		return rows

	def _table_inline_budget(self, node):
		if isinstance(node, (Comment, Doctype)):
			return 0
		if isinstance(node, NavigableString):
			return len(_ascii(re.sub(r"\s+", " ", str(node))))
		if not isinstance(node, Tag):
			return 0
		name = node.name.lower()
		if name == "br":
			return 2
		if name == "img":
			alt = _ascii(
				node.get("alt", ""), limit=DOX_MAX_TABLE_IMAGE_ALT_BYTES
			)
			return max(8, len(alt) + 2 if alt else 0)
		budget = sum(self._table_inline_budget(child) for child in node.children)
		if name == "a":
			budget += 8
		if name in ("b", "strong", "em", "i", "cite"):
			budget += 6
		return budget

	def _table_budget(self, rows, layout):
		budget = len(_TEXT_FORMAT_RESET) + 2 if self._standard_paragraph_open else 0
		for row in rows:
			budget += 3
			for cell in row:
				budget += _TABLE_COLUMN_HEADER_BYTES + 1
				if layout is not None:
					budget += len(_TABLE_CENTER_FORMAT)
				budget += self._table_inline_budget(cell)
				if cell.name.lower() == "th":
					budget += 6
		return budget

	def _render_table_cell(
		self,
		cell,
		column_count,
		*,
		image_bounds=None,
		center=False,
	):
		text = self.text
		paragraph_open = self._standard_paragraph_open
		table_image_width = self._table_image_width
		table_image_height = self._table_image_height
		self.text = bytearray()
		self._standard_paragraph_open = False
		if image_bounds is None:
			# RENILN does not clip an oversized first inline object. Size responsive
			# tables against the minimum render width so narrowing stays safe.
			self._table_image_width = min(
				self.limits.max_image_width,
				DOX_MIN_DOCUMENT_WIDTH // column_count
				- _TABLE_CELL_HORIZONTAL_OVERHEAD,
			)
			self._table_image_height = None
		else:
			self._table_image_width = image_bounds[0]
			self._table_image_height = image_bounds[1]
		try:
			if center:
				self._append_control(_TABLE_CENTER_FORMAT)
			if cell.name.lower() == "th":
				self._append_control(b"\x02\x03\x01")
			self._render_children(cell, skip_blank=center)
			if cell.name.lower() == "th":
				self._append_control(b"\x02\x01\x01")
			return bytes(self.text)
		finally:
			self.text = text
			self._standard_paragraph_open = paragraph_open
			self._table_image_width = table_image_width
			self._table_image_height = table_image_height

	def _render_table(self, table):
		rows = self._simple_table_rows(table)
		if rows is None:
			return False
		column_count = len(rows[0])
		layout = self._fitted_table_layout(table, rows)
		if (
			self._table_rows + len(rows) > self.limits.max_table_rows
			or self._table_cells + len(rows) * column_count > self.limits.max_table_cells
		):
			return False
		budget = self._table_budget(rows, layout)
		if (
			len(self.text) + self._reserved_text_bytes + budget + len(_TEXT_TRAILER)
			> self.limits.max_text_bytes
		):
			return False

		encoded_rows = []
		for row_number, cells in enumerate(rows):
			row = bytearray((0xff, 0x10 | column_count))
			for column, cell in enumerate(cells):
				start = len(row)
				if layout is None:
					row.extend(_table_column_header(column, column_count))
				else:
					row.extend(_fitted_table_column_header(
						column, layout.cell_widths
					))
				row.extend(self._render_table_cell(
					cell,
					column_count,
					image_bounds=(
						None if layout is None
						else layout.image_bounds[row_number][column]
					),
					center=layout is not None,
				))
				row.append(0)
				delta = len(row) - start
				if not _TABLE_COLUMN_HEADER_BYTES + 1 <= delta <= 0xffff:
					raise DoxError("DOX table cell offset exceeds its 16-bit field")
				struct.pack_into("<H", row, start + 1, delta)
			row.append(0)
			encoded_rows.append(bytes(row))

		prefix = (
			_TEXT_FORMAT_RESET + b"\x00\x00"
			if self._standard_paragraph_open else b""
		)
		encoded = prefix + b"".join(encoded_rows)
		if len(encoded) > budget:
			raise DoxError("DOX table exceeded its preflight text budget")
		self.text.extend(encoded)
		self._standard_paragraph_open = False
		self._table_rows += len(rows)
		self._table_cells += len(rows) * column_count
		if layout is not None:
			self._document_min_width = max(
				self._document_min_width, layout.table_width
			)
		return True

	def render_html(self, document):
		if isinstance(document, bytes):
			document = document.decode("utf-8", errors="replace")
		soup = BeautifulSoup(document, "html5lib")
		for tag in list(soup.find_all(_REMOVED_TAGS)):
			tag.decompose()
		title_tag = soup.find("title")
		title = title_tag.get_text(" ", strip=True) if title_tag else urlparse(self.base_url).netloc
		if soup.find("a", href=True) is not None:
			# Reserve the shared clickable-link glyph before page images consume
			# the bounded graphic table.
			self._ensure_link_icon()
		root = soup.body or soup
		self._render_children(root)
		return title or "SymZilla document"

	def _render_children(
		self,
		node,
		*,
		preserve=False,
		link_id=None,
		skip_blank=False,
	):
		for child in list(node.children):
			self._render_node(
				child,
				preserve=preserve,
				link_id=link_id,
				skip_blank=skip_blank,
			)

	def _render_node(
		self,
		node,
		*,
		preserve=False,
		link_id=None,
		skip_blank=False,
	):
		if isinstance(node, (Comment, Doctype)):
			return
		if isinstance(node, NavigableString):
			if skip_blank and not str(node).strip():
				return
			self.append_text(node, preserve=preserve)
			return
		if not isinstance(node, Tag):
			return
		name = node.name.lower()
		if name in _REMOVED_TAGS:
			return
		if name == "table":
			if not self._table_fallback_depth and self._render_table(node):
				return
			self.line_break()
			self._table_fallback_depth += 1
			try:
				self._render_children(
					node,
					preserve=preserve,
					link_id=link_id,
					skip_blank=skip_blank,
				)
			finally:
				self._table_fallback_depth -= 1
			self.line_break()
			return
		if name in ("input", "button", "textarea", "select"):
			self._append_form_marker(node)
			return
		if name == "br":
			self.line_break()
			return
		if name == "hr":
			self.line_break()
			self.append_text("--------------------------------")
			self.line_break()
			return
		if name == "img":
			source = self._image_source(node)
			if source:
				self.append_image(source, node.get("alt", ""), link_id=link_id)
			return
		if name == "a":
			link_id = self._add_link(node.get("href"))
			linked_before = self._linked_graphic_insertions
			# SymbOS 4.1 changed control 3 from a one-byte underline toggle
			# into a two-byte formatting command.  The clickable link graphic
			# is portable across releases, so keep the label as plain text.
			self._render_link_children(
				node,
				link_id,
				preserve=preserve,
				skip_blank=skip_blank,
			)
			if link_id is not None:
				if self._linked_graphic_insertions == linked_before:
					self.append_link_icon(link_id)
			return

		is_block = name in _BLOCK_TAGS
		if is_block:
			self.line_break()
		if name == "form":
			self._register_form(node)
		if name == "li":
			self.append_text("- ")
		font = None
		if name in ("h1", "h2"):
			font = 4
		elif name in ("h3", "h4", "h5", "h6", "b", "strong", "th", "dt"):
			font = 3
		elif name in ("em", "i", "cite"):
			font = 2
		if font is not None:
			self._append_control(bytes((2, font, 1)))
		self._render_children(
			node,
			preserve=preserve or name in ("pre", "xmp"),
			link_id=link_id,
			skip_blank=skip_blank,
		)
		if font is not None:
			self._append_control(b"\x02\x01\x01")
		if is_block:
			self.line_break()

	def _render_link_children(
		self,
		node,
		link_id,
		*,
		preserve=False,
		skip_blank=False,
	):
		for child in list(node.children):
			self._render_node(
				child,
				preserve=preserve,
				link_id=link_id,
				skip_blank=skip_blank,
			)

	def serialize(self, title):
		if self._reserved_text_bytes or self._control_ids:
			raise DoxError("Form controls were registered but not rendered")
		text = bytes(self.text) + _TEXT_TRAILER
		info_values = (title, "GB-proxy", "SymbOS", "1", "", "Web page", "Internet")
		info = b"".join(_ascii(value)[:63] + b"\x00" for value in info_values)[:255]
		if not info.endswith(b"\x00"):
			info = info[:-1] + b"\x00"
		head = struct.pack(
			"<HHBB", self._document_min_width, DOX_MAX_DOCUMENT_WIDTH, 0, 2
		)
		graphics = bytes((len(self.graphics),))
		graphics += b"".join(struct.pack("<H", len(item)) for item in self.graphics)
		graphics += b"".join(self.graphics)
		links = bytes((len(self.links),))
		links += b"".join(struct.pack("<H", len(item)) for item in self.links)
		links += b"".join(self.links)
		chunks = [
			_chunk(b"INFO", info),
			_chunk(b"HEAD", head),
			_chunk(b"TEXT", text),
			_chunk(b"GRPH", graphics),
			_chunk(b"LINK", links),
		]
		if self.controls:
			chunks.append(_chunk(
				b"CTRL", self._serialize_ctrl_payload(self.controls, self.control_strings)
			))
		chunks.append(_chunk(b"ENDF", b""))
		document = b"".join(chunks)
		if len(document) > self.limits.max_document_bytes:
			raise DoxError("DOX document exceeds its configured size limit")
		validate_dox(document, limits=self.limits, profile=self.profile)
		return document


def build_dox_from_html(
	document,
	base_url,
	*,
	profile=SAFE_SGX_PROFILE,
	limits=None,
	image_fetcher=None,
	image_shortener=None,
	link_shortener=None,
	dithering="FLOYDSTEINBERG",
	svg_timeout=10,
):
	"""Convert an HTML document into the bounded DOX subset SymZilla loads."""
	limits = limits or DoxLimits()
	builder = _DoxBuilder(
		base_url,
		profile,
		limits,
		image_fetcher,
		image_shortener=image_shortener,
		link_shortener=link_shortener,
		dithering=dithering,
		svg_timeout=svg_timeout,
	)
	title = builder.render_html(document)
	return builder.serialize(title)


def build_dox_from_image(
	content,
	base_url,
	*,
	profile=SAFE_SGX_PROFILE,
	limits=None,
	image_shortener=None,
	dithering="FLOYDSTEINBERG",
	svg_timeout=10,
):
	"""Wrap one directly requested image in a complete one-image DOX file."""
	limits = limits or DoxLimits()
	builder = _DoxBuilder(
		base_url,
		profile,
		limits,
		None,
		image_shortener=image_shortener,
		link_shortener=None,
		dithering=dithering,
		svg_timeout=svg_timeout,
	)
	title = urlparse(base_url).path.rsplit("/", 1)[-1] or "Image"
	if not builder.append_image(base_url, title, content=content):
		builder.append_text("Image could not be converted")
	return builder.serialize(title)


def _parse_chunks(document, max_document_bytes):
	if not isinstance(document, bytes):
		raise DoxValidationError("DOX input must be bytes")
	if len(document) > max_document_bytes:
		raise DoxValidationError("DOX document exceeds its size limit")
	chunks = {}
	offset = 0
	while offset < len(document):
		if len(document) - offset < 8:
			raise DoxValidationError("Truncated DOX chunk header")
		name = document[offset:offset + 4]
		length = struct.unpack_from("<I", document, offset + 4)[0]
		end = offset + 8 + length
		if name not in _CHUNK_NAMES:
			raise DoxValidationError(f"Unsupported DOX chunk {name!r}")
		if name in chunks:
			raise DoxValidationError(f"Duplicate DOX chunk {name.decode('ascii')}")
		if end > len(document):
			raise DoxValidationError("DOX chunk extends past end of file")
		chunks[name] = document[offset + 8:end]
		offset = end
		if name == b"ENDF" and offset != len(document):
			raise DoxValidationError("ENDF must be the final DOX chunk")
	if not _REQUIRED_CHUNKS.issubset(chunks) or list(chunks)[-1:] != [b"ENDF"]:
		raise DoxValidationError("DOX is missing a required chunk")
	if chunks[b"ENDF"]:
		raise DoxValidationError("ENDF must be empty")
	return chunks


def _split_counted_records(payload, *, maximum, label):
	if not payload:
		raise DoxValidationError(f"Empty {label} chunk")
	count = payload[0]
	if count > maximum:
		raise DoxValidationError(f"Too many {label} records")
	table_end = 1 + count * 2
	if table_end > len(payload):
		raise DoxValidationError(f"Truncated {label} length table")
	lengths = struct.unpack_from(f"<{count}H", payload, 1) if count else ()
	offset = table_end
	records = []
	for length in lengths:
		end = offset + length
		if end > len(payload):
			raise DoxValidationError(f"Truncated {label} record")
		records.append(payload[offset:end])
		offset = end
	if offset != len(payload):
		raise DoxValidationError(f"Trailing bytes in {label} chunk")
	return records


def _split_ctrl_strings(payload):
	if len(payload) < 2:
		raise DoxValidationError("CTRL string section is missing its terminator")
	strings = []
	offset = 0
	while offset < len(payload):
		if offset + 2 > len(payload):
			raise DoxValidationError("Truncated CTRL string length")
		length = struct.unpack_from("<H", payload, offset)[0]
		if length == 0:
			if offset + 2 != len(payload):
				raise DoxValidationError("CTRL string terminator is not final")
			return strings
		if length < 3:
			raise DoxValidationError("CTRL string record is too short")
		end = offset + length
		if end > len(payload):
			raise DoxValidationError("Truncated CTRL string record")
		value = payload[offset + 2:end]
		terminator = value.find(b"\x00")
		if terminator < 0:
			raise DoxValidationError("CTRL string is not NUL-terminated")
		if any(byte < 32 or byte > 126 for byte in value[:terminator]):
			raise DoxValidationError("CTRL string is not printable ASCII")
		if any(value[terminator + 1:]):
			raise DoxValidationError("CTRL string has non-zero padding")
		strings.append(value)
		offset = end
	raise DoxValidationError("CTRL string section is missing its terminator")


def _scan_text_body(text, start, terminator=None):
	"""Scan one standard or column body without mistaking control data for NUL."""
	marker_ids = []
	offset = start
	limit = len(text) if terminator is None else terminator + 1
	while offset < limit:
		value = text[offset]
		if value == 0:
			if terminator is not None and offset != terminator:
				raise DoxValidationError("DOX table cell terminates before its column jump")
			return offset, marker_ids
		if value >= 12:
			offset += 1
			continue
		if value == 1:
			length = 2
		elif value == 2:
			length = 3
		elif value in (3, 4, 6, 7):
			length = 1
		elif value == 5:
			length = 2
		elif 8 <= value <= 11:
			length = 2 * (value - 7)
		else:
			raise DoxValidationError("Invalid control byte in DOX TEXT")
		end = offset + length
		if end > limit:
			raise DoxValidationError("Truncated control code in DOX TEXT")
		if value == 10 and text[offset + 1] == 7:
			end = offset + 8
			if end > limit or text[offset + 3:end] != _FORM_MARKER_SUFFIX:
				raise DoxValidationError("Malformed CTRL marker in TEXT")
			marker_ids.append(text[offset + 2])
		offset = end
	if terminator is not None:
		raise DoxValidationError("DOX table column jump does not land after a cell terminator")
	raise DoxValidationError("DOX paragraph is missing its text terminator")


def _validate_text(text, limits, minimum_width, maximum_width):
	marker_ids = []
	offset = 0
	table_rows = 0
	table_cells = 0
	while offset < len(text):
		if text[offset] != 0xff:
			terminator, found = _scan_text_body(text, offset)
			marker_ids.extend(found)
			follow = terminator + 1
		else:
			if offset + 2 > len(text):
				raise DoxValidationError("Truncated DOX table paragraph header")
			flags_count = text[offset + 1]
			column_count = flags_count & 15
			if (
				flags_count != (0x10 | column_count)
				or not 2 <= column_count <= limits.max_table_columns
			):
				raise DoxValidationError("Invalid DOX table column count or flags")
			table_rows += 1
			table_cells += column_count
			if table_rows > limits.max_table_rows or table_cells > limits.max_table_cells:
				raise DoxValidationError("DOX table exceeds its configured row or cell limit")
			column = offset + 2
			headers = []
			for number in range(column_count):
				if column + _TABLE_COLUMN_HEADER_BYTES > len(text):
					raise DoxValidationError("Truncated DOX table column header")
				header = text[column:column + _TABLE_COLUMN_HEADER_BYTES]
				if header[0] != _TABLE_COLUMN_HEADER_BYTES:
					raise DoxValidationError("Invalid DOX table column header length")
				headers.append(header)
				delta = struct.unpack_from("<H", header, 1)[0]
				if delta < _TABLE_COLUMN_HEADER_BYTES + 1:
					raise DoxValidationError("DOX table column jump points inside its header")
				target = column + delta
				if target >= len(text):
					raise DoxValidationError("DOX table column jump extends past TEXT")
				terminator = target - 1
				_, found = _scan_text_body(
					text, column + _TABLE_COLUMN_HEADER_BYTES, terminator
				)
				marker_ids.extend(found)
				column = target
				if number + 1 < column_count and text[column] != _TABLE_COLUMN_HEADER_BYTES:
					raise DoxValidationError("DOX table column jump misses the next header")
			_validate_table_geometry(
				headers, column_count, minimum_width, maximum_width
			)
			follow = column

		if follow >= len(text) or text[follow] not in (0, 0xff):
			raise DoxValidationError("Invalid DOX paragraph continuation marker")
		if text[follow] == 0xff:
			if follow + 1 != len(text):
				raise DoxValidationError("DOX end marker must be final")
			return marker_ids
		offset = follow + 1
	raise DoxValidationError("DOX TEXT is missing its final marker")


def _validate_ctrl(payload, links, marker_ids, limits):
	if len(payload) > limits.max_control_bytes:
		raise DoxValidationError("CTRL working allocation exceeds its configured size limit")
	if len(payload) < 4:
		raise DoxValidationError("CTRL chunk is missing its section lengths")
	control_length, string_length = struct.unpack_from("<HH", payload)
	if control_length < 1 or string_length < 2:
		raise DoxValidationError("CTRL section is shorter than its canonical minimum")
	if 4 + control_length + string_length != len(payload):
		raise DoxValidationError("CTRL section lengths do not match its payload")
	control_section = payload[4:4 + control_length]
	string_section = payload[4 + control_length:]
	controls = _split_counted_records(
		control_section, maximum=limits.max_controls, label="control"
	)
	if (
		len(payload) + len(controls) * DOX_CONTROL_EXTENSION_BYTES
		> limits.max_control_bytes
	):
		raise DoxValidationError("CTRL working allocation exceeds its configured size limit")
	strings = _split_ctrl_strings(string_section)

	def string_value(string_id, label):
		if string_id == 0 or string_id > len(strings):
			raise DoxValidationError(f"Invalid CTRL {label} string reference")
		value = strings[string_id - 1]
		return value, value.find(b"\x00")

	normal_string_ids = set()
	mutable_string_ids = set()
	for control in controls:
		if len(control) < 2:
			raise DoxValidationError("CTRL record is missing its common prefix")
		action_link_id, control_type = control[:2]
		if action_link_id == 0 or action_link_id > len(links):
			raise DoxValidationError("CTRL action link is out of range")
		if links[action_link_id - 1][0] != 0:
			raise DoxValidationError("CTRL actions must use GET links")
		if control_type == 32:
			if len(control) != 9:
				raise DoxValidationError("Text CTRL records must contain exactly nine bytes")
			width, height = control[2:4]
			name_id, value_id, max_length = struct.unpack_from("<HHB", control, 4)
			if not 40 <= width <= 160 or height != 12 or not 1 <= max_length <= 63:
				raise DoxValidationError("Invalid text CTRL dimensions or maximum length")
			name, name_end = string_value(name_id, "name")
			value, value_end = string_value(value_id, "value")
			if not 1 <= name_end <= DOX_MAX_CONTROL_NAME_BYTES:
				raise DoxValidationError("Invalid text CTRL name")
			if name_end != len(name) - 1:
				raise DoxValidationError("Text CTRL names cannot use padded strings")
			if len(value) != max_length + 1 or value_end > max_length:
				raise DoxValidationError("Text CTRL value buffer has the wrong capacity")
			if value_id in mutable_string_ids:
				raise DoxValidationError("Text CTRL value buffers must not be shared")
			normal_string_ids.add(name_id)
			mutable_string_ids.add(value_id)
		elif control_type == 16:
			if len(control) != 8:
				raise DoxValidationError("Button CTRL records must contain exactly eight bytes")
			width, height = control[2:4]
			name_id, label_id = struct.unpack_from("<HH", control, 4)
			if not 40 <= width <= 160 or height != 12 or name_id != 0xffff:
				raise DoxValidationError("Invalid button CTRL dimensions or name")
			label, label_end = string_value(label_id, "label")
			if not 1 <= label_end <= DOX_MAX_CONTROL_LABEL_BYTES:
				raise DoxValidationError("Invalid button CTRL label")
			if label_end != len(label) - 1:
				raise DoxValidationError("Button CTRL labels cannot use padded strings")
			normal_string_ids.add(label_id)
		else:
			raise DoxValidationError("Unsupported CTRL record type")
	if normal_string_ids & mutable_string_ids:
		raise DoxValidationError("Mutable CTRL value buffers cannot be reused")

	if marker_ids != list(range(1, len(controls) + 1)):
		raise DoxValidationError("TEXT CTRL markers do not match the CTRL records")


def validate_dox(document, *, limits=None, profile=None):
	"""Validate and return parsed chunks for GB-proxy's supported DOX subset."""
	limits = limits or DoxLimits()
	profile = profile or SAFE_SGX_PROFILE
	chunks = _parse_chunks(document, limits.max_document_bytes)
	if not chunks[b"INFO"] or len(chunks[b"INFO"]) > 255 or b"\x00" not in chunks[b"INFO"]:
		raise DoxValidationError("Invalid INFO chunk")
	if len(chunks[b"HEAD"]) != 6:
		raise DoxValidationError("HEAD must contain exactly six bytes")
	minimum_width, maximum_width, reserved, version = struct.unpack(
		"<HHBB", chunks[b"HEAD"]
	)
	if (
		not DOX_MIN_DOCUMENT_WIDTH <= minimum_width <= maximum_width
		or maximum_width != DOX_MAX_DOCUMENT_WIDTH
		or reserved != 0 or version != 2
	):
		raise DoxValidationError("Invalid DOX document width or HEAD format")
	text = chunks[b"TEXT"]
	if len(text) > limits.max_text_bytes or not text.endswith(b"\x00\xff"):
		raise DoxValidationError("Invalid or oversized TEXT chunk")
	marker_ids = _validate_text(text, limits, minimum_width, maximum_width)

	graphics = _split_counted_records(
		chunks[b"GRPH"], maximum=limits.max_graphics, label="graphic"
	)
	graphics_bytes = 0
	for graphic in graphics:
		if isinstance(profile, GbpcProfile):
			if graphic[:1] == bytes((_GEOBENCH_EXTERNAL_GRAPHIC,)):
				if (
					len(graphic) < 10
					or graphic[-1] != 0
					or _direct_url_bytes(
						graphic[1:-1].decode("ascii", errors="ignore"),
						limits.max_url_bytes,
					) != graphic[1:-1]
					or not graphic[1:-1].lower().startswith(b"http://")
				):
					raise DoxValidationError("Invalid external GEOBENCH graphic URL")
				graphics_bytes += len(graphic)
				continue
			if len(graphic) < 14 or graphic[:4] != b"GBPC" or graphic[4] != 2:
				raise DoxValidationError("Invalid GEOBENCH graphic record")
			mode = graphic[5]
			width, height = struct.unpack_from("<HH", graphic, 6)
			if mode != profile.mode:
				raise DoxValidationError("GBPC graphic does not match the requested mode")
			row_bytes = width // (4 if mode == GBPC_MODE_1 else 2)
			if (
				mode not in (GBPC_MODE_1, GBPC_MODE_7)
				or width < 1 or height < 1 or width % 4
				or width > limits.max_image_width or height > limits.max_image_height
				or len(graphic) != 14 + row_bytes * height
				or len(graphic) > DOX_MAX_GRAPHIC_ENTRY_BYTES
			):
				raise DoxValidationError("Invalid GBPC dimensions or payload length")
		else:
			if len(graphic) < 8 or graphic[0] != 0x40 or graphic[1] not in (0, 5):
				raise DoxValidationError("Invalid extended SGX graphic")
			width_bytes, width, height = struct.unpack_from("<HHH", graphic, 2)
			multiple = 8 if graphic[1] == 0 else 4
			expected_width_bytes = width // (4 if graphic[1] == 0 else 2)
			if (
				width < 1 or height < 1 or width > 255 or height > 255
				or width % multiple or width_bytes != expected_width_bytes
				or width_bytes % 2
				or len(graphic) != 8 + width_bytes * height
				or len(graphic) > DOX_MAX_GRAPHIC_ENTRY_BYTES
			):
				raise DoxValidationError("Invalid SGX dimensions or payload length")
		graphics_bytes += len(graphic)
	if graphics_bytes > limits.max_graphics_bytes:
		raise DoxValidationError("Graphics exceed their aggregate size limit")

	links = _split_counted_records(
		chunks[b"LINK"], maximum=limits.max_links, label="link"
	)
	for link in links:
		if (
			len(link) < 3 or len(link) > limits.max_url_bytes + 2
			or link[0] not in (0, 1) or link[-1] != 0
			or b"\x00" in link[1:-1]
		):
			raise DoxValidationError("Invalid LINK record")
	if b"CTRL" in chunks:
		_validate_ctrl(chunks[b"CTRL"], links, marker_ids, limits)
	elif marker_ids:
		raise DoxValidationError("TEXT contains CTRL markers without a CTRL chunk")
	return chunks
