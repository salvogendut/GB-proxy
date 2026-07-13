"""HTML transformations for legacy and constrained proxy clients."""

import base64
import binascii
import copy
import hashlib
import html as html_module
import re
import unicodedata
from urllib.parse import unquote_to_bytes, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Doctype
from bs4.formatter import HTMLFormatter
from flask import current_app, url_for

from utils.image_utils import fetch_and_cache_image, image_extension
from utils.resource_registry import register_resource


class URLAwareHTMLFormatter(HTMLFormatter):
	def escape(self, string):
		if isinstance(string, list):
			return [html_module.escape(str(item), quote=True) for item in string]
		if string is None:
			return ""
		return html_module.escape(str(string), quote=True)

	def attributes(self, tag):
		for key, value in tag.attrs.items():
			if key in ("href", "src"):
				yield key, value
			else:
				yield key, self.escape(value)


def transcode_content(content):
	"""Convert HTTPS references in CSS or JavaScript content to HTTP."""
	if isinstance(content, bytes):
		content = content.decode("utf-8", errors="replace")

	patterns = [
		(r"""url\(['"]?(https://[^)'"]+)['"]?\)""", r"url(\1)"),
		(r'"https://', '"http://'),
		(r"'https://", "'http://"),
		(r"https://", "http://"),
	]
	for pattern, replacement in patterns:
		content = re.sub(
			pattern,
			lambda match: replacement.replace(
				r"\1",
				match.group(1).replace("https://", "http://") if match.groups() else "",
			),
			content,
		)
	return content.encode("utf-8")


def _proxy_url(endpoint, **values):
	path = url_for(endpoint, **values)
	base_url = current_app.config.get("GB_PROXY_ADVERTISE_URL")
	if not base_url:
		base_url = f"http://{current_app.config['MACPROXY_HOST_AND_PORT']}"
	return f"{base_url.rstrip('/')}{path}"


def _image_setting(name, default=None):
	return current_app.config.get(name, default)


def _absolute_web_url(base_url, value):
	if not value:
		return None
	value = value.strip()
	if value.startswith("//"):
		scheme = urlparse(base_url).scheme or "https"
		value = f"{scheme}:{value}"
	absolute = urljoin(base_url, value)
	if urlparse(absolute).scheme.lower() not in ("http", "https"):
		return None
	return absolute


def _domain_matches(host, domain):
	if not host or not domain:
		return False
	host = host.rstrip(".").lower()
	domain = domain.rstrip(".").lower()
	return host == domain or host.endswith("." + domain)


def _decode_data_uri(source):
	try:
		header, payload = source.split(",", 1)
		if ";base64" in header.lower():
			return base64.b64decode(payload, validate=True)
		return unquote_to_bytes(payload)
	except (ValueError, binascii.Error):
		return None


def _ascii_text(value):
	return unicodedata.normalize("NFKD", value).encode("ascii", errors="ignore").decode("ascii")


def _replace_inline_svgs(soup, short_image_urls):
	for use_tag in list(soup.find_all("use")):
		attribute = None
		if "href" in use_tag.attrs:
			attribute = "href"
		elif "xlink:href" in use_tag.attrs:
			attribute = "xlink:href"
		if not attribute or not str(use_tag[attribute]).startswith("#"):
			continue
		symbol_tag = soup.find("symbol", {"id": str(use_tag[attribute])[1:]})
		if symbol_tag is None:
			continue
		if "viewBox" in symbol_tag.attrs and use_tag.parent.name == "svg" and "viewBox" not in use_tag.parent.attrs:
			use_tag.parent["viewBox"] = symbol_tag["viewBox"]
		symbol_copy = copy.copy(symbol_tag)
		use_tag.replace_with(symbol_copy)
		symbol_copy.unwrap()

	for svg_tag in list(soup.find_all("svg")):
		svg_attrs = dict(svg_tag.attrs)
		view_box = svg_attrs.get("viewBox") or svg_attrs.get("viewbox")
		if view_box:
			parts = str(view_box).replace(",", " ").split()
			if len(parts) == 4:
				svg_attrs.setdefault("width", parts[2])
				svg_attrs.setdefault("height", parts[3])

		svg_data = str(svg_tag).encode("utf-8")
		if len(svg_data) > _image_setting("MAX_INLINE_RESOURCE_BYTES", 2 * 1024 * 1024):
			svg_tag.decompose()
			continue
		fake_url = "inline-svg:" + hashlib.sha256(svg_data).hexdigest()
		extension = image_extension(
			_image_setting("CONVERT_IMAGES", True),
			_image_setting("CONVERT_IMAGES_TO_FILETYPE", "gif"),
			fake_url,
		)
		if short_image_urls:
			token = register_resource("image", fake_url, svg_data)
			source = _proxy_url("serve_short_image", token=token, extension=extension)
		else:
			cached_url = fetch_and_cache_image(
				fake_url,
				svg_data,
				resize=_image_setting("RESIZE_IMAGES", True),
				max_width=_image_setting("MAX_IMAGE_WIDTH", 512),
				max_height=_image_setting("MAX_IMAGE_HEIGHT", 342),
				convert=_image_setting("CONVERT_IMAGES", True),
				convert_to=_image_setting("CONVERT_IMAGES_TO_FILETYPE", "gif"),
				dithering=_image_setting("DITHERING_ALGORITHM", "FLOYDSTEINBERG"),
				hash_url=False,
				cache_dir=_image_setting("GB_PROXY_CACHE_DIR"),
				timeout=_image_setting("IMAGE_REQUEST_TIMEOUT", 30),
				max_download_bytes=_image_setting("MAX_IMAGE_DOWNLOAD_BYTES", 16 * 1024 * 1024),
				max_cache_bytes=_image_setting("MAX_IMAGE_CACHE_BYTES", 512 * 1024 * 1024),
				max_cache_files=_image_setting("MAX_IMAGE_CACHE_FILES", 4096),
				max_image_pixels=_image_setting("MAX_IMAGE_PIXELS", 16 * 1024 * 1024),
			)
			if not cached_url:
				svg_tag.decompose()
				continue
			base_url = current_app.config.get("GB_PROXY_ADVERTISE_URL")
			if not base_url:
				base_url = f"http://{current_app.config['MACPROXY_HOST_AND_PORT']}"
			source = f"{base_url.rstrip('/')}{cached_url}"

		image_attrs = {"src": source, "data-proxy-cached": "1"}
		for dimension in ("width", "height"):
			if dimension in svg_attrs:
				image_attrs[dimension] = svg_attrs[dimension]
		if "aria-label" in svg_attrs:
			image_attrs["alt"] = svg_attrs["aria-label"]
		svg_tag.replace_with(soup.new_tag("img", **image_attrs))


def _rewrite_images(soup, base_url, max_alt_length=None):
	extension = image_extension(
		_image_setting("CONVERT_IMAGES", True),
		_image_setting("CONVERT_IMAGES_TO_FILETYPE", "gif"),
	)
	for image_tag in list(soup.find_all("img")):
		if image_tag.attrs.pop("data-proxy-cached", None):
			continue

		source = image_tag.get("data-src") or image_tag.get("data-original") or image_tag.get("src")
		if not source and image_tag.get("srcset"):
			source = str(image_tag["srcset"]).split(",", 1)[0].strip().split(" ", 1)[0]
		if not source:
			if image_tag.get("alt"):
				image_tag.replace_with(image_tag["alt"])
			else:
				image_tag.decompose()
			continue

		content = None
		if str(source).startswith("data:"):
			content = _decode_data_uri(str(source))
			if content is None:
				image_tag.decompose()
				continue
			if len(content) > _image_setting("MAX_INLINE_RESOURCE_BYTES", 2 * 1024 * 1024):
				image_tag.decompose()
				continue
			target = "inline-image:" + hashlib.sha256(content).hexdigest()
		else:
			target = _absolute_web_url(base_url, str(source))
			if target is None:
				image_tag.decompose()
				continue

		token = register_resource("image", target, content)
		image_tag["src"] = _proxy_url("serve_short_image", token=token, extension=extension)
		for attribute in ("data-src", "data-original", "loading", "srcset"):
			image_tag.attrs.pop(attribute, None)
		if max_alt_length is not None and image_tag.get("alt"):
			image_tag["alt"] = str(image_tag["alt"])[:max_alt_length]


def _rewrite_navigation(soup, base_url):
	for link_tag in soup.find_all("a"):
		href = link_tag.get("href")
		if not href or str(href).startswith("#"):
			continue
		target = _absolute_web_url(base_url, str(href))
		if target is None:
			link_tag.attrs.pop("href", None)
			continue
		token = register_resource("url", target)
		link_tag["href"] = _proxy_url("follow_short_url", token=token)

	for form_tag in soup.find_all("form"):
		action = form_tag.get("action") or base_url
		target = _absolute_web_url(base_url, str(action))
		if target is None:
			form_tag.attrs.pop("action", None)
			continue
		token = register_resource("url", target)
		form_tag["action"] = _proxy_url("follow_short_url", token=token)
		method = str(form_tag.get("method", "get")).lower()
		form_tag["method"] = method if method in ("get", "post") else "get"


def _downgrade_urls(soup):
	for tag in soup.find_all(("link", "script", "img", "a", "iframe", "form")):
		for attribute in ("src", "href", "action"):
			value = tag.get(attribute)
			if not value:
				continue
			if str(value).startswith("https://"):
				tag[attribute] = "http://" + str(value)[8:]
			elif str(value).startswith("//"):
				tag[attribute] = "http:" + str(value)


def _apply_allowlists(soup, allowed_tags, allowed_attributes):
	if allowed_tags is not None:
		allowed = set(allowed_tags)
		for tag in list(soup.find_all(True)):
			if tag.name not in allowed and tag.parent is not None:
				tag.unwrap()

	if allowed_attributes is not None:
		global_attributes = set(allowed_attributes.get("*", ()))
		for tag in soup.find_all(True):
			allowed = global_attributes | set(allowed_attributes.get(tag.name, ()))
			for attribute in list(tag.attrs):
				if attribute not in allowed:
					del tag[attribute]


def _transliterate_document(soup):
	for text_node in list(soup.find_all(string=True)):
		if isinstance(text_node, (Comment, Doctype)):
			text_node.extract()
			continue
		text_node.replace_with(_ascii_text(str(text_node)))
	for tag in soup.find_all(True):
		for attribute in ("alt", "placeholder", "title", "value"):
			if attribute in tag.attrs:
				tag[attribute] = _ascii_text(str(tag[attribute]))


def transcode_html(document, url=None, whitelisted_domains=None, simplify_html=False,
				  tags_to_unwrap=None, tags_to_strip=None, attributes_to_strip=None,
				  convert_characters=False, conversion_table=None,
				  allowed_tags=None, allowed_attributes=None,
				  shorten_link_urls=False, short_image_urls=False, ascii_only=False,
				  max_image_alt_length=None):
	"""Convert an HTML response for the configured legacy client."""
	if isinstance(document, bytes):
		document = document.decode("utf-8", errors="replace")

	if convert_characters:
		for key, replacement in (conversion_table or {}).items():
			if isinstance(replacement, bytes):
				replacement = replacement.decode("utf-8")
			document = document.replace(key, replacement)

	base_url = url or "http://localhost/"
	soup = BeautifulSoup(document, "html5lib")
	domain = urlparse(base_url).hostname
	is_whitelisted = any(_domain_matches(domain, item) for item in (whitelisted_domains or ()))

	if simplify_html and not is_whitelisted:
		for tag in list(soup.find_all(tags_to_unwrap or ())):
			if tag.parent is not None:
				tag.unwrap()
		for tag in list(soup.find_all(tags_to_strip or ())):
			tag.decompose()
		for tag in soup.find_all(True):
			for attribute in (attributes_to_strip or ()):
				tag.attrs.pop(attribute, None)

	_replace_inline_svgs(soup, short_image_urls)
	if short_image_urls:
		_rewrite_images(soup, base_url, max_image_alt_length)
	if shorten_link_urls:
		_rewrite_navigation(soup, base_url)
	else:
		_downgrade_urls(soup)

	for meta_tag in soup.find_all("meta", attrs={"http-equiv": "refresh"}):
		if "content" in meta_tag.attrs:
			meta_tag["content"] = str(meta_tag["content"]).replace("https://", "http://")

	if simplify_html and not is_whitelisted:
		_apply_allowlists(soup, allowed_tags, allowed_attributes)
	if ascii_only:
		_transliterate_document(soup)

	output = soup.decode(formatter=URLAwareHTMLFormatter())
	output = output.replace("<br/>", "<br>").replace("<hr/>", "<hr>")
	output = re.sub(r"<(img|input)([^>]*)/>", r"<\1\2>", output)
	return output.encode("ascii" if ascii_only else "utf-8", errors="ignore" if ascii_only else "strict")
