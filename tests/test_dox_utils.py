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


def _counted_records(payload):
	count = payload[0]
	lengths = struct.unpack_from(f"<{count}H", payload, 1) if count else ()
	offset = 1 + count * 2
	records = []
	for length in lengths:
		records.append(payload[offset:offset + length])
		offset += length
	return records


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

	def test_simple_table_has_exact_current_symzilla_column_vector(self):
		document = build_dox_from_html(
			("<table><tr><th>A</th><th>B</th></tr>"
			 "<tr><td>1</td><td>2</td></tr></table>"),
			"http://example.com/",
		)
		text = validate_dox(document)[b"TEXT"]

		self.assertEqual(text, bytes.fromhex(
			"ff 12 "
			"0e 16 00 05 01 01 02 ff ff 02 02 33 23 13 "
			"02 03 01 41 02 01 01 00 "
			"0e 16 00 03 01 02 02 ff ff 02 02 33 23 13 "
			"02 03 01 42 02 01 01 00 00 "
			"ff 12 "
			"0e 10 00 05 01 01 02 ff ff 02 02 33 23 13 31 00 "
			"0e 10 00 03 01 02 02 ff ff 02 02 33 23 13 32 00 00 "
			"04 02 01 01 00 ff"
		))

	def test_four_column_geometry_uses_marked_signed_coefficients(self):
		document = build_dox_from_html(
			"<table><tr>" + "".join(f"<td>{value}</td>" for value in "ABCD")
			+ "</tr></table>",
			"http://example.com/",
		)
		text = validate_dox(document)[b"TEXT"]
		offset = 2
		geometries = []
		for _ in range(4):
			geometries.append(text[offset + 3:offset + 11])
			offset += struct.unpack_from("<H", text, offset + 1)[0]

		self.assertEqual(geometries, [
			bytes.fromhex("05 01 01 04 ff ff 02 04"),
			bytes.fromhex("03 01 02 04 ff ff 02 04"),
			bytes.fromhex("01 01 03 04 ff ff 02 04"),
			bytes.fromhex("ff ff 04 04 ff ff 02 04"),
		])

	def test_table_boundaries_preserve_surrounding_standard_text(self):
		document = build_dox_from_html(
			("<p>Before</p><table><tr><td>Left</td><td>Right</td></tr></table>"
			 "<p>After</p>"),
			"http://example.com/",
		)
		text = validate_dox(document)[b"TEXT"]

		self.assertLess(text.index(b"Before"), text.index(b"\xff\x12"))
		self.assertLess(text.index(b"\xff\x12"), text.index(b"After"))
		self.assertIn(b"\x04\x02\x01\x01\x00\x00\xff\x12", text)

	def test_inline_table_links_keep_the_existing_clickable_icon(self):
		document = build_dox_from_html(
			("<table><tr><td><a href='/left'>Left</a></td>"
			 "<td><strong>Right</strong></td></tr></table>"),
			"https://example.com/start",
		)
		chunks = validate_dox(document)

		self.assertEqual(chunks[b"LINK"][0], 1)
		self.assertIn(b"\x00https://example.com/left\x00", chunks[b"LINK"])
		self.assertIn(bytes((10, 2, 1, 0x80, 1, 1, 5, 1)), chunks[b"TEXT"])
		self.assertIn(b"\x02\x03\x01Right\x02\x01\x01", chunks[b"TEXT"])

	def test_unsupported_tables_fall_back_wholly_in_source_order(self):
		cases = (
			("<table><tr><td colspan='2'>A</td><td>B</td></tr></table>", (b"A", b"B")),
			(
				"<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>",
				(b"A", b"B", b"C"),
			),
			(
				"<table><tr>" + "".join(f"<td>{value}</td>" for value in "ABCDE")
				+ "</tr></table>",
				tuple(value.encode("ascii") for value in "ABCDE"),
			),
			(
				("<table><tr><td>Outer<table><tr><td>Inner</td><td>Grid</td></tr>"
				 "</table></td><td>End</td></tr></table>"),
				(b"Outer", b"Inner", b"Grid", b"End"),
			),
			(
				("<table><tr><td><div><img src='/x.png' alt='Picture'></div></td>"
				 "<td>End</td></tr></table>"),
				(b"Picture", b"End"),
			),
		)
		for html, labels in cases:
			with self.subTest(html=html):
				document = build_dox_from_html(html, "http://example.com/")
				text = validate_dox(document)[b"TEXT"]
				self.assertNotIn(b"\xff\x12", text)
				positions = [text.index(label) for label in labels]
				self.assertEqual(positions, sorted(positions))

	def test_unsupported_image_table_fallback_keeps_page_sized_sgx(self):
		requests = []
		image = _png((0, 1) * (160 * 80 // 2), 160, 80)
		document = build_dox_from_html(
			("<table><tr><td colspan='2'><img src='/picture.png' alt='Picture'></td>"
			 "<td>Caption</td></tr></table>"),
			"http://example.com/",
			image_fetcher=lambda url: requests.append(url) or image,
		)
		chunks = validate_dox(document)
		graphics = _counted_records(chunks[b"GRPH"])

		self.assertEqual(requests, ["http://example.com/picture.png"])
		self.assertNotIn(b"\xff\x12", chunks[b"TEXT"])
		self.assertEqual(len(graphics), 1)
		self.assertEqual(struct.unpack_from("<HHH", graphics[0], 2)[1:], (160, 80))
		self.assertIn(bytes((10, 2, 1, 0x80, 0, 1, 5, 1)), chunks[b"TEXT"])
		self.assertIn(b"Caption", chunks[b"TEXT"])

	def test_retrocheats_linked_image_grid_uses_cell_sized_sgx(self):
		images = [
			_png(tuple(
				1 if pixel % 120 < (identity + 1) * 20 else 0
				for pixel in range(120 * 80)
			), 120, 80)
			for identity in range(6)
		]
		html = "<table>" + "".join(
			"<tr>" + "".join(
				f"<td><a href='/{row}-{column}'><img src='/{row}-{column}.png' "
				f"alt='{row}-{column}'></a></td>"
				for column in range(3)
			) + "</tr>"
			for row in range(2)
		) + "</table>"

		for profile, expected_size in (
			(SgxProfile(SGX_MODE_0, 4), (56, 37)),
			(SgxProfile(SGX_MODE_5, 16), (60, 40)),
		):
			with self.subTest(profile=profile):
				requests = []

				def fetch(url):
					requests.append(url)
					return images[len(requests) - 1]

				document = build_dox_from_html(
					html,
					"https://retro.example/",
					profile=profile,
					image_fetcher=fetch,
					dithering="none",
				)
				chunks = validate_dox(document)
				graphics = _counted_records(chunks[b"GRPH"])

				self.assertEqual(len(requests), 6)
				self.assertEqual(chunks[b"TEXT"].count(b"\xff\x13"), 2)
				self.assertEqual(chunks[b"LINK"][0], 6)
				# The reserved eye icon is graphic 1. Each linked cell uses its image
				# directly, without emitting the fallback icon marker.
				self.assertEqual(len(graphics), 7)
				self.assertNotIn(bytes((10, 2, 1, 0x80)), chunks[b"TEXT"])
				for identity in range(1, 7):
					self.assertIn(
						bytes((10, 2, identity + 1, 0x80, identity, 1, 5, 1)),
						chunks[b"TEXT"],
					)
				for graphic in graphics[1:]:
					_width_bytes, width, height = struct.unpack_from("<HHH", graphic, 2)
					self.assertEqual((width, height), expected_size)

	def test_retrocheats_legacy_widths_fit_and_center_full_sized_images(self):
		images = [
			_png(tuple(
				1 if pixel % 120 < (identity + 1) * 20 else 0
				for pixel in range(120 * 80)
			), 120, 80)
			for identity in range(6)
		]
		html = (
			"<center><table border='0' cellpadding='2' cellspacing='0' width='384'>"
			+ "".join(
				"<tr>" + "".join(
					f"<td width='128' align='center'><a href='/{row}-{column}'>"
					f"<img src='/{row}-{column}.png' width='120' height='80' "
					f"border='0' alt='{row}-{column}'></a></td>"
					for column in range(3)
				) + "</tr>"
				for row in range(2)
			)
			+ "</table></center>"
		)
		expected_geometry = (
			bytes.fromhex("85 fd 02 02 ff 01 01 01"),
			bytes.fromhex("83 ff 02 02 ff 01 01 01"),
			bytes.fromhex("81 01 02 02 ff 01 01 01"),
		)

		for profile in (
			SgxProfile(SGX_MODE_0, 4),
			SgxProfile(SGX_MODE_5, 16),
		):
			with self.subTest(profile=profile):
				requests = []

				def fetch(url):
					requests.append(url)
					return images[len(requests) - 1]

				document = build_dox_from_html(
					html,
					"http://retrocheats.neocities.org/",
					profile=profile,
					image_fetcher=fetch,
					dithering="none",
				)
				chunks = validate_dox(document)
				graphics = _counted_records(chunks[b"GRPH"])

				self.assertEqual(struct.unpack("<HHBB", chunks[b"HEAD"]), (384, 600, 0, 2))
				self.assertEqual(len(requests), 6)
				self.assertEqual(len(graphics), 7)
				self.assertEqual(chunks[b"LINK"][0], 6)
				self.assertEqual(chunks[b"TEXT"].count(b"\x09\x01\x03\x01"), 6)
				for graphic in graphics[1:]:
					self.assertEqual(struct.unpack_from("<HHH", graphic, 2)[1:], (120, 80))

				offset = 0
				for _row in range(2):
					self.assertEqual(chunks[b"TEXT"][offset:offset + 2], b"\xff\x13")
					offset += 2
					for column in range(3):
						header = chunks[b"TEXT"][offset:offset + 14]
						self.assertEqual(header[3:11], expected_geometry[column])
						delta = struct.unpack_from("<H", header, 1)[0]
						body = chunks[b"TEXT"][offset + 14:offset + delta - 1]
						self.assertTrue(body.startswith(b"\x09\x01\x03\x01"))
						offset += delta
					self.assertEqual(chunks[b"TEXT"][offset], 0)
					offset += 1

	def test_fitted_image_tables_cover_two_to_four_columns_on_both_profiles(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)
		for columns in (2, 3, 4):
			for profile in (
				SgxProfile(SGX_MODE_0, 4),
				SgxProfile(SGX_MODE_5, 16),
			):
				with self.subTest(columns=columns, profile=profile):
					table_width = columns * 128
					html = (
						f"<center><table width='{table_width}'><tr>"
						+ "".join(
							("<td width='128' align='center'>"
							 "<img src='/same.png' width='120' height='80'></td>")
							for _ in range(columns)
						)
						+ "</tr></table></center>"
					)
					document = build_dox_from_html(
						html,
						"https://example.com/",
						profile=profile,
						image_fetcher=lambda _url: image,
						dithering="none",
					)
					chunks = validate_dox(document)
					graphic, = _counted_records(chunks[b"GRPH"])

					self.assertEqual(
						struct.unpack("<HHBB", chunks[b"HEAD"]),
						(table_width, 600, 0, 2),
					)
					self.assertEqual(
						chunks[b"TEXT"].count(b"\x09\x01\x03\x01"),
						columns,
					)
					self.assertEqual(
						struct.unpack_from("<HHH", graphic, 2)[1:],
						(120, 80),
					)

	def test_odd_heterogeneous_fitted_widths_remain_centered(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)
		document = build_dox_from_html(
			("<center><table width='257'><tr>"
			 "<td width='128' align='center'><img src='/same.png' width='120' height='80'></td>"
			 "<td width='129' align='center'><img src='/same.png' width='120' height='80'></td>"
			 "</tr></table></center>"),
			"https://example.com/",
			image_fetcher=lambda _url: image,
			dithering="none",
		)
		chunks = validate_dox(document)

		self.assertEqual(struct.unpack("<HHBB", chunks[b"HEAD"]), (257, 600, 0, 2))
		self.assertEqual(chunks[b"TEXT"].count(b"\x09\x01\x03\x01"), 2)

	def test_fitted_cells_ignore_formatting_whitespace_around_linked_images(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)
		compact = (
			"<center><table width='256'><tr>"
			"<td width='128' align='center'><center><a href='/one'>"
			"<img src='/same.png' width='120' height='80'></a></center></td>"
			"<td width='128' align='center'><center><a href='/two'>"
			"<img src='/same.png' width='120' height='80'></a></center></td>"
			"</tr></table></center>"
		)
		formatted = (
			"<center><table width='256'><tr>"
			"<td width='128' align='center'>\n  <center>\n    <a href='/one'>\n"
			"      <img src='/same.png' width='120' height='80'>\n"
			"    </a>\n  </center>\n</td>"
			"<td width='128' align='center'>\n  <center>\n    <a href='/two'>\n"
			"      <img src='/same.png' width='120' height='80'>\n"
			"    </a>\n  </center>\n</td>"
			"</tr></table></center>"
		)

		def build(html):
			return build_dox_from_html(
				html,
				"https://example.com/",
				image_fetcher=lambda _url: image,
				dithering="none",
			)

		self.assertEqual(build(formatted), build(compact))

	def test_unsafe_or_ambiguous_legacy_widths_keep_responsive_geometry(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)

		def cell(*, width="128", align="center", image_width="120", extra=""):
			return (
				f"<td width='{width}' align='{align}'><img src='/same.png' "
				f"width='{image_width}' height='80'>{extra}</td>"
			)

		def table(cells, *, width="384", style=""):
			style_attribute = f" style='{style}'" if style else ""
			return (
				f"<center><table width='{width}'{style_attribute}><tr>"
				+ "".join(cells)
				+ "</tr></table></center>"
			)

		base_cells = [cell(), cell(), cell()]
		cases = (
			table(base_cells, width="384px"),
			table(base_cells, width="9" * 5000),
			table(base_cells, width="383"),
			table([cell(width="127"), cell(), cell()]),
			table([cell(align="left"), cell(), cell()]),
			table(base_cells, style="width:384px"),
			table(base_cells).replace("<tr>", "<tr style='height:80px'>"),
			table(base_cells).replace(
				"<tr>", "<tbody style='width:384px'><tr>"
			).replace("</tr>", "</tr></tbody>"),
			table([cell(image_width="124"), cell(), cell()]),
			table([cell(extra=" Caption"), cell(), cell()]),
		)
		canonical_first_geometry = bytes.fromhex("05 01 01 03 ff ff 02 03")
		for html in cases:
			with self.subTest(html=html):
				document = build_dox_from_html(
					html,
					"https://example.com/",
					image_fetcher=lambda _url: image,
					dithering="none",
				)
				chunks = validate_dox(document)

				self.assertEqual(struct.unpack("<HHBB", chunks[b"HEAD"]), (200, 600, 0, 2))
				self.assertEqual(chunks[b"TEXT"][:2], b"\xff\x13")
				self.assertEqual(chunks[b"TEXT"][5:13], canonical_first_geometry)
				self.assertNotIn(b"\x09\x01\x03\x01", chunks[b"TEXT"])

	def test_failed_fitted_images_keep_centered_bounded_alt_cells(self):
		html = (
			"<center><table width='256'><tr>"
			"<td width='128' align='center'><img src='/one.png' width='120' "
			"height='80' alt='One unavailable'></td>"
			"<td width='128' align='center'><img src='/two.png' width='120' "
			"height='80' alt='Two unavailable'></td>"
			"</tr></table></center>"
		)
		document = build_dox_from_html(
			html,
			"https://example.com/",
			image_fetcher=lambda _url: b"not an image",
		)
		chunks = validate_dox(document)

		self.assertEqual(struct.unpack("<HHBB", chunks[b"HEAD"]), (256, 600, 0, 2))
		self.assertEqual(chunks[b"TEXT"].count(b"\x09\x01\x03\x01"), 2)
		self.assertIn(b"[One unavailable]", chunks[b"TEXT"])
		self.assertIn(b"[Two unavailable]", chunks[b"TEXT"])
		self.assertEqual(chunks[b"GRPH"][0], 0)

	def test_fitted_table_text_preflight_does_not_leak_document_width(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)
		html = (
			"<center><table width='256'><tr>"
			"<td width='128' align='center'><img src='/same.png' width='120' height='80'></td>"
			"<td width='128' align='center'><img src='/same.png' width='120' height='80'></td>"
			"</tr></table></center>"
		)
		fitted = build_dox_from_html(
			html,
			"https://example.com/",
			image_fetcher=lambda _url: image,
			dithering="none",
		)
		fitted_text_size = len(validate_dox(fitted)[b"TEXT"])
		fallback = build_dox_from_html(
			html,
			"https://example.com/",
			limits=DoxLimits(max_text_bytes=fitted_text_size - 1),
			image_fetcher=lambda _url: image,
			dithering="none",
		)
		chunks = validate_dox(
			fallback,
			limits=DoxLimits(max_text_bytes=fitted_text_size - 1),
		)

		self.assertEqual(struct.unpack("<HHBB", chunks[b"HEAD"]), (200, 600, 0, 2))
		self.assertNotIn(b"\xff\x12", chunks[b"TEXT"])
		self.assertNotIn(b"\x09\x01\x03\x01", chunks[b"TEXT"])

	def test_page_and_table_image_variants_share_one_source_fetch(self):
		image = _png((0, 1) * (160 * 80 // 2), 160, 80)
		page = "<img src='/same.png'>"
		table = ("<table><tr><td><img src='/same.png'></td>"
			"<td>Caption</td></tr></table>")
		for html, expected_sizes in (
			(page + table, [(160, 80), (88, 44)]),
			(table + page, [(88, 44), (160, 80)]),
		):
			with self.subTest(html=html):
				requests = []
				document = build_dox_from_html(
					html,
					"https://example.com/",
					image_fetcher=lambda url: requests.append(url) or image,
					dithering="none",
				)
				chunks = validate_dox(document)
				graphics = _counted_records(chunks[b"GRPH"])
				sizes = [
					struct.unpack_from("<HHH", graphic, 2)[1:] for graphic in graphics
				]

				self.assertEqual(requests, ["https://example.com/same.png"])
				self.assertEqual(sizes, expected_sizes)
				self.assertIn(bytes((10, 2, 1, 0x80, 0, 1, 5, 1)), chunks[b"TEXT"])
				self.assertIn(bytes((10, 2, 2, 0x80, 0, 1, 5, 1)), chunks[b"TEXT"])
				self.assertIn(b"\xff\x12", chunks[b"TEXT"])

	def test_identical_converted_variants_share_one_graphic_record(self):
		requests = []
		image = _png((0, 1, 0, 1, 1, 0, 1, 0) * 8, 8, 8)
		document = build_dox_from_html(
			("<img src='/small.png'>"
			 "<table><tr><td><img src='/small.png'></td><td>A</td></tr></table>"
			 "<table><tr><td><img src='/small.png'></td><td>B</td><td>C</td></tr></table>"
			 "<table><tr><td><img src='/small.png'></td><td>D</td><td>E</td><td>F</td>"
			 "</tr></table>"),
			"https://example.com/",
			image_fetcher=lambda url: requests.append(url) or image,
			dithering="none",
		)
		chunks = validate_dox(document)

		self.assertEqual(requests, ["https://example.com/small.png"])
		self.assertEqual(chunks[b"GRPH"][0], 1)
		self.assertEqual(
			chunks[b"TEXT"].count(bytes((10, 2, 1, 0x80, 0, 1, 5, 1))),
			4,
		)

	def test_table_image_width_tracks_column_count_and_sgx_mode(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)
		expected = {
			SGX_MODE_0: ((88, 59), (56, 37), (40, 27)),
			SGX_MODE_5: ((92, 61), (60, 40), (44, 29)),
		}
		for profile in (SgxProfile(SGX_MODE_0, 4), SgxProfile(SGX_MODE_5, 16)):
			for columns in (2, 3, 4):
				with self.subTest(profile=profile, columns=columns):
					requests = []
					document = build_dox_from_html(
						("<table><tr>" + "".join(
							"<td><img src='/same.png'></td>" for _ in range(columns)
						) + "</tr></table>"),
						"https://example.com/",
						profile=profile,
						image_fetcher=lambda url: requests.append(url) or image,
						dithering="none",
					)
					chunks = validate_dox(document)
					graphic, = _counted_records(chunks[b"GRPH"])
					_width_bytes, width, height = struct.unpack_from("<HHH", graphic, 2)

					self.assertEqual(requests, ["https://example.com/same.png"])
					self.assertEqual(chunks[b"TEXT"].count(bytes((0xff, 0x10 | columns))), 1)
					self.assertEqual(graphic[1], profile.mode)
					self.assertEqual((width, height), expected[profile.mode][columns - 2])

	def test_failed_table_image_keeps_grid_and_alt_text(self):
		document = build_dox_from_html(
			("<table><tr><td><img src='/bad.png' alt='Unavailable'></td>"
			 "<td>Caption</td></tr></table>"),
			"https://example.com/",
			image_fetcher=lambda _url: b"not an image",
		)
		chunks = validate_dox(document)

		self.assertIn(b"\xff\x12", chunks[b"TEXT"])
		self.assertIn(b"[Unavailable]", chunks[b"TEXT"])
		self.assertIn(b"Caption", chunks[b"TEXT"])
		self.assertEqual(chunks[b"GRPH"][0], 0)

	def test_table_image_alt_is_bounded_without_rejecting_valid_grid(self):
		alt = "A" * 2000
		html = ("<table><tr><td><img src='/image.png' alt='" + alt + "'></td>"
			"<td>Caption</td></tr></table>")
		limits = DoxLimits(max_text_bytes=200)
		valid_document = build_dox_from_html(
			html,
			"https://example.com/",
			limits=limits,
			image_fetcher=lambda _url: _png((0, 1) * 4, 8),
		)
		failed_document = build_dox_from_html(
			html,
			"https://example.com/",
			limits=limits,
			image_fetcher=lambda _url: b"not an image",
		)
		valid_text = validate_dox(valid_document, limits=limits)[b"TEXT"]
		failed_text = validate_dox(failed_document, limits=limits)[b"TEXT"]

		self.assertIn(b"\xff\x12", valid_text)
		self.assertIn(b"\xff\x12", failed_text)
		self.assertIn(b"[" + b"A" * 63 + b"]", failed_text)
		self.assertNotIn(b"A" * 64, failed_text)

	def test_table_near_text_limit_falls_back_without_a_partial_column_row(self):
		limits = DoxLimits(max_text_bytes=64)
		document = build_dox_from_html(
			("<table><tr><td>" + "A" * 40 + "</td><td>" + "B" * 40
			 + "</td></tr></table>"),
			"http://example.com/",
			limits=limits,
		)
		text = validate_dox(document, limits=limits)[b"TEXT"]

		self.assertNotIn(b"\xff\x12", text)
		self.assertLessEqual(len(text), 64)

	def test_table_image_alt_budget_falls_back_before_partial_grid(self):
		limits = DoxLimits(max_text_bytes=80)
		alt = "A" * 30
		document = build_dox_from_html(
			("<table><tr><td><img src='/one.png' alt='" + alt + "'></td>"
			 "<td><img src='/two.png' alt='" + alt + "'></td></tr></table>"),
			"https://example.com/",
			limits=limits,
			image_fetcher=lambda _url: b"not an image",
		)
		text = validate_dox(document, limits=limits)[b"TEXT"]

		self.assertNotIn(b"\xff\x12", text)
		self.assertLessEqual(len(text), 80)

	def test_table_row_and_cell_policy_limits_use_the_same_atomic_fallback(self):
		html = (
			"<table><tr><td>A</td><td>B</td></tr>"
			"<tr><td>C</td><td>D</td></tr></table>"
		)
		for limits in (
			DoxLimits(max_table_rows=1),
			DoxLimits(max_table_cells=3),
		):
			with self.subTest(limits=limits):
				text = validate_dox(
					build_dox_from_html(html, "http://example.com/", limits=limits),
					limits=limits,
				)[b"TEXT"]
				self.assertNotIn(b"\xff\x12", text)
				self.assertLess(text.index(b"A"), text.index(b"D"))

	def test_adjacent_tables_share_document_limits_without_invalidating_output(self):
		limits = DoxLimits(max_table_rows=2, max_table_cells=4)
		document = build_dox_from_html(
			("<table><tr><td>A</td><td>B</td></tr></table>"
			 "<table><tr><td>C</td><td>D</td></tr>"
			 "<tr><td>E</td><td>F</td></tr></table>"),
			"http://example.com/",
			limits=limits,
		)
		text = validate_dox(document, limits=limits)[b"TEXT"]

		self.assertEqual(text.count(b"\xff\x12"), 1)
		self.assertEqual([text.index(value) for value in b"ABCDEF"], sorted(
			text.index(value) for value in b"ABCDEF"
		))

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

	def test_validator_rejects_corrupt_table_headers_geometry_and_jumps(self):
		document = build_dox_from_html(
			("<table><tr><td>A</td><td>B</td></tr>"
			 "<tr><td>C</td><td>D</td></tr></table>"),
			"http://example.com/",
		)
		text = validate_dox(document)[b"TEXT"]
		mutations = []

		bad_count = bytearray(text)
		bad_count[1] = 0x11
		mutations.append((bad_count, "column count"))
		bad_header = bytearray(text)
		bad_header[2] = 13
		mutations.append((bad_header, "header length"))
		bad_divisor = bytearray(text)
		bad_divisor[8] = 0
		mutations.append((bad_divisor, "geometry"))
		inside_header = bytearray(text)
		inside_header[3:5] = b"\x01\x00"
		mutations.append((inside_header, "inside its header"))
		past_text = bytearray(text)
		past_text[3:5] = b"\xff\xff"
		mutations.append((past_text, "past TEXT"))
		missed_terminator = bytearray(text)
		first_target = 2 + struct.unpack_from("<H", text, 3)[0]
		missed_terminator[first_target - 1] = ord("X")
		mutations.append((missed_terminator, "column jump"))
		bad_follow = bytearray(text)
		separator = text.index(b"\x00\x00\xff\x12")
		bad_follow[separator + 1] = 1
		mutations.append((bad_follow, "continuation"))

		for mutated, message in mutations:
			with self.subTest(message=message), self.assertRaisesRegex(
				DoxValidationError, message
			):
				validate_dox(_replace_chunk(document, b"TEXT", bytes(mutated)))

	def test_validator_rejects_unsafe_fitted_geometry_and_head_bounds(self):
		image = _png((0, 1) * (120 * 80 // 2), 120, 80)
		document = build_dox_from_html(
			("<center><table width='256'><tr>"
			 "<td width='128' align='center'><img src='/same.png' width='120' height='80'></td>"
			 "<td width='128' align='center'><img src='/same.png' width='120' height='80'></td>"
			 "</tr></table></center>"),
			"https://example.com/",
			image_fetcher=lambda _url: image,
			dithering="none",
		)
		chunks = validate_dox(document)

		with self.assertRaisesRegex(DoxValidationError, "outside the document"):
			validate_dox(_replace_chunk(
				document, b"HEAD", struct.pack("<HHBB", 200, 600, 0, 2)
			))
		with self.assertRaisesRegex(DoxValidationError, "HEAD"):
			validate_dox(_replace_chunk(
				document, b"HEAD", struct.pack("<HHBB", 601, 600, 0, 2)
			))

		unmarked = bytearray(chunks[b"TEXT"])
		unmarked[5] &= 0xfe
		with self.assertRaisesRegex(DoxValidationError, "marked DOX column"):
			validate_dox(_replace_chunk(document, b"TEXT", bytes(unmarked)))

		overlap = bytearray(chunks[b"TEXT"])
		overlap[9:11] = b"\x91\x03"  # marked signed-14 representation of 200
		with self.assertRaisesRegex(DoxValidationError, "not contiguous"):
			validate_dox(_replace_chunk(document, b"TEXT", bytes(overlap)))

	def test_table_limits_reject_unsafe_native_column_counts(self):
		for columns in (1, 16):
			with self.subTest(columns=columns), self.assertRaisesRegex(
				ValueError, "between 2 and 15"
			):
				DoxLimits(max_table_columns=columns)

	def test_validator_rejects_corrupt_chunk_length(self):
		document = bytearray(build_dox_from_html("<p>ok</p>", "http://example.com/"))
		document[4:8] = struct.pack("<I", len(document))

		with self.assertRaisesRegex(DoxValidationError, "past end"):
			validate_dox(bytes(document))


if __name__ == "__main__":
	unittest.main()
