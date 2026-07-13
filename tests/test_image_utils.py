import io
import os
import struct
import tempfile
import unittest

from PIL import Image

from utils.image_utils import GBPC_INKS, encode_gbpc, fetch_and_cache_image, optimize_image


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

	def test_decoded_images_over_the_pixel_limit_are_rejected(self):
		image = Image.new("RGB", (5, 5), (255, 255, 255))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")

		encoded = optimize_image(buffer.getvalue(), max_image_pixels=24)

		self.assertIsNone(encoded)

	def test_cache_key_includes_transformation_settings(self):
		image = Image.new("RGB", (8, 8), (255, 255, 255))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")

		with tempfile.TemporaryDirectory() as directory:
			first = fetch_and_cache_image(
				"https://example.com/image.png",
				buffer.getvalue(),
				max_width=8,
				cache_dir=directory,
			)
			second = fetch_and_cache_image(
				"https://example.com/image.png",
				buffer.getvalue(),
				max_width=4,
				cache_dir=directory,
			)

			self.assertNotEqual(first, second)
			self.assertTrue(os.path.isfile(os.path.join(directory, os.path.basename(first))))
			self.assertTrue(os.path.isfile(os.path.join(directory, os.path.basename(second))))

	def test_cache_evicts_oldest_file_at_configured_limit(self):
		image = Image.new("RGB", (4, 4), (255, 255, 255))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")

		with tempfile.TemporaryDirectory() as directory:
			for number in range(2):
				fetch_and_cache_image(
					f"https://example.com/image-{number}.png",
					buffer.getvalue(),
					cache_dir=directory,
					max_cache_files=1,
				)

			self.assertEqual(len(os.listdir(directory)), 1)

	def test_output_larger_than_the_cache_quota_is_rejected(self):
		with tempfile.TemporaryDirectory() as directory:
			cached_url = fetch_and_cache_image(
				"https://example.com/image.png",
				b"four",
				resize=False,
				convert=False,
				cache_dir=directory,
				max_download_bytes=4,
				max_cache_bytes=3,
			)

			self.assertIsNone(cached_url)
			self.assertEqual(os.listdir(directory), [])


if __name__ == "__main__":
	unittest.main()
