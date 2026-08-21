"""Flask application factory for GB-proxy."""

import html
import importlib
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, current_app, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers.response import Response as WerkzeugResponse

from utils.dox_utils import (
	DOX_MIMETYPE,
	DoxLimits,
	build_dox_from_html,
	build_dox_from_image,
	parse_sgx_profile,
)
from utils.html_utils import transcode_content, transcode_html
from utils.image_utils import (
	GBPC_MODE_1,
	GBPC_MODE_7,
	default_cache_dir,
	fetch_and_cache_image,
	image_extension,
	image_mimetype,
	is_image_url,
)
from utils.markdown_utils import (
	MarkdownSafetyError,
	is_markdown_response,
	markdown_to_html,
)
from utils.resource_registry import configure_resources, register_resource, resolve_resource
from utils.system_utils import ConfigurationError, apply_preset


USER_AGENT = (
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
	"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
)
_EXTENSION_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_GBPC_REQUEST_HEADER = "X-GBPC"
_SGX_REQUEST_HEADER = "X-GB-SGX"
_STANDARD_UPSTREAM_ACCEPT = (
	"text/html, application/xhtml+xml, text/markdown;q=0.9, image/*;q=0.8, */*;q=0.1"
)


class UpstreamResponseTooLarge(RuntimeError):
	"""Raised when an upstream response exceeds the configured memory bound."""


_SELF_HOSTS = frozenset(("localhost", "127.0.0.1", "::1", "[::1]", "waitress.invalid"))


class _EmptyPathMiddleware:
	"""Normalize an origin-only absolute proxy URI before Flask routing."""

	def __init__(self, application):
		self.application = application

	def __call__(self, environ, start_response):
		if not environ.get("PATH_INFO"):
			environ["PATH_INFO"] = "/"
			for name in ("RAW_URI", "REQUEST_URI"):
				if name in environ and not environ[name]:
					environ[name] = "/"
		return self.application(environ, start_response)


def _is_proxy_self_request():
	"""Return true when the request target is the proxy itself.

	A well-formed proxy request uses an absolute-form target whose authority is
	the upstream site, so its reconstructed URL never names the proxy. A plain
	request to the proxy (``GET /``) reconstructs to the proxy's own address;
	fetching that would connect the proxy to itself and wedge the worker.
	"""
	target = urlparse(request.url)
	host = (target.hostname or "").lower()
	if not host:
		return False
	advertise_host = (
		urlparse(current_app.config["GB_PROXY_ADVERTISE_URL"]).hostname or ""
	).lower()
	if advertise_host and host == advertise_host:
		return True
	return host in _SELF_HOSTS


@dataclass
class ProxyRuntime:
	settings: object
	cache_dir: str
	state_dir: str
	request_callable: object
	session_factory: object
	request_timeout: tuple
	max_response_bytes: int
	max_markdown_source_bytes: int
	dox_limits: DoxLimits
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
	app.wsgi_app = _EmptyPathMiddleware(app.wsgi_app)
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
		("MAX_MARKDOWN_SOURCE_BYTES", 1024 * 1024),
		("MAX_IMAGE_DOWNLOAD_BYTES", 16 * 1024 * 1024),
		("MAX_IMAGE_CACHE_BYTES", 512 * 1024 * 1024),
		("MAX_IMAGE_CACHE_FILES", 4096),
		("MAX_IMAGE_PIXELS", 16 * 1024 * 1024),
		("MAX_DOX_TEXT_BYTES", 11500),
		("MAX_DOX_LINKS", 64),
		("MAX_DOX_GRAPHICS", 8),
		("MAX_DOX_GRAPHICS_BYTES", 64 * 1024),
		("MAX_DOX_CONTROLS", 16),
		("MAX_DOX_CONTROL_BYTES", 2 * 1024),
		("MAX_DOX_DOCUMENT_BYTES", 96 * 1024),
		("MAX_DOX_IMAGE_WIDTH", 160),
		("MAX_DOX_IMAGE_HEIGHT", 96),
		("MAX_DOX_URL_BYTES", 127),
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
	try:
		dox_limits = DoxLimits(
			max_text_bytes=int(getattr(settings, "MAX_DOX_TEXT_BYTES", 11500)),
			max_links=int(getattr(settings, "MAX_DOX_LINKS", 64)),
			max_graphics=int(getattr(settings, "MAX_DOX_GRAPHICS", 8)),
			max_graphics_bytes=int(
				getattr(settings, "MAX_DOX_GRAPHICS_BYTES", 64 * 1024)
			),
			max_controls=int(getattr(settings, "MAX_DOX_CONTROLS", 16)),
			max_control_bytes=int(
				getattr(settings, "MAX_DOX_CONTROL_BYTES", 2 * 1024)
			),
			max_document_bytes=int(
				getattr(settings, "MAX_DOX_DOCUMENT_BYTES", 96 * 1024)
			),
			max_image_width=int(getattr(settings, "MAX_DOX_IMAGE_WIDTH", 160)),
			max_image_height=int(getattr(settings, "MAX_DOX_IMAGE_HEIGHT", 96)),
			max_image_source_bytes=min(
				int(getattr(settings, "MAX_IMAGE_DOWNLOAD_BYTES", 16 * 1024 * 1024)),
				int(getattr(settings, "MAX_INLINE_RESOURCE_BYTES", 2 * 1024 * 1024)),
			),
			max_image_pixels=int(
				getattr(settings, "MAX_IMAGE_PIXELS", 16 * 1024 * 1024)
			),
			max_url_bytes=int(getattr(settings, "MAX_DOX_URL_BYTES", 127)),
		)
	except ValueError as error:
		raise ConfigurationError(f"Invalid DOX limits: {error}") from error

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
		max_markdown_source_bytes=_positive_setting(
			settings, "MAX_MARKDOWN_SOURCE_BYTES", 1024 * 1024
		),
		dox_limits=dox_limits,
		extensions=extensions,
		domain_to_extension=domain_to_extension,
	)
	app.extensions["gb_proxy_runtime"] = runtime
	_register_routes(app, runtime)
	return app


def _pic_output_enabled(runtime):
	settings = runtime.settings
	return bool(settings.CONVERT_IMAGES) and image_extension(
		settings.CONVERT_IMAGES,
		settings.CONVERT_IMAGES_TO_FILETYPE,
	) == "pic"


def _requested_gbpc_mode(runtime):
	"""Select the first advertised supported GBPC mode, defaulting safely."""
	if not _pic_output_enabled(runtime):
		return GBPC_MODE_1
	offer = request.headers.get(_GBPC_REQUEST_HEADER, "")
	parts = tuple(part.strip() for part in offer.split(","))
	if not parts or any(part not in ("1", "7") for part in parts):
		return GBPC_MODE_1
	return GBPC_MODE_7 if parts[0] == "7" else GBPC_MODE_1


def _accepts_symbos_dox():
	"""Return true when DOX is an explicitly accepted representation."""
	for item in request.headers.get("Accept", "").split(","):
		parts = [part.strip() for part in item.split(";")]
		if not parts or parts[0].lower() != DOX_MIMETYPE:
			continue
		quality = 1.0
		for parameter in parts[1:]:
			if parameter.lower().startswith("q="):
				try:
					quality = float(parameter[2:])
				except ValueError:
					quality = 0
		if quality > 0:
			return True
	return False


def _requested_sgx_profile():
	return parse_sgx_profile(request.headers.get(_SGX_REQUEST_HEADER))


def _cache_image(runtime, url, content=None, gbpc_mode=GBPC_MODE_1):
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
		gbpc_mode=gbpc_mode,
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
	response = _send_image_file(runtime, os.path.basename(cached_url))
	if _pic_output_enabled(runtime):
		response.vary.add(_GBPC_REQUEST_HEADER)
	return response


def _handle_image_request(runtime, url, gbpc_mode=GBPC_MODE_1):
	cached_url = _cache_image(runtime, url, gbpc_mode=gbpc_mode)
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


def _header_value(headers, name):
	"""Return one header value from an ordinary or case-insensitive mapping."""
	name = name.lower()
	return next(
		(str(value) for key, value in headers.items() if str(key).lower() == name),
		"",
	)


def _prepare_headers(dox_requested=False):
	headers = {"User-Agent": USER_AGENT}
	if dox_requested:
		headers["Accept"] = _STANDARD_UPSTREAM_ACCEPT
	for name in ("Accept", "Accept-Language", "Referer"):
		if dox_requested and name == "Accept":
			continue
		value = request.headers.get(name)
		if value:
			headers[name] = value
	return headers


def _send_request(runtime, url, append_query=False, dox_requested=False):
	kwargs = {
		"headers": _prepare_headers(dox_requested=dox_requested),
		"allow_redirects": True,
		"timeout": runtime.request_timeout,
		"stream": True,
	}
	if request.method == "POST":
		kwargs["data"] = list(request.form.items(multi=True))
	else:
		kwargs["params"] = list(request.args.items(multi=True)) if append_query else None
	if runtime.request_callable is not None:
		return runtime.request_callable(request.method, url, **kwargs), None
	session = runtime.session_factory()
	try:
		return session.request(request.method, url, **kwargs), session
	except Exception:
		session.close()
		raise


def _handle_target_request(
	runtime,
	url,
	append_query=False,
	gbpc_mode=GBPC_MODE_1,
	sgx_profile=None,
):
	current_app.logger.info("Fetching upstream URL %s", urlparse(url)._replace(query="").geturl())
	response = None
	session = None
	try:
		response, session = _send_request(
			runtime,
			url,
			append_query=append_query,
			dox_requested=sgx_profile is not None,
		)
		final_url = getattr(response, "url", url)
		response_headers = dict(response.headers)
		read_limit = runtime.max_response_bytes
		if is_markdown_response(
			_header_value(response_headers, "Content-Type"), final_url
		):
			read_limit = min(read_limit, runtime.max_markdown_source_bytes)
		content = _read_upstream_content(response, read_limit)
		result = (content, response.status_code, response_headers)
		return _process_response(
			runtime,
			result,
			final_url,
			gbpc_mode=gbpc_mode,
			sgx_profile=sgx_profile,
		)
	except requests.Timeout:
		current_app.logger.warning("Upstream request timed out for %s", url)
		if sgx_profile is not None:
			return _dox_error_response(
				runtime, 504, "Upstream timeout", "The remote server did not respond in time.",
				sgx_profile, url,
			)
		return abort(504, "Upstream request timed out")
	except UpstreamResponseTooLarge as error:
		current_app.logger.warning("%s", error)
		if sgx_profile is not None:
			return _dox_error_response(
				runtime, 502, "Response too large", str(error), sgx_profile, url,
			)
		return abort(502, "Upstream response is too large")
	except HTTPException:
		# Preserve deliberate processing errors raised with Flask's abort().
		# The generic handler below is only for unexpected proxy failures.
		raise
	except requests.RequestException:
		current_app.logger.exception("Upstream request failed for %s", url)
		if sgx_profile is not None:
			return _dox_error_response(
				runtime, 502, "Connection failed", "Could not connect to the remote server.",
				sgx_profile, url,
			)
		return abort(502, "Upstream connection failed")
	except Exception:
		current_app.logger.exception("Unhandled proxy error for %s", url)
		if sgx_profile is not None:
			return _dox_error_response(
				runtime, 500, "Proxy error", "GB-proxy could not build this page.",
				sgx_profile, url,
			)
		return abort(500, "GB-proxy encountered an internal error")
	finally:
		if response is not None and callable(getattr(response, "close", None)):
			response.close()
		if session is not None:
			session.close()


def _fetch_dox_image(runtime, url):
	parsed = urlparse(url)
	if parsed.scheme not in ("http", "https") or not parsed.netloc:
		raise ValueError("DOX image URL must be absolute HTTP or HTTPS")
	response = None
	session = None
	kwargs = {
		"headers": {"User-Agent": USER_AGENT, "Accept": "image/*"},
		"allow_redirects": True,
		"timeout": runtime.request_timeout,
		"stream": True,
	}
	try:
		if runtime.request_callable is not None:
			response = runtime.request_callable("GET", url, **kwargs)
		else:
			session = runtime.session_factory()
			response = session.request("GET", url, **kwargs)
		status = int(getattr(response, "status_code", 200))
		if status < 200 or status >= 300:
			raise ValueError(f"Image server returned HTTP {status}")
		return _read_upstream_content(
			response,
			min(
				runtime.dox_limits.max_image_source_bytes,
				int(getattr(runtime.settings, "MAX_IMAGE_DOWNLOAD_BYTES", 16 * 1024 * 1024)),
			),
		)
	finally:
		if response is not None and callable(getattr(response, "close", None)):
			response.close()
		if session is not None:
			session.close()


def _dox_response(runtime, content, status_code, content_type, url, profile):
	settings = runtime.settings
	arguments = {
		"profile": profile,
		"limits": runtime.dox_limits,
		"dithering": settings.DITHERING_ALGORITHM,
		"svg_timeout": float(getattr(settings, "SVG_CONVERSION_TIMEOUT", 10)),
	}
	media_type = content_type.split(";", 1)[0].strip()
	binary_without_type = (
		not media_type
		and isinstance(content, bytes)
		and (
			b"\x00" in content[:4096]
			or sum(byte < 9 or 13 < byte < 32 for byte in content[:4096])
			> max(4, len(content[:4096]) // 20)
		)
	)
	if media_type.startswith("image/") or is_image_url(url):
		document = build_dox_from_image(content, url, **arguments)
	elif media_type == "text/plain":
		if isinstance(content, bytes):
			content = content.decode("utf-8", errors="replace")
		document = build_dox_from_html(
			f"<html><head><title>Text document</title></head><body><pre>{html.escape(content)}</pre></body></html>",
			url,
			**arguments,
		)
	elif binary_without_type or (
		media_type and media_type not in ("text/html", "application/xhtml+xml")
	):
		description = media_type or "unknown binary"
		return _dox_error_response(
			runtime,
			415,
			"Unsupported content",
			f"SymZilla cannot display {description} content.",
			profile,
			url,
		)
	else:
		document = build_dox_from_html(
			content,
			url,
			image_fetcher=lambda image_url: _fetch_dox_image(runtime, image_url),
			link_shortener=_shorten_dox_link,
			**arguments,
		)
	return _serialized_dox_response(document, status_code)


def _serialized_dox_response(document, status_code):
	# Werkzeug correctly suppresses bodies (and sometimes entity headers) for
	# body-forbidden statuses. SymZilla always needs the generated DOX body, so
	# turn those upstream statuses into a displayable response.
	if 100 <= int(status_code) < 200 or int(status_code) in (204, 205, 304):
		status_code = 200
	result = Response(document, status=status_code, content_type=DOX_MIMETYPE)
	result.headers["Content-Disposition"] = 'inline; filename="document.dox"'
	result.vary.add("Accept")
	result.vary.add(_SGX_REQUEST_HEADER)
	return result


def _shorten_dox_link(target):
	token = register_resource("url", target)
	base_url = current_app.config["GB_PROXY_ADVERTISE_URL"].rstrip("/")
	return f"{base_url}/u/{token}"


def _dox_error_response(runtime, status_code, title, message, profile, url):
	document = build_dox_from_html(
		("<html><head><title>" + html.escape(title) + "</title></head><body><h1>"
		 + html.escape(title) + "</h1><p>" + html.escape(message) + "</p></body></html>"),
		url,
		profile=profile,
		limits=runtime.dox_limits,
		dithering=runtime.settings.DITHERING_ALGORITHM,
		svg_timeout=float(getattr(runtime.settings, "SVG_CONVERSION_TIMEOUT", 10)),
	)
	return _serialized_dox_response(document, status_code)


def _process_response(
	runtime,
	response,
	url,
	gbpc_mode=GBPC_MODE_1,
	sgx_profile=None,
):
	varies_by_gbpc = False
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
		response_content_type = _header_value(response.headers, "Content-Type")
		if sgx_profile is None and not is_markdown_response(response_content_type, url):
			return response
		content = response.get_data()
		status_code = response.status_code
		headers = dict(response.headers)
	else:
		content = response
		status_code = 200
		headers = {}

	content_type = _header_value(headers, "Content-Type")
	if is_markdown_response(content_type, url):
		content_size = (
			len(content.encode("utf-8")) if isinstance(content, str) else len(content)
		)
		if content_size > runtime.max_markdown_source_bytes:
			message = (
				"Markdown source exceeds the "
				f"{runtime.max_markdown_source_bytes}-byte limit"
			)
			if sgx_profile is not None:
				return _dox_error_response(
					runtime, 502, "Response too large", message, sgx_profile, url
				)
			return abort(502, message)
		try:
			content = markdown_to_html(content, content_type, url)
		except MarkdownSafetyError as error:
			if sgx_profile is not None:
				return _dox_error_response(
					runtime,
					502,
					"Markdown too complex",
					str(error),
					sgx_profile,
					url,
				)
			return abort(502, str(error))
		headers = {
			key: value
			for key, value in headers.items()
			if str(key).lower() not in ("content-disposition", "content-type")
		}
		headers["Content-Type"] = "text/html; charset=utf-8"
		content_type = headers["Content-Type"]

	content_type = content_type.lower()
	if sgx_profile is not None:
		return _dox_response(
			runtime,
			content,
			status_code,
			content_type,
			url,
			sgx_profile,
		)

	if content_type.startswith("image/"):
		cached_url = _cache_image(runtime, url, content, gbpc_mode=gbpc_mode)
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
		short_image_urls = getattr(settings, "SHORT_IMAGE_URLS", False)
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
			short_image_urls=short_image_urls,
			ascii_only=getattr(settings, "ASCII_ONLY", False),
			max_image_alt_length=getattr(settings, "MAX_IMAGE_ALT_LENGTH", None),
			gbpc_mode=gbpc_mode,
		)
		varies_by_gbpc = _pic_output_enabled(runtime) and not short_image_urls

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
	if varies_by_gbpc:
		result.vary.add(_GBPC_REQUEST_HEADER)
	return result


def _register_routes(app, runtime):
	@app.errorhandler(HTTPException)
	def handle_http_error(error):
		if not _accepts_symbos_dox():
			return error
		return _dox_error_response(
			runtime,
			error.code or 500,
			error.name,
			error.description,
			_requested_sgx_profile(),
			request.url,
		)

	@app.get("/cached_image/<path:filename>")
	def serve_cached_image(filename):
		return _send_image_file(runtime, filename)

	@app.get("/i/<token>.<extension>")
	def serve_short_image(token, extension):
		gbpc_mode = _requested_gbpc_mode(runtime)
		expected_extension = image_extension(
			runtime.settings.CONVERT_IMAGES,
			runtime.settings.CONVERT_IMAGES_TO_FILETYPE,
		)
		if extension.lower() != expected_extension:
			return abort(404, "Unknown converted image format")
		resource = resolve_resource("image", token)
		if resource is None:
			return abort(404, "Image token has expired")
		cached_url = _cache_image(
			runtime,
			resource.target,
			resource.content,
			gbpc_mode=gbpc_mode,
		)
		if not cached_url:
			return abort(404, "Image could not be processed")
		return _send_cached_image(runtime, cached_url)

	@app.route("/u/<token>", methods=("GET", "POST"))
	def follow_short_url(token):
		resource = resolve_resource("url", token)
		if resource is None:
			return abort(404, "Link token has expired")
		return _handle_target_request(
			runtime,
			resource.target,
			append_query=True,
			gbpc_mode=_requested_gbpc_mode(runtime),
			sgx_profile=_requested_sgx_profile() if _accepts_symbos_dox() else None,
		)

	@app.route("/", defaults={"path": "/"}, methods=("GET", "POST"))
	@app.route("/<path:path>", methods=("GET", "POST"))
	def handle_request(path):
		gbpc_mode = _requested_gbpc_mode(runtime)
		sgx_profile = _requested_sgx_profile() if _accepts_symbos_dox() else None
		if _is_proxy_self_request():
			# The request was addressed to the proxy rather than to an upstream
			# site. Refuse it instead of fetching ourselves recursively.
			if sgx_profile is not None:
				return _dox_error_response(
					runtime,
					400,
					"Not a proxy request",
					"Send an absolute URL through the proxy, for example http://example.com/.",
					sgx_profile,
					request.url,
				)
			return abort(
				400,
				"This is a proxy. Send an absolute URL, for example http://example.com/",
			)
		parsed_url = urlparse(request.url)
		override_response = _handle_override_extension(runtime, parsed_url.scheme)
		if override_response is not None:
			return _process_response(
				runtime,
				override_response,
				request.url,
				gbpc_mode=gbpc_mode,
				sgx_profile=sgx_profile,
			)

		matching_extension = _find_matching_extension(runtime, parsed_url.hostname)
		if matching_extension:
			return _process_response(
				runtime,
				_handle_matching_extension(runtime, matching_extension),
				request.url,
				gbpc_mode=gbpc_mode,
				sgx_profile=sgx_profile,
			)

		if sgx_profile is None and is_image_url(request.url):
			return _handle_image_request(runtime, request.url, gbpc_mode=gbpc_mode)
		return _handle_target_request(
			runtime,
			request.url,
			append_query=False,
			gbpc_mode=gbpc_mode,
			sgx_profile=sgx_profile,
		)
