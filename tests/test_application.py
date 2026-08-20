import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import requests
from PIL import Image

from gb_proxy.application import create_app, domain_matches
from tests.config_stub import install_config
from utils.dox_utils import DOX_MIMETYPE, validate_dox
from utils.image_utils import SYMBOS_PALETTE
from utils.resource_registry import resolve_resource
from utils.system_utils import ConfigurationError


class ApplicationFactoryTests(unittest.TestCase):
	def test_imports_do_not_require_config_or_write_runtime_files(self):
		repository_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		with tempfile.TemporaryDirectory() as home:
			environment = os.environ.copy()
			environment.pop("GB_PROXY_CONFIG", None)
			environment.pop("GB_PROXY_CACHE_DIR", None)
			environment.pop("GB_PROXY_STATE_DIR", None)
			environment.update(
				HOME=home,
				PYTHONDONTWRITEBYTECODE="1",
				PYTHONPATH=repository_root,
			)
			result = subprocess.run(
				[sys.executable, "-c", "import proxy, utils.html_utils, utils.image_utils"],
				cwd=home,
				env=environment,
				capture_output=True,
				text=True,
				check=False,
			)

			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertEqual(os.listdir(home), [])

	def test_domain_matching_requires_a_label_boundary(self):
		self.assertTrue(domain_matches("reddit.com", "reddit.com"))
		self.assertTrue(domain_matches("old.reddit.com", "reddit.com"))
		self.assertFalse(domain_matches("evilreddit.com", "reddit.com"))

	def test_factory_uses_explicit_runtime_directories_and_advertised_url(self):
		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(),
				cache_dir=f"{directory}/cache",
				state_dir=f"{directory}/state",
				advertise_url="http://192.0.2.10:5001",
			)

		self.assertEqual(app.config["GB_PROXY_ADVERTISE_URL"], "http://192.0.2.10:5001")
		self.assertEqual(app.config["MACPROXY_HOST_AND_PORT"], "192.0.2.10:5001")

	def test_factory_does_not_clear_an_existing_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			cache_dir = os.path.join(directory, "cache")
			os.makedirs(cache_dir)
			sentinel = os.path.join(cache_dir, "sentinel")
			with open(sentinel, "w", encoding="utf-8") as sentinel_file:
				sentinel_file.write("keep")

			create_app(
				install_config(),
				cache_dir=cache_dir,
				state_dir=os.path.join(directory, "state"),
			)

			self.assertTrue(os.path.isfile(sentinel))

	def test_oversized_upstream_response_is_rejected(self):
		with tempfile.TemporaryDirectory() as directory:
			config = install_config()
			original_limit = getattr(config, "MAX_UPSTREAM_RESPONSE_BYTES", None)
			config.MAX_UPSTREAM_RESPONSE_BYTES = 3
			upstream = SimpleNamespace(
				content=b"four",
				status_code=200,
				headers={"Content-Type": "text/plain"},
				url="http://example.com/",
			)
			app = create_app(
				config,
				cache_dir=directory,
				state_dir=directory,
				request_callable=lambda *args, **kwargs: upstream,
			)
			response = app.test_client().get("/", base_url="http://example.com")
			if original_limit is None:
				del config.MAX_UPSTREAM_RESPONSE_BYTES
			else:
				config.MAX_UPSTREAM_RESPONSE_BYTES = original_limit

		self.assertEqual(response.status_code, 502)

	def test_each_inbound_request_uses_and_closes_a_fresh_session(self):
		created_sessions = []

		class FakeSession:
			def __init__(self):
				self.closed = False
				created_sessions.append(self)

			def request(self, method, url, **kwargs):
				return SimpleNamespace(
					content=b"ok",
					status_code=200,
					headers={"Content-Type": "text/plain"},
					url=url,
				)

			def close(self):
				self.closed = True

		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				session_factory=FakeSession,
			)
			client = app.test_client()
			self.assertEqual(client.get("/one", base_url="http://example.com").status_code, 200)
			self.assertEqual(client.get("/two", base_url="http://example.com").status_code, 200)

		self.assertEqual(len(created_sessions), 2)
		self.assertTrue(all(session.closed for session in created_sessions))

	def test_upstream_timeout_returns_gateway_timeout(self):
		def timeout_request(*args, **kwargs):
			raise requests.Timeout("too slow")

		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				request_callable=timeout_request,
			)
			response = app.test_client().get("/", base_url="http://example.com")

		self.assertEqual(response.status_code, 504)

	def test_factory_rejects_dox_limits_that_exceed_symzilla_loader_bounds(self):
		config = install_config()
		for name, value in (
			("MAX_DOX_TEXT_BYTES", 5),
			("MAX_DOX_TEXT_BYTES", 11765),
			("MAX_DOX_GRAPHICS", 128),
			("MAX_DOX_GRAPHICS_BYTES", 39),
			("MAX_DOX_IMAGE_WIDTH", 7),
			("MAX_DOX_IMAGE_HEIGHT", 255),
			("MAX_DOX_DOCUMENT_BYTES", 1000),
			("MAX_DOX_DOCUMENT_BYTES", 96 * 1024 + 1),
			("MAX_DOX_URL_BYTES", 128),
		):
			with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
				had_value = hasattr(config, name)
				old_value = getattr(config, name, None)
				setattr(config, name, value)
				try:
					with self.assertRaises(ConfigurationError):
						create_app(config, cache_dir=directory, state_dir=directory)
				finally:
					if had_value:
						setattr(config, name, old_value)
					else:
						delattr(config, name)


class SymzillaDoxApplicationTests(unittest.TestCase):
	@staticmethod
	def _png(indexes, width):
		image = Image.new("RGB", (width, 1))
		image.putdata([SYMBOS_PALETTE[index] for index in indexes])
		output = io.BytesIO()
		image.save(output, format="PNG")
		return output.getvalue()

	def _request(self, upstream, headers=None):
		with tempfile.TemporaryDirectory() as directory:
			calls = []

			def send(method, url, **kwargs):
				calls.append((method, url, kwargs))
				return upstream(method, url, **kwargs) if callable(upstream) else upstream

			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				request_callable=send,
			)
			response = app.test_client().get(
				"/page",
				base_url="http://example.com",
				headers=headers or {},
			)
			return response, calls

	def test_origin_only_absolute_uri_is_handled_without_a_flask_redirect(self):
		calls = []
		upstream = SimpleNamespace(
			content=b"<html><body>FrogFind</body></html>",
			status_code=200,
			headers={"Content-Type": "text/html"},
			url="http://frogfind.au/",
		)

		with tempfile.TemporaryDirectory() as directory:
			def send(method, url, **kwargs):
				calls.append((method, url, kwargs))
				return upstream

			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				request_callable=send,
			)
			response = app.test_client().open(
				"http://frogfind.au",
				headers={
					"Accept": DOX_MIMETYPE,
					"X-GB-SGX": "5,16",
				},
				follow_redirects=False,
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content_type, DOX_MIMETYPE)
		self.assertNotIn("Location", response.headers)
		self.assertEqual(calls[0][1], "http://frogfind.au/")
		self.assertIn(b"FrogFind", validate_dox(response.data)[b"TEXT"])

	def test_request_addressed_to_the_proxy_is_refused_without_upstream_fetch(self):
		with tempfile.TemporaryDirectory() as directory:
			calls = []

			def send(method, url, **kwargs):
				calls.append((method, url, kwargs))
				return SimpleNamespace(
					content=b"<html><body>Never</body></html>",
					status_code=200,
					headers={"Content-Type": "text/html"},
					url=url,
				)

			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				advertise_url="http://192.0.2.10:5001",
				request_callable=send,
			)
			response = app.test_client().get("/", base_url="http://192.0.2.10:5001")

		self.assertEqual(response.status_code, 400)
		self.assertEqual(calls, [])

	def test_request_to_loopback_address_is_refused_without_upstream_fetch(self):
		with tempfile.TemporaryDirectory() as directory:
			calls = []

			def send(method, url, **kwargs):
				calls.append((method, url, kwargs))
				return SimpleNamespace(
					content=b"<html><body>Never</body></html>",
					status_code=200,
					headers={"Content-Type": "text/html"},
					url=url,
				)

			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				request_callable=send,
			)
			response = app.test_client().get("/", base_url="http://127.0.0.1:5001")

		self.assertEqual(response.status_code, 400)
		self.assertEqual(calls, [])

	def test_explicit_accept_selects_dox_and_consumes_capability_headers(self):
		upstream = SimpleNamespace(
			content=b"<html><head><title>Test</title></head><body>Hello</body></html>",
			status_code=200,
			headers={"Content-Type": "text/html"},
			url="https://example.com/final",
		)
		response, calls = self._request(upstream, headers={
			"Accept": f"text/html, {DOX_MIMETYPE}",
			"X-GB-SGX": "5,16",
		})

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content_type, DOX_MIMETYPE)
		self.assertEqual(int(response.headers["Content-Length"]), len(response.data))
		self.assertEqual(response.headers["Vary"], "Accept, X-GB-SGX")
		self.assertEqual(response.headers["Content-Disposition"], 'inline; filename="document.dox"')
		self.assertIn(b"Hello", validate_dox(response.data)[b"TEXT"])
		self.assertNotIn(DOX_MIMETYPE, calls[0][2]["headers"]["Accept"])
		self.assertIn("text/html", calls[0][2]["headers"]["Accept"])
		self.assertNotIn("X-GB-SGX", calls[0][2]["headers"])

	def test_missing_or_malformed_sgx_header_uses_two_colour_sgx0(self):
		image = self._png((0, 1, 0, 1, 1, 0, 1, 0), 8)
		upstream = SimpleNamespace(
			content=image,
			status_code=200,
			headers={"Content-Type": "image/png"},
			url="http://example.com/image.png",
		)
		for value in (None, "0", "5", "bad"):
			with self.subTest(value=value):
				headers = {"Accept": DOX_MIMETYPE}
				if value is not None:
					headers["X-GB-SGX"] = value
				response, _ = self._request(upstream, headers=headers)
				graphics = validate_dox(response.data)[b"GRPH"]
				entry_length = struct.unpack_from("<H", graphics, 1)[0]
				entry = graphics[3:3 + entry_length]
				self.assertEqual(entry, bytes.fromhex("400002000800010050a0"))

	def test_valid_sgx5_header_embeds_sixteen_colour_direct_image(self):
		upstream = SimpleNamespace(
			content=self._png((0, 5, 10, 15), 4),
			status_code=200,
			headers={"Content-Type": "image/png"},
			url="http://example.com/image.png",
		)
		response, _ = self._request(upstream, headers={
			"Accept": DOX_MIMETYPE,
			"X-GB-SGX": "5,16",
		})
		graphics = validate_dox(response.data)[b"GRPH"]

		self.assertEqual(graphics[3:], bytes.fromhex("400502000400010005af"))

	def test_valid_sgx0_four_colour_header_preserves_all_four_pens(self):
		upstream = SimpleNamespace(
			content=self._png((0, 1, 2, 3, 3, 2, 1, 0), 8),
			status_code=200,
			headers={"Content-Type": "image/png"},
			url="http://example.com/image.png",
		)
		response, _ = self._request(upstream, headers={
			"Accept": DOX_MIMETYPE,
			"X-GB-SGX": "0,4",
		})
		graphics = validate_dox(response.data)[b"GRPH"]

		self.assertEqual(graphics[3:], bytes.fromhex("400002000800010053ac"))

	def test_html_images_are_fetched_eagerly_without_symzilla_accept_header(self):
		image = self._png((0, 1, 0, 1, 1, 0, 1, 0), 8)

		def upstream(method, url, **kwargs):
			if url == "http://example.com/logo.png":
				return SimpleNamespace(
					content=image,
					status_code=200,
					headers={"Content-Type": "image/png"},
					url=url,
				)
			return SimpleNamespace(
				content=b'<html><body><img src="/logo.png" alt="logo"></body></html>',
				status_code=200,
				headers={"Content-Type": "text/html"},
				url=url,
			)

		response, calls = self._request(upstream, headers={"Accept": DOX_MIMETYPE})
		chunks = validate_dox(response.data)

		self.assertEqual(chunks[b"GRPH"][0], 1)
		self.assertEqual([call[1] for call in calls], [
			"http://example.com/page", "http://example.com/logo.png",
		])
		self.assertNotIn(DOX_MIMETYPE, calls[1][2]["headers"]["Accept"])

	def test_q_zero_dox_accept_keeps_existing_html_behavior(self):
		upstream = SimpleNamespace(
			content=b"<html><body>ordinary</body></html>",
			status_code=200,
			headers={"Content-Type": "text/html"},
			url="http://example.com/page",
		)
		accept = f"text/html, {DOX_MIMETYPE};q=0"
		response, calls = self._request(upstream, headers={"Accept": accept})

		self.assertTrue(response.data.startswith(b"<html"))
		self.assertNotEqual(response.content_type, DOX_MIMETYPE)
		self.assertEqual(calls[0][2]["headers"]["Accept"], accept)

	def test_links_use_short_proxy_urls_but_retain_https_targets(self):
		upstream = SimpleNamespace(
			content=(
				b'<html><body><a href="https://destination.example/a/very/long/path?'
				b'query=abcdefghijklmnopqrstuvwxyz">Destination</a></body></html>'
			),
			status_code=200,
			headers={"Content-Type": "text/html"},
			url="https://example.com/page",
		)
		response, _ = self._request(upstream, headers={"Accept": DOX_MIMETYPE})
		links = validate_dox(response.data)[b"LINK"]
		entry_length = struct.unpack_from("<H", links, 1)[0]
		url = links[4:3 + entry_length].rstrip(b"\x00").decode("ascii")
		token = url.rsplit("/", 1)[-1]

		self.assertLessEqual(len(url), 127)
		self.assertTrue(url.startswith("http://127.0.0.1:5001/u/"))
		self.assertEqual(
			resolve_resource("url", token).target,
			"https://destination.example/a/very/long/path?query=abcdefghijklmnopqrstuvwxyz",
		)

	def test_binary_content_returns_a_negotiated_dox_error(self):
		upstream = SimpleNamespace(
			content=b"%PDF-binary\x00data",
			status_code=200,
			headers={"Content-Type": "application/pdf"},
			url="http://example.com/manual.pdf",
		)
		response, _ = self._request(upstream, headers={"Accept": DOX_MIMETYPE})
		text = validate_dox(response.data)[b"TEXT"]

		self.assertEqual(response.status_code, 415)
		self.assertEqual(response.content_type, DOX_MIMETYPE)
		self.assertIn(b"Unsupported content", text)
		self.assertIn(b"application/pdf", text)

	def test_upstream_failure_returns_a_negotiated_dox_error(self):
		def timeout(*args, **kwargs):
			raise requests.Timeout("slow")

		response, _ = self._request(timeout, headers={"Accept": DOX_MIMETYPE})
		text = validate_dox(response.data)[b"TEXT"]

		self.assertEqual(response.status_code, 504)
		self.assertEqual(response.content_type, DOX_MIMETYPE)
		self.assertIn(b"Upstream timeout", text)
		self.assertEqual(response.headers["Vary"], "Accept, X-GB-SGX")

	def test_body_forbidden_upstream_status_is_normalized_for_dox(self):
		for status in (101, 204, 205, 304):
			with self.subTest(status=status):
				upstream = SimpleNamespace(
					content=b"",
					status_code=status,
					headers={"Content-Type": "text/html"},
					url="http://example.com/empty",
				)
				response, _ = self._request(
					upstream, headers={"Accept": DOX_MIMETYPE}
				)

				self.assertEqual(response.status_code, 200)
				self.assertEqual(response.content_type, DOX_MIMETYPE)
				validate_dox(response.data)

	def test_flask_http_errors_are_returned_as_dox(self):
		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(), cache_dir=directory, state_dir=directory
			)
			response = app.test_client().get(
				"/u/not-a-token", headers={"Accept": DOX_MIMETYPE}
			)

		self.assertEqual(response.status_code, 404)
		self.assertEqual(response.content_type, DOX_MIMETYPE)
		self.assertEqual(int(response.headers["Content-Length"]), len(response.data))
		self.assertIn(b"Not Found", validate_dox(response.data)[b"TEXT"])


if __name__ == "__main__":
	unittest.main()
