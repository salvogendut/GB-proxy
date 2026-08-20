"""Bounded HTML/image to SymZilla DOX conversion and validation helpers."""

import base64
import binascii
import hashlib
import logging
import re
import struct
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag

from utils.image_utils import (
	SGX_MODE_0,
	SGX_MODE_5,
	convert_to_sgx,
	encode_sgx_pixels,
)


LOGGER = logging.getLogger(__name__)
DOX_MIMETYPE = "application/x-symbos-dox"
DOX_MAX_GRAPHIC_ENTRY_BYTES = 16382
DOX_MAX_DOCUMENT_BYTES = 96 * 1024
DOX_MAX_TEXT_BYTES = 11764
DOX_DOCUMENT_OVERHEAD_RESERVE = 192
_TEXT_TRAILER = b"\x04\x02\x01\x01\x00\xff"
_CHUNK_NAMES = (b"INFO", b"HEAD", b"TEXT", b"GRPH", b"LINK", b"ENDF")
_REQUIRED_CHUNKS = frozenset(_CHUNK_NAMES)
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
	max_document_bytes: int = 96 * 1024
	max_image_width: int = 160
	max_image_height: int = 96
	max_image_source_bytes: int = 2 * 1024 * 1024
	max_image_pixels: int = 16 * 1024 * 1024
	max_url_bytes: int = 127

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
		if self.max_graphics > 127:
			raise ValueError("SymZilla safely supports at most 127 graphics")
		if self.max_graphics_bytes < 40:
			raise ValueError("SymZilla graphics limit must fit its SGX5 link icon")
		if not 8 <= self.max_image_width <= 248 or self.max_image_height > 255:
			raise ValueError("SymZilla images must fit its byte-sized dimensions")
		aligned_width = (self.max_image_width // 4) * 4
		if aligned_width // 2 * self.max_image_height > DOX_MAX_GRAPHIC_ENTRY_BYTES - 8:
			raise ValueError("SymZilla SGX5 images must fit one 16K memory area")
		if self.max_url_bytes > 127:
			raise ValueError("SymZilla history holds at most 127 URL bytes")
		if self.max_document_bytes < self.max_text_bytes + DOX_DOCUMENT_OVERHEAD_RESERVE:
			raise ValueError("SymZilla document limit is too small for its TEXT limit")
		if self.max_document_bytes > DOX_MAX_DOCUMENT_BYTES:
			raise ValueError("SymZilla accepts DOX documents up to 96 KiB")


def _chunk(name, payload):
	if name not in _CHUNK_NAMES:
		raise ValueError(f"Unsupported DOX chunk {name!r}")
	return name + struct.pack("<I", len(payload)) + payload


def _ascii(value, *, limit=None):
	value = unicodedata.normalize("NFKD", str(value))
	value = value.encode("ascii", errors="ignore").decode("ascii")
	value = "".join(
		character if 32 <= ord(character) <= 126 else " " for character in value
	)
	data = value.encode("ascii")
	return data if limit is None else data[:limit]


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
	return encode_sgx_pixels(
		pixels,
		mode=profile.mode,
		colours=profile.colours,
	)


class _DoxBuilder:
	def __init__(
		self,
		base_url,
		profile,
		limits,
		image_fetcher,
		*,
		link_shortener,
		dithering,
		svg_timeout,
	):
		self.base_url = base_url
		self.profile = profile
		self.limits = limits
		self.image_fetcher = image_fetcher
		self.link_shortener = link_shortener
		self.dithering = dithering
		self.svg_timeout = svg_timeout
		self.text = bytearray()
		self.graphics = []
		self.links = []
		self._link_ids = {}
		self._link_targets = {}
		self._image_ids = {}
		self._graphics_bytes = 0
		self._link_icon_id = None
		self._linked_graphic_insertions = 0
		self._image_fetches = 0
		self._image_conversions = 0

	def _append_control(self, data):
		if len(self.text) + len(data) + len(_TEXT_TRAILER) > self.limits.max_text_bytes:
			return False
		self.text.extend(data)
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
		if value.startswith(" ") and (not self.text or self.text[-1] in (3, 32)):
			value = value.lstrip(" ")
		self._append_ascii(value)

	def _append_ascii(self, value):
		data = _ascii(value)
		remaining = self.limits.max_text_bytes - len(self.text) - len(_TEXT_TRAILER)
		if remaining > 0:
			self.text.extend(data[:remaining])

	def line_break(self):
		if self.text.endswith(b"\x08\x03"):
			return
		self._append_control(b"\x08\x03")

	def _add_graphic(self, graphic):
		if len(self.graphics) >= self.limits.max_graphics:
			return None
		if len(graphic) > DOX_MAX_GRAPHIC_ENTRY_BYTES:
			return None
		if self._graphics_bytes + len(graphic) > self.limits.max_graphics_bytes:
			return None
		self.graphics.append(graphic)
		self._graphics_bytes += len(graphic)
		return len(self.graphics)

	def _add_link(self, value):
		url = _absolute_http_url(self.base_url, value)
		if url is None:
			return None
		if url in self._link_targets:
			return self._link_targets[url]
		# Shortening registers a proxy-local token. Check the document limit
		# first so links we cannot emit do not evict tokens that we did emit.
		if len(self.links) >= self.limits.max_links:
			return None
		target = url
		if self.link_shortener is not None:
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
		if url in self._link_ids:
			link_id = self._link_ids[url]
			self._link_targets[target] = link_id
			return link_id
		entry = b"\x00" + data + b"\x00"
		self.links.append(entry)
		link_id = len(self.links)
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

	def append_image(self, source, alt="", link_id=None, content=None):
		key = None
		if content is None:
			if str(source).startswith("data:"):
				content = _data_uri(str(source), self.limits.max_image_source_bytes)
				if content is not None:
					key = "data:" + hashlib.sha256(content).hexdigest()
			else:
				target = _absolute_http_url(self.base_url, source)
				if target is not None:
					target = urljoin(self.base_url, str(source).strip())
					key = target
					graphic_id = self._image_ids.get(key)
					if graphic_id is not None:
						return self._insert_graphic(graphic_id, link_id)
					if len(self.graphics) >= self.limits.max_graphics:
						if alt:
							self.append_text(f"[{alt}]")
						return False
					if self._image_fetches >= self.limits.max_graphics:
						if alt:
							self.append_text(f"[{alt}]")
						return False
					self._image_fetches += 1
					try:
						content = self.image_fetcher(target) if self.image_fetcher else None
					except Exception as error:
						LOGGER.warning("Could not fetch DOX image %s: %s", target, error)
						content = None
		if content is None or len(content) > self.limits.max_image_source_bytes:
			if alt:
				self.append_text(f"[{alt}]")
			return False
		if key is None:
			key = "content:" + hashlib.sha256(content).hexdigest()
		graphic_id = self._image_ids.get(key)
		if graphic_id is not None:
			return self._insert_graphic(graphic_id, link_id)
		if len(self.graphics) >= self.limits.max_graphics:
			if alt:
				self.append_text(f"[{alt}]")
			return False
		if self._image_conversions >= self.limits.max_graphics:
			if alt:
				self.append_text(f"[{alt}]")
			return False
		self._image_conversions += 1
		try:
			graphic = convert_to_sgx(
				content,
				mode=self.profile.mode,
				colours=self.profile.colours,
				max_width=self.limits.max_image_width,
				max_height=self.limits.max_image_height,
				dithering=self.dithering,
				max_image_pixels=self.limits.max_image_pixels,
				svg_timeout=self.svg_timeout,
				max_intermediate_bytes=self.limits.max_image_source_bytes,
			)
		except Exception as error:
			LOGGER.warning("Could not convert DOX image %s: %s", key, error)
			graphic = None
		if graphic is not None:
			graphic_id = self._add_graphic(graphic)
		if graphic_id is None:
			if alt:
				self.append_text(f"[{alt}]")
			return False
		self._image_ids[key] = graphic_id
		return self._insert_graphic(graphic_id, link_id)

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

	def _render_children(self, node, *, preserve=False, link_id=None):
		for child in list(node.children):
			self._render_node(child, preserve=preserve, link_id=link_id)

	def _render_node(self, node, *, preserve=False, link_id=None):
		if isinstance(node, (Comment, Doctype)):
			return
		if isinstance(node, NavigableString):
			self.append_text(node, preserve=preserve)
			return
		if not isinstance(node, Tag):
			return
		name = node.name.lower()
		if name in _REMOVED_TAGS:
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
			source = node.get("data-src") or node.get("data-original") or node.get("src")
			if not source and node.get("srcset"):
				source = str(node["srcset"]).split(",", 1)[0].strip().split(" ", 1)[0]
			if source:
				self.append_image(source, node.get("alt", ""), link_id=link_id)
			return
		if name == "a":
			link_id = self._add_link(node.get("href"))
			linked_before = self._linked_graphic_insertions
			# SymbOS 4.1 changed control 3 from a one-byte underline toggle
			# into a two-byte formatting command.  The clickable link graphic
			# is portable across releases, so keep the label as plain text.
			self._render_link_children(node, link_id, preserve=preserve)
			if link_id is not None:
				if self._linked_graphic_insertions == linked_before:
					self.append_link_icon(link_id)
			return

		is_block = name in _BLOCK_TAGS
		if is_block:
			self.line_break()
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
		)
		if font is not None:
			self._append_control(b"\x02\x01\x01")
		if is_block:
			self.line_break()

	def _render_link_children(self, node, link_id, *, preserve=False):
		for child in list(node.children):
			self._render_node(child, preserve=preserve, link_id=link_id)

	def serialize(self, title):
		text = bytes(self.text) + _TEXT_TRAILER
		info_values = (title, "GB-proxy", "SymbOS", "1", "", "Web page", "Internet")
		info = b"".join(_ascii(value)[:63] + b"\x00" for value in info_values)[:255]
		if not info.endswith(b"\x00"):
			info = info[:-1] + b"\x00"
		head = struct.pack("<HHBB", 200, 600, 0, 2)
		graphics = bytes((len(self.graphics),))
		graphics += b"".join(struct.pack("<H", len(item)) for item in self.graphics)
		graphics += b"".join(self.graphics)
		links = bytes((len(self.links),))
		links += b"".join(struct.pack("<H", len(item)) for item in self.links)
		links += b"".join(self.links)
		document = b"".join((
			_chunk(b"INFO", info),
			_chunk(b"HEAD", head),
			_chunk(b"TEXT", text),
			_chunk(b"GRPH", graphics),
			_chunk(b"LINK", links),
			_chunk(b"ENDF", b""),
		))
		if len(document) > self.limits.max_document_bytes:
			raise DoxError("DOX document exceeds its configured size limit")
		validate_dox(document, limits=self.limits)
		return document


def build_dox_from_html(
	document,
	base_url,
	*,
	profile=SAFE_SGX_PROFILE,
	limits=None,
	image_fetcher=None,
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
	if set(chunks) != _REQUIRED_CHUNKS or list(chunks)[-1:] != [b"ENDF"]:
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


def validate_dox(document, *, limits=None):
	"""Validate and return parsed chunks for GB-proxy's supported DOX subset."""
	limits = limits or DoxLimits()
	chunks = _parse_chunks(document, limits.max_document_bytes)
	if not chunks[b"INFO"] or len(chunks[b"INFO"]) > 255 or b"\x00" not in chunks[b"INFO"]:
		raise DoxValidationError("Invalid INFO chunk")
	if len(chunks[b"HEAD"]) != 6:
		raise DoxValidationError("HEAD must contain exactly six bytes")
	text = chunks[b"TEXT"]
	if len(text) > limits.max_text_bytes or not text.endswith(b"\x00\xff"):
		raise DoxValidationError("Invalid or oversized TEXT chunk")

	graphics = _split_counted_records(
		chunks[b"GRPH"], maximum=limits.max_graphics, label="graphic"
	)
	graphics_bytes = 0
	for graphic in graphics:
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
	return chunks
