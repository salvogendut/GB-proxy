import io
import struct
import unittest

from PIL import Image

from utils.dox_utils import (
	DoxLimits,
	DoxValidationError,
	SgxProfile,
	build_dox_from_html,
	build_dox_from_image,
	parse_sgx_profile,
	validate_dox,
)
from utils.image_utils import SGX_MODE_0, SGX_MODE_5, SYMBOS_PALETTE


def _png(indexes, width, height=1):
	image = Image.new("RGB", (width, height))
	image.putdata([SYMBOS_PALETTE[index] for index in indexes])
	output = io.BytesIO()
	image.save(output, format="PNG")
	return output.getvalue()


def _serialized_chunk(document, name):
	offset = 0
	while offset < len(document):
		length = struct.unpack_from("<I", document, offset + 4)[0]
		end = offset + 8 + length
		if document[offset:offset + 4] == name:
			return document[offset:end]
		offset = end
	return None


class SgxProfileTests(unittest.TestCase):
	def test_strict_supported_profiles(self):
		self.assertEqual(parse_sgx_profile("0,2"), SgxProfile(0, 2))
		self.assertEqual(parse_sgx_profile(" 0,4 "), SgxProfile(0, 4))
		self.assertEqual(parse_sgx_profile("5,16"), SgxProfile(5, 16))

	def test_missing_or_malformed_profile_defaults_to_safest_depth(self):
		for value in (None, "", "0", "5", "0,16", "5,4", "0,4,5", "garbage"):
			with self.subTest(value=value):
				self.assertEqual(parse_sgx_profile(value), SgxProfile(0, 2))


class DoxSerializationTests(unittest.TestCase):
	def test_four_colour_image_has_exact_extended_grph_vector(self):
		document = build_dox_from_image(
			_png((0, 1, 2, 3, 3, 2, 1, 0), 8),
			"http://example.com/four.png",
			profile=SgxProfile(SGX_MODE_0, 4),
			dithering="none",
		)

		self.assertEqual(
			_serialized_chunk(document, b"GRPH"),
			bytes.fromhex("475250480d000000010a00400002000800010053ac"),
		)

	def test_two_colour_image_has_exact_extended_grph_vector(self):
		document = build_dox_from_image(
			_png((0, 1, 0, 1, 1, 0, 1, 0), 8),
			"http://example.com/two.png",
			profile=SgxProfile(SGX_MODE_0, 2),
			dithering="none",
		)

		self.assertEqual(
			_serialized_chunk(document, b"GRPH"),
			bytes.fromhex("475250480d000000010a00400002000800010050a0"),
		)

	def test_sixteen_colour_image_has_exact_extended_grph_vector(self):
		document = build_dox_from_image(
			_png((0, 5, 10, 15), 4),
			"http://example.com/sixteen.png",
			profile=SgxProfile(SGX_MODE_5, 16),
			dithering="none",
		)

		self.assertEqual(
			_serialized_chunk(document, b"GRPH"),
			bytes.fromhex("475250480d000000010a00400502000400010005af"),
		)

	def test_html_is_sanitized_and_contains_required_chunks(self):
		document = build_dox_from_html(
			"""
			<html><head><title>Retro Web</title><script>SECRET_SCRIPT</script></head>
			<body><h1>Hello</h1><style>SECRET_STYLE</style><p>World</p></body></html>
			""",
			"http://example.com/page",
		)
		chunks = validate_dox(document)

		self.assertEqual(tuple(chunks), (b"INFO", b"HEAD", b"TEXT", b"GRPH", b"LINK", b"ENDF"))
		self.assertIn(b"Retro Web", chunks[b"INFO"])
		self.assertIn(b"Hello", chunks[b"TEXT"])
		self.assertIn(b"World", chunks[b"TEXT"])
		self.assertNotIn(b"SECRET", chunks[b"TEXT"])

	def test_links_are_bounded_and_get_clickable_icon_graphic(self):
		document = build_dox_from_html(
			'<p><a href="https://example.com/next">Next</a></p>',
			"https://example.com/start",
		)
		chunks = validate_dox(document)

		self.assertEqual(chunks[b"LINK"][0], 1)
		self.assertIn(b"\x00https://example.com/next\x00", chunks[b"LINK"])
		self.assertEqual(chunks[b"GRPH"][0], 1)
		self.assertIn(b"Next", chunks[b"TEXT"])
		self.assertNotIn(b"\x03Next\x04", chunks[b"TEXT"])
		self.assertIn(bytes((10, 2, 1, 0x80, 1, 1, 5, 1)), chunks[b"TEXT"])

	def test_page_images_are_fetched_eagerly_and_embedded(self):
		requests = []

		def fetch(url):
			requests.append(url)
			return _png((0, 1, 0, 1, 1, 0, 1, 0), 8)

		document = build_dox_from_html(
			'<p>Logo<img src="/logo.png" alt="logo"></p>',
			"https://example.com/start",
			image_fetcher=fetch,
		)
		chunks = validate_dox(document)

		self.assertEqual(requests, ["https://example.com/logo.png"])
		self.assertEqual(chunks[b"GRPH"][0], 1)
		self.assertIn(bytes((10, 2, 1, 0x80, 0, 1, 5, 1)), chunks[b"TEXT"])

	def test_duplicate_images_are_fetched_and_stored_only_once(self):
		requests = []

		def fetch(url):
			requests.append(url)
			return _png((0, 1, 0, 1, 1, 0, 1, 0), 8)

		document = build_dox_from_html(
			'<img src="/same.png"><img src="/same.png">',
			"https://example.com/start",
			image_fetcher=fetch,
		)
		chunks = validate_dox(document)

		self.assertEqual(requests, ["https://example.com/same.png"])
		self.assertEqual(chunks[b"GRPH"][0], 1)
		self.assertEqual(chunks[b"TEXT"].count(bytes((10, 2, 1, 0x80))), 2)

	def test_failed_image_work_is_capped_by_graphic_limit(self):
		requests = []

		def fetch(url):
			requests.append(url)
			return b"not an image"

		document = build_dox_from_html(
			"".join(f'<img src="/{number}.png" alt="{number}">' for number in range(10)),
			"https://example.com/start",
			image_fetcher=fetch,
			limits=DoxLimits(max_graphics=2),
		)
		chunks = validate_dox(document, limits=DoxLimits(max_graphics=2))

		self.assertEqual(len(requests), 2)
		self.assertEqual(chunks[b"GRPH"][0], 0)

	def test_text_and_link_limits_are_enforced_without_invalid_output(self):
		limits = DoxLimits(max_text_bytes=64, max_links=1)
		document = build_dox_from_html(
			("<p>" + "word " * 200 + "</p>"
			 '<a href="http://one.example/">one</a>'
			 '<a href="http://two.example/">two</a>'),
			"http://example.com/",
			limits=limits,
		)
		chunks = validate_dox(document, limits=limits)

		self.assertLessEqual(len(chunks[b"TEXT"]), 64)
		self.assertEqual(chunks[b"TEXT"][-2:], b"\x00\xff")
		self.assertEqual(chunks[b"LINK"][0], 1)

	def test_non_printable_del_is_not_emitted_as_text(self):
		document = build_dox_from_html("<p>A\x7fB</p>", "http://example.com/")
		text = validate_dox(document)[b"TEXT"]

		self.assertNotIn(b"\x7f", text)
		self.assertIn(b"A B", text)

	def test_links_past_limit_are_not_shortened_or_registered(self):
		shortened = []

		def shorten(url):
			shortened.append(url)
			return "http://proxy.example/u/one"

		document = build_dox_from_html(
			"".join(
				f'<a href="http://target.example/{number}">{number}</a>'
				for number in range(100)
			),
			"http://example.com/",
			limits=DoxLimits(max_links=1),
			link_shortener=shorten,
		)
		chunks = validate_dox(document, limits=DoxLimits(max_links=1))

		self.assertEqual(shortened, ["http://target.example/0"])
		self.assertEqual(chunks[b"LINK"][0], 1)

	def test_nested_link_image_is_clickable_without_extra_fallback_icon(self):
		image = _png((0, 1, 0, 1, 1, 0, 1, 0), 8)
		document = build_dox_from_html(
			'<a href="/next"><span><img src="/button.png"></span></a>',
			"http://example.com/",
			image_fetcher=lambda _url: image,
		)
		chunks = validate_dox(document)

		# One reserved link icon plus the linked page image. The image marker is
		# linked directly, so no second fallback icon marker is emitted.
		self.assertEqual(chunks[b"GRPH"][0], 2)
		self.assertEqual(
			chunks[b"TEXT"].count(bytes((10, 2, 2, 0x80, 1, 1, 5, 1))),
			1,
		)
		self.assertNotIn(bytes((10, 2, 1, 0x80, 1, 1, 5, 1)), chunks[b"TEXT"])

	def test_text_trailer_resets_formatting_before_terminator(self):
		limits = DoxLimits(max_text_bytes=16)
		document = build_dox_from_html(
			"<b>abcdefghijklmnopqrstuvwxyz</b>",
			"http://example.com/",
			limits=limits,
		)
		text = validate_dox(document, limits=limits)[b"TEXT"]

		self.assertTrue(text.endswith(b"\x04\x02\x01\x01\x00\xff"))

	def test_validator_rejects_corrupt_chunk_length(self):
		document = bytearray(build_dox_from_html("<p>ok</p>", "http://example.com/"))
		document[4:8] = struct.pack("<I", len(document))

		with self.assertRaisesRegex(DoxValidationError, "past end"):
			validate_dox(bytes(document))


if __name__ == "__main__":
	unittest.main()
