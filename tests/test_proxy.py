import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

from bs4 import BeautifulSoup
from PIL import Image

from tests.config_stub import install_config


install_config()

from gb_proxy.application import create_app
from utils.resource_registry import clear_resources, register_resource


class GeobenchProxyRouteTests(unittest.TestCase):
	def setUp(self):
		self.temp_directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp_directory.cleanup)
		self.app = create_app(
			install_config(),
			cache_dir=self.temp_directory.name,
			state_dir=self.temp_directory.name,
			advertise_url="http://127.0.0.1:5001",
		)
		self.runtime = self.app.extensions["gb_proxy_runtime"]
		clear_resources()
		self.client = self.app.test_client()

	def test_short_image_route_returns_gbpc(self):
		image = Image.new("RGB", (8, 4), (255, 0, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		token = register_resource("image", "inline-image:test", buffer.getvalue())

		response = self.client.get(f"/i/{token}.pic")
		self.addCleanup(response.close)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content_type, "image/x-geobench-pic")
		self.assertEqual(response.data[:6], b"GBPC\x02\x01")
		self.assertEqual(response.headers["Vary"], "X-GBPC")
		self.assertNotIn("ETag", response.headers)
		self.assertNotIn("Last-Modified", response.headers)

	def test_short_image_route_negotiates_mode7_and_separates_cache(self):
		image = Image.new("RGB", (8, 4), (0, 255, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		token = register_resource("image", "inline-image:negotiated", buffer.getvalue())

		mode1 = self.client.get(f"/i/{token}.pic")
		mode7 = self.client.get(f"/i/{token}.pic", headers={"X-GBPC": "7,1"})
		self.addCleanup(mode1.close)
		self.addCleanup(mode7.close)

		self.assertEqual(mode1.data[:6], b"GBPC\x02\x01")
		self.assertEqual(mode7.data[:6], b"GBPC\x02\x07")
		self.assertEqual(mode7.headers["Vary"], "X-GBPC")
		self.assertEqual(len([name for name in os.listdir(self.temp_directory.name) if name.endswith(".pic")]), 2)

	def test_unknown_gbpc_offer_defaults_to_mode1(self):
		image = Image.new("RGB", (8, 4), (255, 0, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		token = register_resource("image", "inline-image:unknown-mode", buffer.getvalue())

		response = self.client.get(
			f"/i/{token}.pic",
			headers={"X-GBPC": "8,7"},
		)
		self.addCleanup(response.close)

		self.assertEqual(response.data[:6], b"GBPC\x02\x01")

	def test_gbpc_offer_does_not_change_non_pic_output(self):
		image = Image.new("RGB", (8, 4), (0, 255, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		token = register_resource("image", "inline-image:gif", buffer.getvalue())

		with patch.object(self.runtime.settings, "CONVERT_IMAGES_TO_FILETYPE", "gif"):
			response = self.client.get(
				f"/i/{token}.gif",
				headers={"X-GBPC": "7,1"},
			)
		self.addCleanup(response.close)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.data.startswith(b"GIF8"))
		self.assertNotIn("Vary", response.headers)

	def test_direct_image_response_negotiates_mode7_without_forwarding_header(self):
		image = Image.new("RGB", (8, 4), (0, 255, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		upstream = SimpleNamespace(
			content=buffer.getvalue(),
			status_code=200,
			headers={"Content-Type": "image/png"},
			url="http://example.com/art",
		)

		with patch.object(self.runtime, "request_callable", return_value=upstream) as request_get:
			response = self.client.get(
				"/art",
				base_url="http://example.com",
				headers={"X-GBPC": "7,1"},
			)
		self.addCleanup(response.close)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.data[:6], b"GBPC\x02\x07")
		self.assertEqual(response.headers["Vary"], "X-GBPC")
		self.assertNotIn("X-GBPC", request_get.call_args.kwargs["headers"])

	def test_eager_inline_svg_negotiates_mode7_and_varies_html(self):
		upstream = SimpleNamespace(
			content=(
				b'<html><body><svg viewBox="0 0 8 4">'
				b'<rect width="8" height="4" fill="green"/></svg></body></html>'
			),
			status_code=200,
			headers={"Content-Type": "text/html"},
			url="http://example.com/inline",
		)
		rendered_svg = Image.new("RGB", (8, 4), (0, 255, 0))

		with patch.object(
			self.runtime.settings,
			"SHORT_IMAGE_URLS",
			False,
		), patch.object(
			self.runtime,
			"request_callable",
			return_value=upstream,
		), patch(
			"utils.image_utils._render_svg",
			return_value=rendered_svg,
		):
			mode1_page = self.client.get("/inline", base_url="http://example.com")
			mode7_page = self.client.get(
				"/inline",
				base_url="http://example.com",
				headers={"X-GBPC": "7,1"},
			)
		self.addCleanup(mode1_page.close)
		self.addCleanup(mode7_page.close)

		mode1_source = BeautifulSoup(mode1_page.data, "html.parser").img["src"]
		mode7_source = BeautifulSoup(mode7_page.data, "html.parser").img["src"]
		self.assertNotEqual(mode1_source, mode7_source)
		self.assertEqual(mode1_page.headers["Vary"], "X-GBPC")
		self.assertEqual(mode7_page.headers["Vary"], "X-GBPC")

		mode1_image = self.client.get(urlparse(mode1_source).path)
		mode7_image = self.client.get(urlparse(mode7_source).path)
		self.addCleanup(mode1_image.close)
		self.addCleanup(mode7_image.close)
		self.assertEqual(mode1_image.data[:6], b"GBPC\x02\x01")
		self.assertEqual(mode7_image.data[:6], b"GBPC\x02\x07")

	def test_short_get_form_appends_query_once(self):
		token = register_resource("url", "https://search.example/find?source=gb")
		upstream = SimpleNamespace(
			content=b"<html><body>ok</body></html>",
			status_code=200,
			headers={"Content-Type": "text/html", "Server": "upstream", "X-Upstream": "remove-me"},
			url="https://search.example/find?source=gb&q=retro",
		)

		with patch.object(self.runtime, "request_callable", return_value=upstream) as request_get:
			response = self.client.get(f"/u/{token}?q=retro")

		self.assertEqual(response.status_code, 200)
		self.assertNotIn("X-Upstream", response.headers)
		call = request_get.call_args
		self.assertEqual(call.args[:2], ("GET", "https://search.example/find?source=gb"))
		self.assertEqual(call.kwargs["params"].get("q"), "retro")
		self.assertEqual(call.kwargs["timeout"], (5.0, 30.0))

	def test_direct_get_does_not_duplicate_existing_query(self):
		upstream = SimpleNamespace(
			content=b"<html><body>ok</body></html>",
			status_code=200,
			headers={"Content-Type": "text/html"},
			url="http://example.com/search?q=once",
		)

		with patch.object(self.runtime, "request_callable", return_value=upstream) as request_get:
			response = self.client.get("/search?q=once", base_url="http://example.com")

		self.assertEqual(response.status_code, 200)
		call = request_get.call_args
		self.assertEqual(call.args[:2], ("GET", "http://example.com/search?q=once"))
		self.assertIsNone(call.kwargs["params"])


if __name__ == "__main__":
	unittest.main()
