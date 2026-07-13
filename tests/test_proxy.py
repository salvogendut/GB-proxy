import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
		self.assertNotIn("ETag", response.headers)
		self.assertNotIn("Last-Modified", response.headers)

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
