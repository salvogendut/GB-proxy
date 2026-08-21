"""Safe conversion of remote Markdown documents into bounded-pipeline HTML."""

import html
import posixpath
import re
import unicodedata
from email.message import Message
from urllib.parse import unquote, urlsplit

import markdown
from bs4 import BeautifulSoup, Comment
from markdown.extensions import Extension
from markdown.inlinepatterns import BacktickInlineProcessor


_MARKDOWN_MEDIA_TYPES = frozenset((
	"text/markdown",
	"text/x-markdown",
	"application/markdown",
	"application/x-markdown",
))
_MARKDOWN_PATH_SUFFIXES = (".md", ".markdown")
_ALLOWED_TAGS = frozenset((
	"a",
	"blockquote",
	"br",
	"code",
	"em",
	"h1",
	"h2",
	"h3",
	"h4",
	"h5",
	"h6",
	"hr",
	"img",
	"li",
	"ol",
	"p",
	"pre",
	"strong",
	"table",
	"tbody",
	"td",
	"th",
	"thead",
	"tr",
	"ul",
))
_ALLOWED_ATTRIBUTES = {
	"a": frozenset(("href", "title")),
	"img": frozenset(("alt", "src", "title")),
	"ol": frozenset(("start",)),
}
_DROP_TAGS = frozenset((
	"applet",
	"audio",
	"button",
	"canvas",
	"embed",
	"form",
	"iframe",
	"input",
	"link",
	"meta",
	"object",
	"script",
	"select",
	"source",
	"style",
	"svg",
	"template",
	"textarea",
	"video",
))
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SAFE_URL_SCHEMES = frozenset(("http", "https"))
_ORDERED_LIST_START = re.compile(r"-?[0-9]{1,9}\Z")
_BACKTICK_RUN = re.compile(r"`+")
_MAX_BACKTICK_RUNS = 4096
_MAX_FAILED_BACKTICK_SCAN_WORK = 16 * 1024 * 1024

# Python-Markdown 3.10.3 and earlier use a back-reference which can take cubic
# time on unmatched backticks (upstream issue #1617).  This Python-3.9-safe
# lookahead makes the opening run atomic, and the run-start boundary prevents
# retrying every suffix of the same run.  The deterministic preflight below
# also caps adversarial arrangements of many differently sized unmatched runs.
_SAFE_BACKTICK_RE = (
	r"(?:(?<!\\)((?:\\{2})+)(?=`+)|(?<![`\\])(?=(`+))\2(.+?)(?<!`)\2(?!`))"
)


class MarkdownSafetyError(ValueError):
	"""Raised when Markdown exceeds a deterministic parser-safety bound."""


class _EscapeRawHtml(Extension):
	"""Make source HTML visible as text instead of passing it through."""

	def extendMarkdown(self, md):
		# These are the two standard Python-Markdown raw-HTML entry points.
		# Deregistering them is the supported replacement for the removed
		# ``safe_mode='escape'`` option.
		md.preprocessors.deregister("html_block")
		md.inlinePatterns.deregister("html")
		md.inlinePatterns.register(
			BacktickInlineProcessor(_SAFE_BACKTICK_RE), "backtick", 190
		)


def _header_text(value):
	if isinstance(value, bytes):
		return value.decode("latin-1", errors="replace")
	return str(value or "")


def _media_type(content_type):
	return _header_text(content_type).split(";", 1)[0].strip().lower()


def _decoded_url_path(url):
	try:
		return unquote(urlsplit(str(url or "")).path)
	except (TypeError, ValueError):
		return ""


def is_markdown_response(content_type, url):
	"""Return whether a response should be interpreted as remote Markdown."""
	media_type = _media_type(content_type)
	if media_type in _MARKDOWN_MEDIA_TYPES:
		return True
	if media_type != "text/plain":
		return False
	return _decoded_url_path(url).lower().endswith(_MARKDOWN_PATH_SUFFIXES)


def _declared_charset(content_type):
	header = _header_text(content_type)
	if not header:
		return "utf-8"
	try:
		message = Message()
		message["Content-Type"] = header
		return message.get_content_charset() or "utf-8"
	except (TypeError, ValueError):
		return "utf-8"


def _decode_content(content, content_type):
	if isinstance(content, str):
		return content
	if isinstance(content, (bytearray, memoryview)):
		content = bytes(content)
	if not isinstance(content, bytes):
		return str(content or "")
	charset = _declared_charset(content_type)
	try:
		return content.decode(charset, errors="replace")
	except (LookupError, TypeError):
		return content.decode("utf-8", errors="replace")


def _check_backtick_complexity(source):
	"""Bound the worst-case failed scans performed by the inline-code rule."""
	if "`" not in source:
		return

	runs = []
	for match in _BACKTICK_RUN.finditer(source):
		if len(runs) >= _MAX_BACKTICK_RUNS:
			raise MarkdownSafetyError(
				"Markdown contains too many inline-code delimiter runs"
			)
		start = match.start()
		backslashes = 0
		index = start - 1
		while index >= 0 and source[index] == "\\":
			backslashes += 1
			index -= 1
		runs.append((start, match.end() - start, backslashes % 2 == 0))

	# A valid opening consumes through the first equally sized closing run.
	# Precompute that next run so the preflight itself remains linear.
	next_same = [None] * len(runs)
	last_by_length = {}
	for index in range(len(runs) - 1, -1, -1):
		length = runs[index][1]
		next_same[index] = last_by_length.get(length)
		last_by_length[length] = index

	failed_work = 0
	index = 0
	while index < len(runs):
		start, _length, can_open = runs[index]
		if not can_open:
			index += 1
			continue
		closing = next_same[index]
		if closing is not None:
			index = closing + 1
			continue
		failed_work += len(source) - start
		if failed_work > _MAX_FAILED_BACKTICK_SCAN_WORK:
			raise MarkdownSafetyError(
				"Markdown inline-code delimiters exceed the safe parsing complexity limit"
			)
		index += 1


def _contains_control_character(value):
	return any(unicodedata.category(character).startswith("C") for character in value)


def _safe_url(value):
	if isinstance(value, (list, tuple)):
		return False
	value = str(value or "").strip()
	if not value or _contains_control_character(value):
		return False
	try:
		scheme = urlsplit(value).scheme.lower()
	except ValueError:
		return False
	return not scheme or scheme in _SAFE_URL_SCHEMES


def _clean_text_attribute(value):
	if isinstance(value, (list, tuple)):
		value = " ".join(str(item) for item in value)
	return "".join(
		character for character in str(value)
		if not unicodedata.category(character).startswith("C")
	)


def _sanitize_fragment(fragment):
	soup = BeautifulSoup(fragment, "html.parser")
	for comment in list(soup.find_all(string=lambda value: isinstance(value, Comment))):
		comment.extract()

	for tag in list(soup.find_all(True)):
		name = tag.name.lower()
		if name in _DROP_TAGS:
			tag.decompose()
			continue
		if name not in _ALLOWED_TAGS:
			tag.unwrap()
			continue

		allowed_attributes = _ALLOWED_ATTRIBUTES.get(name, frozenset())
		for attribute in list(tag.attrs):
			if attribute not in allowed_attributes:
				del tag.attrs[attribute]

		if name == "a":
			href = tag.get("href")
			if not _safe_url(href):
				tag.unwrap()
				continue
			tag["href"] = str(href).strip()
		elif name == "img":
			source = tag.get("src")
			if not _safe_url(source):
				alternative = _clean_text_attribute(tag.get("alt", ""))
				if alternative:
					tag.replace_with(alternative)
				else:
					tag.decompose()
				continue
			tag["src"] = str(source).strip()
		elif name == "ol" and "start" in tag.attrs:
			start = str(tag["start"]).strip()
			if _ORDERED_LIST_START.fullmatch(start):
				tag["start"] = start
			else:
				del tag.attrs["start"]

		for attribute in ("alt", "title"):
			if attribute in tag.attrs:
				tag[attribute] = _clean_text_attribute(tag[attribute])

	return soup


def _fallback_title(url):
	path = _decoded_url_path(url).rstrip("/")
	filename = posixpath.basename(path) if path else ""
	if filename:
		return filename
	try:
		return urlsplit(str(url or "")).hostname or "Markdown document"
	except (TypeError, ValueError):
		return "Markdown document"


def _document_title(soup, url):
	heading = soup.find(_HEADING_TAGS)
	title = heading.get_text(" ", strip=True) if heading is not None else ""
	if not title:
		title = _fallback_title(url)
	title = re.sub(r"\s+", " ", _clean_text_attribute(title)).strip()
	return (title or "Markdown document")[:255]


def markdown_to_html(content, content_type, url):
	"""Render one untrusted remote Markdown response as sanitized full HTML5."""
	source = _decode_content(content, content_type)
	_check_backtick_complexity(source)
	fragment = markdown.markdown(
		source,
		extensions=(
			_EscapeRawHtml(),
			"markdown.extensions.fenced_code",
			"markdown.extensions.sane_lists",
			"markdown.extensions.tables",
		),
		output_format="html",
	)
	soup = _sanitize_fragment(fragment)
	title = html.escape(_document_title(soup, url), quote=False)
	body = "".join(str(node) for node in soup.contents)
	return (
		"<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>"
		+ title
		+ "</title></head><body>"
		+ body
		+ "</body></html>"
	)
