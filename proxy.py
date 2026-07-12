# Standard library imports
import argparse
import os
import shutil
import socket
from urllib.parse import urlparse

# Third-party imports
import requests
from flask import Flask, request, g, abort, Response, send_from_directory
from werkzeug.serving import get_interface_ip
from werkzeug.wrappers.response import Response as WerkzeugResponse

# First-party imports
from utils.html_utils import transcode_html, transcode_content
from utils.image_utils import (
	CACHE_DIR,
	fetch_and_cache_image,
	image_extension,
	image_mimetype,
	is_image_url,
)
from utils.resource_registry import clear_resources, resolve_resource
from utils.system_utils import load_preset


os.environ['FLASK_ENV'] = 'development'
app = Flask(__name__)
http_session = requests.Session()

HTTP_ERRORS = (403, 404, 500, 503, 504)
ERROR_HEADER = "[[Macproxy Encountered an Error]]"

# Global variable to store the override extension
override_extension = None

# User-Agent string
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

# Call this function every time the proxy starts
def clear_image_cache():
	if os.path.exists(CACHE_DIR):
		shutil.rmtree(CACHE_DIR)
	os.makedirs(CACHE_DIR, exist_ok=True)
	clear_resources()

clear_image_cache()

# Load preset immediately after config import
config = load_preset()

# Now get the settings we need after preset has potentially modified them
ENABLED_EXTENSIONS = config.ENABLED_EXTENSIONS

# Load extensions
extensions = {}
domain_to_extension = {}
print('Enabled Extensions: ')
for ext in ENABLED_EXTENSIONS:
	print(ext)
	module = __import__(f"extensions.{ext}.{ext}", fromlist=[''])
	extensions[ext] = module
	domain_to_extension[module.DOMAIN] = module

@app.route("/cached_image/<path:filename>")
def serve_cached_image(filename):
	return send_image_file(filename)


def cache_image(url, content=None):
	return fetch_and_cache_image(
		url,
		content,
		resize=config.RESIZE_IMAGES,
		max_width=config.MAX_IMAGE_WIDTH,
		max_height=config.MAX_IMAGE_HEIGHT,
		convert=config.CONVERT_IMAGES,
		convert_to=config.CONVERT_IMAGES_TO_FILETYPE,
		dithering=config.DITHERING_ALGORITHM,
	)


def send_image_file(filename):
	if filename != os.path.basename(filename):
		return abort(404, "Image not found")
	if not getattr(config, "MINIMAL_RESPONSE_HEADERS", False):
		return send_from_directory(CACHE_DIR, filename, mimetype=image_mimetype(filename))
	path = os.path.join(CACHE_DIR, filename)
	try:
		with open(path, "rb") as image_file:
			content = image_file.read()
	except OSError:
		return abort(404, "Image not found")
	return Response(content, status=200, mimetype=image_mimetype(filename))


def send_cached_image(cached_url):
	return send_image_file(os.path.basename(cached_url))

def handle_image_request(url):
	cached_url = cache_image(url)
	if cached_url:
		return send_cached_image(cached_url)
	return abort(404, "Image not found or could not be processed")


@app.route("/i/<token>.<extension>")
def serve_short_image(token, extension):
	expected_extension = image_extension(config.CONVERT_IMAGES, config.CONVERT_IMAGES_TO_FILETYPE)
	if extension.lower() != expected_extension:
		return abort(404, "Unknown converted image format")
	resource = resolve_resource("image", token)
	if resource is None:
		return abort(404, "Image token has expired")
	cached_url = cache_image(resource.target, resource.content)
	if not cached_url:
		return abort(404, "Image could not be processed")
	return send_cached_image(cached_url)


@app.route("/u/<token>", methods=["GET", "POST"])
def follow_short_url(token):
	resource = resolve_resource("url", token)
	if resource is None:
		return abort(404, "Link token has expired")
	return handle_target_request(resource.target, append_query=True)

@app.route("/", defaults={"path": "/"}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def handle_request(path):
	global override_extension
	parsed_url = urlparse(request.url)
	scheme = parsed_url.scheme
	host = parsed_url.netloc.split(':')[0]  # Remove port if present
	
	if override_extension:
		print(f'Current override extension: {override_extension}')

	override_response = handle_override_extension(scheme)
	if override_response is not None:
		return process_response(override_response, request.url)

	matching_extension = find_matching_extension(host)
	if matching_extension:
		response = handle_matching_extension(matching_extension)
		return process_response(response, request.url)
	
	# Only handle image requests here if we're not using an extension
	if is_image_url(request.url) and not (override_extension or matching_extension):
		return handle_image_request(request.url)

	return handle_default_request()

def handle_override_extension(scheme):
	global override_extension
	if override_extension:
		extension_name = override_extension.split('.')[-1]
		if extension_name in extensions:
			if scheme in ['http', 'https', 'ftp']:
				response = extensions[extension_name].handle_request(request)
				check_override_status(extension_name)
				return response
			else:
				print(f"Warning: Unsupported scheme '{scheme}' for override extension.")
		else:
			print(f"Warning: Override extension '{extension_name}' not found. Resetting override.")
			override_extension = None
	return None  # Return None if no override is active

def check_override_status(extension_name):
	global override_extension
	if hasattr(extensions[extension_name], 'get_override_status') and not extensions[extension_name].get_override_status():
		override_extension = None
		print("Override disabled")

def find_matching_extension(host):
	for domain, extension in domain_to_extension.items():
		if host.endswith(domain):
			return extension
	return None

def handle_matching_extension(matching_extension):
	global override_extension
	print(f"Handling request with matching extension: {matching_extension.__name__}")
	response = matching_extension.handle_request(request)
	
	if hasattr(matching_extension, 'get_override_status') and matching_extension.get_override_status():
		override_extension = matching_extension.__name__
		print(f"Override enabled for {override_extension}")
	
	return response

def process_response(response, url):
	print(f"Processing response for URL: {url}")

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
	print(f"Content-Type: {content_type}")

	if content_type.startswith('image/'):
		cached_url = cache_image(url, content)
		if cached_url:
			return send_cached_image(cached_url)
		return abort(404, "Image could not be processed")

	# Handle CSS and JavaScript
	if content_type in ['text/css', 'text/javascript', 'application/javascript', 'application/x-javascript']:
		content = transcode_content(content)
		response = Response(content, status_code)
		response.headers['Content-Type'] = content_type
		return response

	# List of content types that should not be transcoded
	non_transcode_types = [
		'application/octet-stream',
		'application/pdf',
		'application/zip',
		'application/x-zip-compressed',
		'application/x-rar-compressed',
		'application/x-tar',
		'application/x-gzip',
		'application/x-bzip2',
		'application/x-7z-compressed',
		'application/mac-binary',
		'application/macbinary',
		'application/x-binary',
		'application/x-macbinary',
		'application/binhex',
		'application/binhex4',
		'application/mac-binhex',
		'application/mac-binhex40',
		'application/x-binhex40',
		'application/x-mac-binhex40',
		'application/x-sit',
		'application/x-stuffit',
		'application/vnd.openxmlformats-officedocument',
		'application/vnd.ms-excel',
		'application/vnd.ms-powerpoint',
		'application/msword',
		'audio/',
		'video/',
		'text/plain'
	]

	# Check if content type is in the list of non-transcode types
	should_transcode = not any(content_type.startswith(t) for t in non_transcode_types)

	if should_transcode:
		print("Transcoding content")
		if isinstance(content, bytes):
			content = content.decode('utf-8', errors='replace')
		content = transcode_html(
			content,
			url,
			whitelisted_domains=config.WHITELISTED_DOMAINS,
			simplify_html=config.SIMPLIFY_HTML,
			tags_to_unwrap=config.TAGS_TO_UNWRAP,
			tags_to_strip=config.TAGS_TO_STRIP,
			attributes_to_strip=config.ATTRIBUTES_TO_STRIP,
			convert_characters=config.CONVERT_CHARACTERS,
			conversion_table=config.CONVERSION_TABLE,
			allowed_tags=getattr(config, "ALLOWED_HTML_TAGS", None),
			allowed_attributes=getattr(config, "ALLOWED_HTML_ATTRIBUTES", None),
			shorten_link_urls=getattr(config, "SHORTEN_LINK_URLS", False),
			short_image_urls=getattr(config, "SHORT_IMAGE_URLS", False),
			ascii_only=getattr(config, "ASCII_ONLY", False),
			max_image_alt_length=getattr(config, "MAX_IMAGE_ALT_LENGTH", None),
		)
	else:
		print(f"Content type {content_type} should not be transcoded, passing through unchanged")

	response = Response(content, status_code)
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
		if getattr(config, "MINIMAL_RESPONSE_HEADERS", False) and lower_key not in minimal_headers:
			continue
		response.headers[key] = value

	print("Finished processing response")
	return response

def handle_default_request():
	url = request.url.replace("https://", "http://", 1)
	return handle_target_request(url, append_query=False)


def handle_target_request(url, append_query=False):
	headers = prepare_headers()
	print(f"Handling default request for URL: {url}")

	try:
		resp = send_request(url, headers, append_query=append_query)
		content = resp.content
		status_code = resp.status_code
		headers = dict(resp.headers)
		return process_response((content, status_code, headers), resp.url)
	except requests.exceptions.ConnectionError as e:
		error_args = str(e.args)
		if any(keyword in error_args for keyword in ["NameResolutionError", "nodename nor servname provided", "Failed to resolve"]):
			print(f"DNS lookup failed for {url}")
			return abort(502, f"DNS lookup failed for {url}. Please check the domain name.")
		else:
			print(f"Connection error for {url}: {str(e)}")
			return abort(502, f"Connection error: {str(e)}")
	except Exception as e:
		print(f"Error in handle_default_request: {str(e)}")
		return abort(500, ERROR_HEADER + str(e))

def prepare_headers():
	headers = {
		"Accept": request.headers.get("Accept"),
		"Accept-Language": request.headers.get("Accept-Language"),
		"Referer": request.headers.get("Referer"),
		"User-Agent": USER_AGENT,
	}
	return headers

def send_request(url, headers, append_query=False):
	print(f"Sending request to: {url}")
	if request.method == "POST":
		return http_session.post(url, data=request.form, headers=headers, allow_redirects=True)
	params = request.args if append_query else None
	return http_session.get(url, params=params, headers=headers, allow_redirects=True)

@app.after_request
def apply_caching(resp):
	try:
		resp.headers["Content-Type"] = g.content_type
	except:
		pass
	return resp

def get_proxy_hostname(hostname):
	# Based on the `log_startup` function from werkzeug.serving.
	# Translates a "bind all addresses" string into a real IP
	# (or returns the hostname if one was set)
	if hostname == "0.0.0.0":
		display_hostname = get_interface_ip(socket.AF_INET)
	elif hostname == "::":
		display_hostname = get_interface_ip(socket.AF_INET6)
	else:
		display_hostname = hostname
	return display_hostname

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Macproxy command line arguments")
	parser.add_argument(
		"--host",
		type=str,
		default="0.0.0.0",
		action="store",
		help="Host IP the web server will run on",
	)
	parser.add_argument(
		"--port",
		type=int,
		default=5001,
		action="store",
		help="Port number the web server will run on",
	)
	arguments = parser.parse_args()

	# Translate the bind address (typically 0.0.0.0 or ::) to a friendly
	# hostname / IP, and store it and the port in the application config
	# object. This will be used if we need to generate URLs to the proxy itself
	# in the HTML (as opposed to the site we are proxying the request to).
	app.config['MACPROXY_HOST_AND_PORT'] = f"{get_proxy_hostname(arguments.host)}:{arguments.port}"

	app.run(host=arguments.host, port=arguments.port, debug=False)
