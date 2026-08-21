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


def _replace_chunk(document, name, payload):
	result = bytearray()
	offset = 0
	while offset < len(document):
		length = struct.unpack_from("<I", document, offset + 4)[0]
		end = offset + 8 + length
		if document[offset:offset + 4] == name:
			result.extend(name + struct.pack("<I", len(payload)) + payload)
		else:
			result.extend(document[offset:end])
		offset = end
	return bytes(result)


def _without_chunk(document, name):
	result = bytearray()
	offset = 0
	while offset < len(document):
		length = struct.unpack_from("<I", document, offset + 4)[0]
		end = offset + 8 + length
		if document[offset:offset + 4] != name:
			result.extend(document[offset:end])
		offset = end
	return bytes(result)


def _insert_before_end(document, name, payload):
	end_chunk = _serialized_chunk(document, b"ENDF")
	return document[:-len(end_chunk)] + name + struct.pack("<I", len(payload)) + payload + end_chunk


def _ctrl_records(payload):
	control_length, string_length = struct.unpack_from("<HH", payload)
	control_section = payload[4:4 + control_length]
	string_section = payload[4 + control_length:4 + control_length + string_length]
	count = control_section[0]
	lengths = struct.unpack_from(f"<{count}H", control_section, 1) if count else ()
	offset = 1 + count * 2
	controls = []
	for length in lengths:
		controls.append(control_section[offset:offset + length])
		offset += length
	strings = []
	offset = 0
	while struct.unpack_from("<H", string_section, offset)[0]:
		length = struct.unpack_from("<H", string_section, offset)[0]
		strings.append(string_section[offset + 2:offset + length])
		offset += length
	return controls, strings


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
				f'<a href="http://target.example/{"x" * 120}/{number}">{number}</a>'
				for number in range(100)
			),
			"http://example.com/",
			limits=DoxLimits(max_links=1),
			link_shortener=shorten,
		)
		chunks = validate_dox(document, limits=DoxLimits(max_links=1))

		self.assertEqual(shortened, [f"http://target.example/{'x' * 120}/0"])
		self.assertEqual(chunks[b"LINK"][0], 1)

	def test_short_links_are_kept_original_without_registering_proxy_tokens(self):
		shortened = []
		document = build_dox_from_html(
			'<a href="https://example.com/next">Next</a>',
			"https://example.com/start",
			link_shortener=lambda url: shortened.append(url) or "http://proxy/u/x",
		)
		links = validate_dox(document)[b"LINK"]

		self.assertEqual(shortened, [])
		self.assertIn(b"\x00https://example.com/next\x00", links)

	def test_direct_link_limit_is_exact_and_unsafe_urls_are_shortened(self):
		prefix = "https://example.com/"
		at_limit = prefix + "a" * (127 - len(prefix))
		over_limit = at_limit + "b"
		unsafe = "https://example.com/a%20b".replace("%20", " ")
		shortened = []

		def shorten(url):
			shortened.append(url)
			return f"http://p/u/{len(shortened)}"

		document = build_dox_from_html(
			f'<a href="{at_limit}">Fits</a>'
			f'<a href="{over_limit}">Long</a>'
			f'<a href="{unsafe}">Unsafe</a>',
			"https://example.com/start",
			link_shortener=shorten,
		)
		links = validate_dox(document)[b"LINK"]

		self.assertIn(b"\x00" + at_limit.encode("ascii") + b"\x00", links)
		self.assertEqual(shortened, [over_limit, unsafe])
		self.assertIn(b"\x00http://p/u/1\x00", links)
		self.assertIn(b"\x00http://p/u/2\x00", links)

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

	def test_frogfind_get_form_has_exact_bounded_ctrl_records(self):
		actions = []
		document = build_dox_from_html(
			("<form action='/' method='get'>Leap to: "
			 "<input type='text' size='30' name='q'>"
			 "<input type='submit' value='Ribbbit!'>"
			 "<input type='radio' name='region' value='au-en' checked> Australia"
			 "</form>"),
			"http://frogfind.au/",
			link_shortener=lambda action: actions.append(action) or "http://proxy/u/frog",
		)
		chunks = validate_dox(document)
		controls, strings = _ctrl_records(chunks[b"CTRL"])

		self.assertEqual(actions, ["http://frogfind.au/?region=au-en"])
		self.assertEqual(controls, [
			bytes((1, 32, 160, 12)) + struct.pack("<HHB", 1, 2, 63),
			bytes((1, 16, 80, 12)) + struct.pack("<HH", 0xffff, 3),
		])
		self.assertEqual(strings[0], b"q\x00")
		self.assertEqual(strings[1], b"\x00" * 64)
		self.assertEqual(strings[2], b"Ribbbit!\x00")
		self.assertEqual(
			chunks[b"TEXT"].count(bytes((10, 7, 1)) + bytes.fromhex("8000010501")),
			1,
		)
		self.assertEqual(
			chunks[b"TEXT"].count(bytes((10, 7, 2)) + bytes.fromhex("8000010501")),
			1,
		)

	def test_text_defaults_get_independent_padded_mutable_buffers(self):
		document = build_dox_from_html(
			("<form action='/find'>"
			 "<input name='q' maxlength='10' value='retro'>"
			 "<input name='again' maxlength='10' value='retro'>"
			 "</form>"),
			"https://example.com/",
			link_shortener=lambda _action: "http://proxy/u/find",
		)
		controls, strings = _ctrl_records(validate_dox(document)[b"CTRL"])
		first_value_id = struct.unpack_from("<H", controls[0], 6)[0]
		second_value_id = struct.unpack_from("<H", controls[1], 6)[0]

		self.assertNotEqual(first_value_id, second_value_id)
		self.assertEqual(strings[first_value_id - 1], b"retro" + b"\x00" * 6)
		self.assertEqual(strings[second_value_id - 1], b"retro" + b"\x00" * 6)

	def test_forms_with_same_action_keep_distinct_link_identity(self):
		actions = []
		document = build_dox_from_html(
			("<form action='/find'><input name='one'></form>"
			 "<form action='/find'><input name='two'></form>"),
			"https://example.com/",
			link_shortener=lambda action: actions.append(action) or "http://proxy/u/same",
		)
		chunks = validate_dox(document)
		controls, _strings = _ctrl_records(chunks[b"CTRL"])

		self.assertEqual(actions, ["https://example.com/find", "https://example.com/find"])
		self.assertEqual(chunks[b"LINK"][0], 2)
		self.assertEqual([control[0] for control in controls], [1, 2])

	def test_static_get_defaults_are_folded_into_the_action(self):
		actions = []
		document = build_dox_from_html(
			("<form action='/find?source=gb#ignored'>"
			 "<input type='hidden' name='tag' value='one'>"
			 "<input type='hidden' name='tag' value='two'>"
			 "<input type='radio' name='region' value='au-en' checked>"
			 "<input type='checkbox' name='images' value='yes'>"
			 "<input name='q'><button>Search</button></form>"),
			"https://example.com/page",
			link_shortener=lambda action: actions.append(action) or "http://proxy/u/find",
		)
		chunks = validate_dox(document)
		controls, _strings = _ctrl_records(chunks[b"CTRL"])

		self.assertEqual(actions, [
			"https://example.com/find?source=gb&tag=one&tag=two&region=au-en"
		])
		self.assertEqual([control[1] for control in controls], [32, 16])

	def test_unsupported_or_over_limit_forms_do_not_register_action_links(self):
		for html, limits in (
			("<form method='post'><input name='q'><button>Go</button></form>", DoxLimits()),
			("<form><input name='q'><button>Go</button></form>", DoxLimits(max_controls=1)),
			(
				"<form><input name='q' maxlength='5'></form>",
				DoxLimits(max_control_bytes=40),
			),
			(
				("<form><input name='q'><input type='password' name='secret'>"
				 "<button>Go</button></form>"),
				DoxLimits(),
			),
			(
				"<form><input name='q'><button name='go' value='yes'>Go</button></form>",
				DoxLimits(),
			),
			(
				"<form><input name='q'><select name='region'><option>AU</option></select></form>",
				DoxLimits(),
			),
			(
				"<form><input name='q'><button formmethod='post'>Go</button></form>",
				DoxLimits(),
			),
			(
				"<form action='/" + "x" * 2100 + "'><input name='q'></form>",
				DoxLimits(),
			),
		):
			with self.subTest(html=html):
				actions = []
				document = build_dox_from_html(
					html,
					"http://example.com/",
					limits=limits,
					link_shortener=lambda action: actions.append(action) or "http://proxy/u/x",
				)
				chunks = validate_dox(document, limits=limits)
				self.assertNotIn(b"CTRL", chunks)
				self.assertEqual(chunks[b"LINK"][0], 0)
				self.assertEqual(actions, [])

	def test_controls_inside_removed_ancestors_do_not_leave_marker_reservations(self):
		document = build_dox_from_html(
			("<form><template><input name='removed'></template>"
			 "<input name='kept' maxlength='5'></form>"),
			"http://example.com/",
			link_shortener=lambda _action: "http://proxy/u/x",
		)
		controls, strings = _ctrl_records(validate_dox(document)[b"CTRL"])

		self.assertEqual(len(controls), 1)
		self.assertIn(b"kept\x00", strings)
		self.assertNotIn(b"removed\x00", strings)

	def test_legacy_document_and_canonical_empty_ctrl_are_both_valid(self):
		document = build_dox_from_html("<p>legacy</p>", "http://example.com/")
		self.assertNotIn(b"CTRL", validate_dox(document))

		empty_ctrl = bytes.fromhex("01000200000000")
		with_ctrl = _insert_before_end(document, b"CTRL", empty_ctrl)
		self.assertEqual(validate_dox(with_ctrl)[b"CTRL"], empty_ctrl)

	def test_validator_rejects_malformed_ctrl_sections_records_strings_and_markers(self):
		document = build_dox_from_html(
			"<form><input name='q' maxlength='5' value='x'></form>",
			"http://example.com/",
			link_shortener=lambda _action: "http://proxy/u/x",
		)
		chunks = validate_dox(document)
		ctrl = chunks[b"CTRL"]
		control_length = struct.unpack_from("<H", ctrl)[0]
		control_offset = 4 + 1 + 2

		bad_type = bytearray(ctrl)
		bad_type[control_offset + 1] = 99
		bad_padding = bytearray(ctrl)
		bad_padding[-3] = ord("X")
		bad_length = bytearray(ctrl)
		bad_length[:2] = struct.pack("<H", control_length + 1)
		missing_string_terminator = bytearray(ctrl[:-2])
		string_length = struct.unpack_from("<H", ctrl, 2)[0]
		missing_string_terminator[2:4] = struct.pack("<H", string_length - 2)
		for payload, message in (
			(b"\x00\x00\x00", "section lengths"),
			(bytes(bad_length), "section lengths"),
			(bytes(bad_type), "Unsupported CTRL"),
			(bytes(bad_padding), "padding"),
			(bytes(missing_string_terminator), "missing its terminator"),
		):
			with self.subTest(message=message), self.assertRaisesRegex(
				DoxValidationError, message
			):
				validate_dox(_replace_chunk(document, b"CTRL", payload))

		with self.assertRaisesRegex(DoxValidationError, "without a CTRL chunk"):
			validate_dox(_without_chunk(document, b"CTRL"))
		malformed_marker = _replace_chunk(
			document,
			b"TEXT",
			chunks[b"TEXT"].replace(bytes.fromhex("0a07018000010501"), bytes.fromhex("0a07018000010502")),
		)
		with self.assertRaisesRegex(DoxValidationError, "Malformed CTRL marker"):
			validate_dox(malformed_marker)
		wrong_marker_id = _replace_chunk(
			document,
			b"TEXT",
			chunks[b"TEXT"].replace(bytes.fromhex("0a0701"), bytes.fromhex("0a0700")),
		)
		with self.assertRaisesRegex(DoxValidationError, "do not match"):
			validate_dox(wrong_marker_id)

	def test_validator_rejects_invalid_ctrl_cross_references_and_working_size(self):
		document = build_dox_from_html(
			"<form><input name='q' maxlength='5' value='x'></form>",
			"http://example.com/",
			link_shortener=lambda _action: "http://proxy/u/x",
		)
		chunks = validate_dox(document)
		ctrl = chunks[b"CTRL"]
		control_length = struct.unpack_from("<H", ctrl)[0]
		control_offset = 7
		string_offset = 4 + control_length

		bad_action = bytearray(ctrl)
		bad_action[control_offset] = 0
		bad_name = bytearray(ctrl)
		bad_name[control_offset + 4:control_offset + 6] = b"\x00\x00"
		bad_capacity = bytearray(ctrl)
		bad_capacity[control_offset + 8] = 6
		bad_string_length = bytearray(ctrl)
		bad_string_length[string_offset:string_offset + 2] = b"\x02\x00"
		bad_ascii = bytearray(ctrl)
		bad_ascii[string_offset + 2] = 1
		for payload, message in (
			(bytes(bad_action), "action link"),
			(bytes(bad_name), "name string reference"),
			(bytes(bad_capacity), "wrong capacity"),
			(bytes(bad_string_length), "too short"),
			(bytes(bad_ascii), "printable ASCII"),
		):
			with self.subTest(message=message), self.assertRaisesRegex(
				DoxValidationError, message
			):
				validate_dox(_replace_chunk(document, b"CTRL", payload))

		post_links = bytearray(chunks[b"LINK"])
		post_links[3] = 1
		with self.assertRaisesRegex(DoxValidationError, "must use GET"):
			validate_dox(_replace_chunk(document, b"LINK", bytes(post_links)))
		with self.assertRaisesRegex(DoxValidationError, "working allocation"):
			validate_dox(document, limits=DoxLimits(max_control_bytes=40))

		duplicate_ctrl = _insert_before_end(document, b"CTRL", ctrl)
		with self.assertRaisesRegex(DoxValidationError, "Duplicate DOX chunk CTRL"):
			validate_dox(duplicate_ctrl)

	def test_validator_rejects_corrupt_chunk_length(self):
		document = bytearray(build_dox_from_html("<p>ok</p>", "http://example.com/"))
		document[4:8] = struct.pack("<I", len(document))

		with self.assertRaisesRegex(DoxValidationError, "past end"):
			validate_dox(bytes(document))


if __name__ == "__main__":
	unittest.main()
