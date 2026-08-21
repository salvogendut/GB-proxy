import unittest
from unittest.mock import patch

import markdown
from bs4 import BeautifulSoup, Doctype

from utils.markdown_utils import (
	_EscapeRawHtml,
	_SAFE_BACKTICK_RE,
	MarkdownSafetyError,
	is_markdown_response,
	markdown_to_html,
)


class MarkdownResponseDetectionTests(unittest.TestCase):
	def test_explicit_markdown_media_types_are_recognized(self):
		for content_type in (
			"text/markdown",
			"Text/Markdown; Charset=UTF-8",
			"text/x-markdown",
			"application/markdown",
			"application/x-markdown; version=1",
		):
			with self.subTest(content_type=content_type):
				self.assertTrue(
					is_markdown_response(content_type, "https://example.com/document")
				)

	def test_plain_text_requires_a_markdown_filename(self):
		for url in (
			"https://example.com/README.md",
			"https://example.com/Guide.MARKDOWN?raw=1",
			"https://example.com/a%20name.Md#part",
		):
			with self.subTest(url=url):
				self.assertTrue(is_markdown_response("text/plain; charset=utf-8", url))

		for content_type, url in (
			("text/plain", "https://example.com/readme.txt"),
			("text/plain", "https://example.com/markdown"),
			("application/octet-stream", "https://example.com/readme.md"),
			("", "https://example.com/readme.md"),
			("text/html", "https://example.com/readme.md"),
		):
			with self.subTest(content_type=content_type, url=url):
				self.assertFalse(is_markdown_response(content_type, url))


class MarkdownConversionTests(unittest.TestCase):
	def parse(self, source, content_type="text/markdown; charset=utf-8", url="https://example.com/docs/readme.md"):
		document = markdown_to_html(source, content_type, url)
		return document, BeautifulSoup(document, "html.parser")

	def test_declared_charset_and_utf8_fallback_are_used(self):
		document, soup = self.parse(
			"# Caf\xe9".encode("iso-8859-1"),
			"text/markdown; charset=iso-8859-1",
		)
		self.assertEqual(soup.title.string, "Caf\xe9")
		self.assertEqual(soup.h1.get_text(), "Caf\xe9")
		self.assertTrue(document.startswith("<!doctype html>"))

		_, fallback = self.parse(
			"# Pi\xf1ata".encode("utf-8"),
			"text/markdown; charset=definitely-not-a-codec",
		)
		self.assertEqual(fallback.h1.get_text(), "Pi\xf1ata")

		_, replacement = self.parse(b"# bad:\xff", "text/markdown")
		self.assertIn("\ufffd", replacement.h1.get_text())

	def test_common_safe_markdown_semantics_are_preserved(self):
		source = """# Field Guide

Paragraph with **strong** and *emphasis*.

> Quoted text

- first
- second

```python
print("hello")
```

| Name | Value |
| --- | --- |
| mode | retro |

[Manual](../manual.md?view=1#start)

![Logo](images/logo.png "Small logo")
"""
		document, soup = self.parse(source)

		self.assertTrue(any(isinstance(item, Doctype) for item in soup.contents))
		self.assertEqual(soup.title.string, "Field Guide")
		self.assertEqual(soup.strong.get_text(), "strong")
		self.assertEqual(soup.em.get_text(), "emphasis")
		self.assertEqual(soup.blockquote.get_text(" ", strip=True), "Quoted text")
		self.assertEqual([item.get_text(strip=True) for item in soup.ul.find_all("li")], ["first", "second"])
		self.assertIn('print("hello")', soup.pre.code.get_text())
		self.assertEqual(soup.table.td.get_text(strip=True), "mode")
		self.assertEqual(soup.a["href"], "../manual.md?view=1#start")
		self.assertEqual(soup.img["src"], "images/logo.png")
		self.assertEqual(soup.img["title"], "Small logo")
		self.assertNotIn("class", soup.pre.code.attrs)
		self.assertEqual(soup.meta["charset"].lower(), "utf-8")

	def test_inline_code_uses_the_bounded_backtick_rule(self):
		parser = markdown.Markdown(extensions=(_EscapeRawHtml(),))
		processor = parser.inlinePatterns["backtick"]
		self.assertEqual(processor.pattern, _SAFE_BACKTICK_RE)
		self.assertIn(r"(?<![`\\])(?=(`+))", processor.pattern)

		_, soup = self.parse("Use `plain` and `` `nested` `` code.")
		self.assertEqual(
			[item.get_text() for item in soup.find_all("code")],
			["plain", "`nested`"],
		)

	def test_pathological_backtick_layout_is_rejected_before_parser(self):
		source = " ".join("`" * length for length in range(1, 257))
		source += "x" * 65536
		with patch("utils.markdown_utils.markdown.markdown") as render:
			with self.assertRaises(MarkdownSafetyError):
				markdown_to_html(
					source,
					"text/markdown",
					"https://example.com/hostile.md",
				)
		render.assert_not_called()

	def test_raw_html_stays_literal_and_cannot_create_active_elements(self):
		source = """# Safe

<script>alert(1)</script>
<form action="https://evil.example/"><input name="secret"></form>
<a href="https://evil.example/">raw link</a>
<img src="https://evil.example/pixel.png" onerror="alert(1)">
<div onclick="alert(1)">raw block</div>
"""
		_, soup = self.parse(source)
		text = soup.body.get_text("\n", strip=True)

		self.assertIsNone(soup.find("script"))
		self.assertIsNone(soup.find("form"))
		self.assertIsNone(soup.find("input"))
		self.assertIsNone(soup.find("a"))
		self.assertIsNone(soup.find("img"))
		self.assertIn("<script>alert(1)</script>", text)
		self.assertIn('<a href="https://evil.example/">raw link</a>', text)
		self.assertIn("raw block", text)

	def test_unsafe_and_control_character_urls_are_removed(self):
		source = (
		"[web](https://example.com/a) "
		"[relative](../guide.md) "
		"[fragment](#part) "
		"[script](javascript:alert(1)) "
		"[data](data:text/html,bad) "
		"[file](file:///etc/passwd) "
		"[mail](mailto:test@example.com) "
		"[control](https://example.com/bad\x01path)\n\n"
		"![safe](../image.png) ![unsafe](javascript:alert(1))"
	)
		_, soup = self.parse(source)
		hrefs = {link.get_text(strip=True): link["href"] for link in soup.find_all("a")}

		self.assertEqual(hrefs, {
			"web": "https://example.com/a",
			"relative": "../guide.md",
			"fragment": "#part",
		})
		self.assertEqual([image["src"] for image in soup.find_all("img")], ["../image.png"])
		self.assertIn("script", soup.body.get_text(" ", strip=True))
		self.assertIn("unsafe", soup.body.get_text(" ", strip=True))

	def test_title_falls_back_to_filename_then_host(self):
		_, filename = self.parse("No heading", url="https://example.com/docs/README.md")
		self.assertEqual(filename.title.string, "README.md")

		_, host = self.parse("No heading", url="https://example.com/")
		self.assertEqual(host.title.string, "example.com")


if __name__ == "__main__":
	unittest.main()
