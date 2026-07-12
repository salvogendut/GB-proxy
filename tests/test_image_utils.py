import io
import struct
import unittest

from PIL import Image

from utils.image_utils import GBPC_INKS, encode_gbpc, optimize_image


class GbpcEncodingTests(unittest.TestCase):
	def test_exact_header_palette_and_mode1_packing(self):
		image = Image.new("RGB", (4, 1))
		image.putdata([
			(0x00, 0x00, 0x80),
			(0xFF, 0xFF, 0xFF),
			(0x00, 0x00, 0x00),
			(0xFF, 0x00, 0x00),
		])

		encoded = encode_gbpc(image, dithering="none")

		self.assertEqual(encoded[:6], b"GBPC\x02\x01")
		self.assertEqual(struct.unpack("<HH", encoded[6:10]), (4, 1))
		self.assertEqual(encoded[10:14], GBPC_INKS)
		self.assertEqual(encoded[14:], b"\x53")

	def test_pic_resize_preserves_aspect_and_multiple_of_four(self):
		image = Image.new("RGB", (321, 100), (255, 0, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")

		encoded = optimize_image(
			buffer.getvalue(),
			resize=True,
			max_width=160,
			max_height=96,
			convert=True,
			convert_to="pic",
			dithering="none",
		)
		width, height = struct.unpack("<HH", encoded[6:10])

		self.assertEqual(width, 160)
		self.assertEqual(height, 50)
		self.assertEqual(len(encoded), 14 + (width // 4) * height)

	def test_legacy_gif_conversion_still_works(self):
		image = Image.new("RGB", (8, 8), (255, 255, 255))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")

		encoded = optimize_image(buffer.getvalue(), convert=True, convert_to="gif")

		self.assertTrue(encoded.startswith(b"GIF8"))


if __name__ == "__main__":
	unittest.main()
