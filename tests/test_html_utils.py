import unittest

from bs4 import BeautifulSoup
from flask import Flask

from tests.config_stub import install_config


config = install_config()

from presets.geobench import geobench as preset
from utils.html_utils import transcode_html
from utils.resource_registry import clear_resources, resolve_resource


class GeobenchHtmlTests(unittest.TestCase):
	def setUp(self):
		clear_resources()
		self.app = Flask(__name__)
		self.app.config["MACPROXY_HOST_AND_PORT"] = "192.168.1.2:5001"
		self.app.add_url_rule("/i/<token>.<extension>", endpoint="serve_short_image", view_func=lambda token, extension: "")
		self.app.add_url_rule("/u/<token>", endpoint="follow_short_url", view_func=lambda token: "")

	def transcode(self, document):
		with self.app.test_request_context("/"):
			return transcode_html(
				document,
				"https://example.com/base/page.html",
				whitelisted_domains=[],
				simplify_html=True,
				tags_to_unwrap=preset.TAGS_TO_UNWRAP,
				tags_to_strip=preset.TAGS_TO_STRIP,
				attributes_to_strip=preset.ATTRIBUTES_TO_STRIP,
				convert_characters=True,
				conversion_table=config.CONVERSION_TABLE,
				allowed_tags=preset.ALLOWED_HTML_TAGS,
				allowed_attributes=preset.ALLOWED_HTML_ATTRIBUTES,
				shorten_link_urls=True,
				short_image_urls=True,
				ascii_only=True,
			)

	def test_long_resources_become_short_urls_and_forms_survive(self):
		document = """
			<!doctype html><html><body class="layout">
			<script>window.bad = true;</script>
			<h1>Caf\u00e9 \u2014 search</h1>
			<a href="/article?tracking=abcdefghijklmnopqrstuvwxyz">Read</a>
			<form action="https://search.example/find" method="GET">
				<input type="text" name="q" value="">
			</form>
			<img data-src="//cdn.example/images/abcdefghijklmnopqrstuvwxyz/photo.png" alt="Photo" width="900">
			</body></html>
		"""

		output = self.transcode(document).decode("ascii")
		soup = BeautifulSoup(output, "html.parser")

		self.assertTrue(output.startswith("<html>"))
		self.assertIsNone(soup.find("script"))
		self.assertEqual(soup.h1.get_text(), "Cafe - search")
		self.assertIsNotNone(soup.form.find("input", {"name": "q"}))
		self.assertNotIn("class", soup.body.attrs)
		self.assertNotIn("width", soup.img.attrs)

		image_url = soup.img["src"]
		self.assertLess(len(image_url), 64)
		self.assertTrue(image_url.startswith("http://192.168.1.2:5001/i/"))
		image_token = image_url.rsplit("/", 1)[1].split(".", 1)[0]
		self.assertEqual(
			resolve_resource("image", image_token).target,
			"https://cdn.example/images/abcdefghijklmnopqrstuvwxyz/photo.png",
		)

		link_token = soup.a["href"].rsplit("/", 1)[1]
		self.assertEqual(
			resolve_resource("url", link_token).target,
			"https://example.com/article?tracking=abcdefghijklmnopqrstuvwxyz",
		)
		form_token = soup.form["action"].rsplit("/", 1)[1]
		self.assertEqual(resolve_resource("url", form_token).target, "https://search.example/find")

	def test_inline_svg_is_registered_lazily(self):
		output = self.transcode(
			'<p>Logo</p><svg viewBox="0 0 8 4"><rect width="8" height="4" fill="red"/></svg>'
		).decode("ascii")
		soup = BeautifulSoup(output, "html.parser")
		image_url = soup.img["src"]
		token = image_url.rsplit("/", 1)[1].split(".", 1)[0]
		resource = resolve_resource("image", token)

		self.assertTrue(resource.target.startswith("inline-svg:"))
		self.assertIn(b"<svg", resource.content)


if __name__ == "__main__":
	unittest.main()
