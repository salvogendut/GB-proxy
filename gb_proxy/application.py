"""Flask application factory for GB-proxy."""

import importlib
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, current_app, request, send_from_directory
from werkzeug.wrappers.response import Response as WerkzeugResponse

from utils.html_utils import transcode_content, transcode_html
from utils.image_utils import (
	default_cache_dir,
	fetch_and_cache_image,
	image_extension,
	image_mimetype,
	is_image_url,
)
from utils.resource_registry import configure_resources, resolve_resource
from utils.system_utils import ConfigurationError, apply_preset


USER_AGENT = (
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
	"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
)
_EXTENSION_NAME = re.compile(r"^[A-Za-z0-9_]+$")


class UpstreamResponseTooLarge(RuntimeError):
	"""Raised when an upstream response exceeds the configured memory bound."""


@dataclass
class ProxyRuntime:
	settings: object
	cache_dir: str
	state_dir: str
	request_callable: object
	session_factory: object
	request_timeout: tuple
	max_response_bytes: int
	extensions: dict
	domain_to_extension: dict
	override_extension: str = None


def domain_matches(host, domain):
	"""Return true for an exact domain or one of its subdomains."""
	if not host or not domain:
		return False
	host = host.rstrip(".").lower()
	domain = domain.rstrip(".").lower()
	return host == domain or host.endswith("." + domain)


def _default_state_dir():
	configured = os.environ.get("GB_PROXY_STATE_DIR")
	if configured:
		return os.path.abspath(os.path.expanduser(configured))
	state_home = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
	return os.path.join(state_home, "gb-proxy")


def _load_extensions(settings):
	extensions = {}
	domain_to_extension = {}
	for name in getattr(settings, "ENABLED_EXTENSIONS", ()):
		if not isinstance(name, str) or not _EXTENSION_NAME.fullmatch(name):
			raise ConfigurationError(f"Invalid extension name: {name!r}")
		try:
			module = importlib.import_module(f"extensions.{name}.{name}")
		except Exception as error:
			raise ConfigurationError(f"Could not load extension {name}: {error}") from error
		if not hasattr(module, "DOMAIN") or not callable(getattr(module, "handle_request", None)):
			raise ConfigurationError(
				f"Extension {name} must define DOMAIN and handle_request(request)"
			)
		domain = str(module.DOMAIN).rstrip(".").lower()
		if domain in domain_to_extension:
			raise ConfigurationError(f"Multiple extensions handle domain {domain}")
		extensions[name] = module
		domain_to_extension[domain] = module
	return extensions, domain_to_extension


def _copy_settings_to_app(app, settings):
	for name in dir(settings):
		if name.isupper():
			app.config[name] = getattr(settings, name)


def _positive_setting(settings, name, default, converter=int):
	try:
		value = converter(getattr(settings, name, default))
	except (TypeError, ValueError) as error:
		raise ConfigurationError(f"{name} must be numeric") from error
	if value <= 0:
		raise ConfigurationError(f"{name} must be positive")
	return value


def create_app(
	settings,
	*,
	cache_dir=None,
	state_dir=None,
	advertise_url=None,
	request_callable=None,
	session_factory=None,
):
	"""Build and initialize one GB-proxy Flask application."""
	settings = apply_preset(settings)
	required_settings = (
		"ENABLED_EXTENSIONS",
		"WHITELISTED_DOMAINS",
		"SIMPLIFY_HTML",
		"TAGS_TO_UNWRAP",
		"TAGS_TO_STRIP",
		"ATTRIBUTES_TO_STRIP",
		"RESIZE_IMAGES",
		"MAX_IMAGE_WIDTH",
		"MAX_IMAGE_HEIGHT",
		"CONVERT_IMAGES",
		"CONVERT_IMAGES_TO_FILETYPE",
		"DITHERING_ALGORITHM",
		"CONVERT_CHARACTERS",
		"CONVERSION_TABLE",
	)
	missing_settings = [name for name in required_settings if not hasattr(settings, name)]
	if missing_settings:
		raise ConfigurationError(
			"Configuration is missing required settings: " + ", ".join(missing_settings)
		)
	cache_dir = os.path.abspath(os.path.expanduser(cache_dir or default_cache_dir()))
	state_dir = os.path.abspath(os.path.expanduser(state_dir or _default_state_dir()))
	advertise_url = (advertise_url or "http://127.0.0.1:5001").rstrip("/")
	parsed_advertise_url = urlparse(advertise_url)
	if parsed_advertise_url.scheme != "http" or not parsed_advertise_url.netloc:
		raise ConfigurationError("The advertised URL must be an absolute http:// URL")

	# Extensions historically import a module named config. Keep that contract while
	# making the source of the module explicit and package-friendly.
	settings.CACHE_DIR = cache_dir
	settings.STATE_DIR = state_dir
	settings.GB_PROXY_CACHE_DIR = cache_dir
	settings.GB_PROXY_STATE_DIR = state_dir
	sys.modules["config"] = settings

	os.makedirs(cache_dir, exist_ok=True)
	os.makedirs(state_dir, exist_ok=True)

	app = Flask(__name__)
	_copy_settings_to_app(app, settings)
	app.config.update(
		GB_PROXY_CACHE_DIR=cache_dir,
		GB_PROXY_STATE_DIR=state_dir,
		GB_PROXY_ADVERTISE_URL=advertise_url,
		MACPROXY_HOST_AND_PORT=parsed_advertise_url.netloc,
	)
	app.config["MAX_CONTENT_LENGTH"] = _positive_setting(
		settings, "MAX_CLIENT_REQUEST_BYTES", 1024 * 1024
	)

	max_entries = _positive_setting(settings, "RESOURCE_MAX_ENTRIES", 4096)
	ttl_seconds = _positive_setting(settings, "RESOURCE_TTL_SECONDS", 3600)
	max_content_bytes = _positive_setting(
		settings, "MAX_INLINE_RESOURCE_BYTES", 2 * 1024 * 1024
	)
	for name, default in (
		("MAX_UPSTREAM_RESPONSE_BYTES", 16 * 1024 * 1024),
		("MAX_IMAGE_DOWNLOAD_BYTES", 16 * 1024 * 1024),
		("MAX_IMAGE_CACHE_BYTES", 512 * 1024 * 1024),
		("MAX_IMAGE_CACHE_FILES", 4096),
		("MAX_IMAGE_PIXELS", 16 * 1024 * 1024),
	):
		_positive_setting(settings, name, default)
	_positive_setting(settings, "IMAGE_REQUEST_TIMEOUT", 30, float)
	_positive_setting(settings, "SVG_CONVERSION_TIMEOUT", 10, float)
	connect_timeout = _positive_setting(settings, "UPSTREAM_CONNECT_TIMEOUT", 5, float)
	read_timeout = _positive_setting(settings, "UPSTREAM_READ_TIMEOUT", 30, float)
	try:
		configure_resources(
			max_entries=max_entries,
			ttl_seconds=ttl_seconds,
			max_content_bytes=max_content_bytes,
		)
	except ValueError as error:
		raise ConfigurationError(f"Invalid resource registry settings: {error}") from error

	extensions, domain_to_extension = _load_extensions(settings)
	runtime = ProxyRuntime(
		settings=settings,
		cache_dir=cache_dir,
		state_dir=state_dir,
		request_callable=request_callable,
		session_factory=session_factory or requests.Session,
		request_timeout=(
			connect_timeout,
			read_timeout,
		),
		max_response_bytes=_positive_setting(
			settings, "MAX_UPSTREAM_RESPONSE_BYTES", 16 * 1024 * 1024
		),
		extensions=extensions,
		domain_to_extension=domain_to_extension,
	)
	app.extensions["gb_proxy_runtime"] = runtime
	_register_routes(app, runtime)
	return app


def _cache_image(runtime, url, content=None):
	settings = runtime.settings
	return fetch_and_cache_image(
		url,
		content,
		resize=settings.RESIZE_IMAGES,
		max_width=settings.MAX_IMAGE_WIDTH,
		max_height=settings.MAX_IMAGE_HEIGHT,
		convert=settings.CONVERT_IMAGES,
		convert_to=settings.CONVERT_IMAGES_TO_FILETYPE,
		dithering=settings.DITHERING_ALGORITHM,
		cache_dir=runtime.cache_dir,
		timeout=float(getattr(settings, "IMAGE_REQUEST_TIMEOUT", 30)),
		svg_timeout=float(getattr(settings, "SVG_CONVERSION_TIMEOUT", 10)),
		max_download_bytes=int(
			getattr(settings, "MAX_IMAGE_DOWNLOAD_BYTES", 16 * 1024 * 1024)
		),
		max_cache_bytes=int(getattr(settings, "MAX_IMAGE_CACHE_BYTES", 512 * 1024 * 1024)),
		max_cache_files=int(getattr(settings, "MAX_IMAGE_CACHE_FILES", 4096)),
		max_image_pixels=int(getattr(settings, "MAX_IMAGE_PIXELS", 16 * 1024 * 1024)),
	)


def _send_image_file(runtime, filename):
	if filename != os.path.basename(filename):
		return abort(404, "Image not found")
	if not getattr(runtime.settings, "MINIMAL_RESPONSE_HEADERS", False):
		return send_from_directory(runtime.cache_dir, filename, mimetype=image_mimetype(filename))
	path = os.path.join(runtime.cache_dir, filename)
	try:
		with open(path, "rb") as image_file:
			content = image_file.read()
	except OSError:
		return abort(404, "Image not found")
	return Response(content, status=200, mimetype=image_mimetype(filename))


def _send_cached_image(runtime, cached_url):
	return _send_image_file(runtime, os.path.basename(cached_url))


def _handle_image_request(runtime, url):
	cached_url = _cache_image(runtime, url)
	if cached_url:
		return _send_cached_image(runtime, cached_url)
	return abort(404, "Image not found or could not be processed")


def _handle_override_extension(runtime, scheme):
	if not runtime.override_extension:
		return None
	extension_name = runtime.override_extension.split(".")[-1]
	extension = runtime.extensions.get(extension_name)
	if extension is None:
		current_app.logger.warning("Override extension %s is unavailable", extension_name)
		runtime.override_extension = None
		return None
	if scheme not in ("http", "https", "ftp"):
		current_app.logger.warning("Unsupported override URL scheme %s", scheme)
		return None
	response = extension.handle_request(request)
	if hasattr(extension, "get_override_status") and not extension.get_override_status():
		runtime.override_extension = None
	return response


def _find_matching_extension(runtime, host):
	for domain, extension in runtime.domain_to_extension.items():
		if domain_matches(host, domain):
			return extension
	return None


def _handle_matching_extension(runtime, extension):
	response = extension.handle_request(request)
	if hasattr(extension, "get_override_status") and extension.get_override_status():
		runtime.override_extension = extension.__name__
	return response


def _read_upstream_content(response, limit):
	if hasattr(response, "iter_content"):
		chunks = []
		total = 0
		for chunk in response.iter_content(chunk_size=64 * 1024):
			if not chunk:
				continue
			total += len(chunk)
			if total > limit:
				raise UpstreamResponseTooLarge(
					f"Upstream response exceeds the {limit}-byte limit"
				)
			chunks.append(chunk)
		return b"".join(chunks)
	content = response.content
	if len(content) > limit:
		raise UpstreamResponseTooLarge(f"Upstream response exceeds the {limit}-byte limit")
	return content


def _prepare_headers():
	headers = {"User-Agent": USER_AGENT}
	for name in ("Accept", "Accept-Language", "Referer"):
		value = request.headers.get(name)
		if value:
			headers[name] = value
	return headers


def _send_request(runtime, url, append_query=False):
	kwargs = {
		"headers": _prepare_headers(),
		"allow_redirects": True,
		"timeout": runtime.request_timeout,
		"stream": True,
	}
	if request.method == "POST":
		kwargs["data"] = request.form
	else:
		kwargs["params"] = request.args if append_query else None
	if runtime.request_callable is not None:
		return runtime.request_callable(request.method, url, **kwargs), None
	session = runtime.session_factory()
	try:
		return session.request(request.method, url, **kwargs), session
	except Exception:
		session.close()
		raise


def _handle_target_request(runtime, url, append_query=False):
	current_app.logger.info("Fetching upstream URL %s", urlparse(url)._replace(query="").geturl())
	response = None
	session = None
	try:
		response, session = _send_request(runtime, url, append_query=append_query)
		content = _read_upstream_content(response, runtime.max_response_bytes)
		result = (content, response.status_code, dict(response.headers))
		return _process_response(runtime, result, response.url)
	except requests.Timeout:
		current_app.logger.warning("Upstream request timed out for %s", url)
		return abort(504, "Upstream request timed out")
	except UpstreamResponseTooLarge as error:
		current_app.logger.warning("%s", error)
		return abort(502, "Upstream response is too large")
	except requests.RequestException:
		current_app.logger.exception("Upstream request failed for %s", url)
		return abort(502, "Upstream connection failed")
	except Exception:
		current_app.logger.exception("Unhandled proxy error for %s", url)
		return abort(500, "GB-proxy encountered an internal error")
	finally:
		if response is not None and callable(getattr(response, "close", None)):
			response.close()
		if session is not None:
			session.close()


def _process_response(runtime, response, url):
	if isinstance(response, tuple):
		if len(response) == 3:
			content, status_code, headers = response
		elif len(response) == 2:
			content, status_code = response
			headers = {}
		else:
			content = response[0]
			status_code = 200
			headers = {}
	elif isinstance(response, (Response, WerkzeugResponse)):
		return response
	else:
		content = response
		status_code = 200
		headers = {}

	content_type = next(
		(value for key, value in headers.items() if key.lower() == "content-type"),
		"",
	).lower()

	if content_type.startswith("image/"):
		cached_url = _cache_image(runtime, url, content)
		if cached_url:
			return _send_cached_image(runtime, cached_url)
		return abort(404, "Image could not be processed")

	if content_type in (
		"text/css",
		"text/javascript",
		"application/javascript",
		"application/x-javascript",
	):
		content = transcode_content(content)
		result = Response(content, status_code)
		result.headers["Content-Type"] = content_type
		return result

	non_transcode_types = (
		"application/octet-stream",
		"application/pdf",
		"application/zip",
		"application/x-zip-compressed",
		"application/x-rar-compressed",
		"application/x-tar",
		"application/x-gzip",
		"application/x-bzip2",
		"application/x-7z-compressed",
		"application/mac-binary",
		"application/macbinary",
		"application/x-binary",
		"application/x-macbinary",
		"application/binhex",
		"application/binhex4",
		"application/mac-binhex",
		"application/mac-binhex40",
		"application/x-binhex40",
		"application/x-mac-binhex40",
		"application/x-sit",
		"application/x-stuffit",
		"application/vnd.openxmlformats-officedocument",
		"application/vnd.ms-excel",
		"application/vnd.ms-powerpoint",
		"application/msword",
		"audio/",
		"video/",
		"text/plain",
	)

	if not any(content_type.startswith(item) for item in non_transcode_types):
		if isinstance(content, bytes):
			content = content.decode("utf-8", errors="replace")
		settings = runtime.settings
		content = transcode_html(
			content,
			url,
			whitelisted_domains=settings.WHITELISTED_DOMAINS,
			simplify_html=settings.SIMPLIFY_HTML,
			tags_to_unwrap=settings.TAGS_TO_UNWRAP,
			tags_to_strip=settings.TAGS_TO_STRIP,
			attributes_to_strip=settings.ATTRIBUTES_TO_STRIP,
			convert_characters=settings.CONVERT_CHARACTERS,
			conversion_table=settings.CONVERSION_TABLE,
			allowed_tags=getattr(settings, "ALLOWED_HTML_TAGS", None),
			allowed_attributes=getattr(settings, "ALLOWED_HTML_ATTRIBUTES", None),
			shorten_link_urls=getattr(settings, "SHORTEN_LINK_URLS", False),
			short_image_urls=getattr(settings, "SHORT_IMAGE_URLS", False),
			ascii_only=getattr(settings, "ASCII_ONLY", False),
			max_image_alt_length=getattr(settings, "MAX_IMAGE_ALT_LENGTH", None),
		)

	result = Response(content, status_code)
	ignored_headers = {
		"connection",
		"content-encoding",
		"content-length",
		"date",
		"keep-alive",
		"proxy-authenticate",
		"proxy-authorization",
		"server",
		"te",
		"trailer",
		"transfer-encoding",
		"upgrade",
	}
	minimal_headers = {"content-disposition", "content-type"}
	for key, value in headers.items():
		lower_key = key.lower()
		if lower_key in ignored_headers:
			continue
		if getattr(runtime.settings, "MINIMAL_RESPONSE_HEADERS", False) and lower_key not in minimal_headers:
			continue
		result.headers[key] = value
	return result


def _register_routes(app, runtime):
	@app.get("/cached_image/<path:filename>")
	def serve_cached_image(filename):
		return _send_image_file(runtime, filename)

	@app.get("/i/<token>.<extension>")
	def serve_short_image(token, extension):
		expected_extension = image_extension(
			runtime.settings.CONVERT_IMAGES,
			runtime.settings.CONVERT_IMAGES_TO_FILETYPE,
		)
		if extension.lower() != expected_extension:
			return abort(404, "Unknown converted image format")
		resource = resolve_resource("image", token)
		if resource is None:
			return abort(404, "Image token has expired")
		cached_url = _cache_image(runtime, resource.target, resource.content)
		if not cached_url:
			return abort(404, "Image could not be processed")
		return _send_cached_image(runtime, cached_url)

	@app.route("/u/<token>", methods=("GET", "POST"))
	def follow_short_url(token):
		resource = resolve_resource("url", token)
		if resource is None:
			return abort(404, "Link token has expired")
		return _handle_target_request(runtime, resource.target, append_query=True)

	@app.route("/", defaults={"path": "/"}, methods=("GET", "POST"))
	@app.route("/<path:path>", methods=("GET", "POST"))
	def handle_request(path):
		parsed_url = urlparse(request.url)
		override_response = _handle_override_extension(runtime, parsed_url.scheme)
		if override_response is not None:
			return _process_response(runtime, override_response, request.url)

		matching_extension = _find_matching_extension(runtime, parsed_url.hostname)
		if matching_extension:
			return _process_response(
				runtime,
				_handle_matching_extension(runtime, matching_extension),
				request.url,
			)

		if is_image_url(request.url):
			return _handle_image_request(runtime, request.url)
		return _handle_target_request(runtime, request.url, append_query=False)
