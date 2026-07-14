import io
import os
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from PIL import Image

from utils.image_utils import GBPC_INKS, encode_gbpc, fetch_and_cache_image, optimize_image


class GbpcEncodingTests(unittest.TestCase):
	SVG_IMAGE = (
		b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="4">'
		b'<rect width="8" height="4" fill="red"/></svg>'
	)

	@staticmethod
	def _png_bytes(width=8, height=4):
		image = Image.new("RGB", (width, height), (255, 0, 0))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		return buffer.getvalue()

	@staticmethod
	def _converter_result(image_data, returncode=0, stderr=b""):
		def run(command, **arguments):
			arguments["stdout"].write(image_data)
			arguments["stderr"].write(stderr)
			return subprocess.CompletedProcess(command, returncode)
		return run

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

		with mock.patch("utils.image_utils.subprocess.run") as run:
			encoded = optimize_image(buffer.getvalue(), convert=True, convert_to="gif")

		self.assertTrue(encoded.startswith(b"GIF8"))
		run.assert_not_called()

	def test_svg_conversion_uses_bounded_rsvg_subprocess(self):
		with mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=self._converter_result(self._png_bytes()),
		) as run:
			encoded = optimize_image(
				self.SVG_IMAGE,
				max_width=8,
				max_height=4,
				convert=True,
				convert_to="gif",
				svg_timeout=3,
			)

		self.assertTrue(encoded.startswith(b"GIF8"))
		command = run.call_args.args[0]
		self.assertEqual(command[:6], [
			"/usr/bin/rsvg-convert", "-f", "png", "-a", "-z", "1",
		])
		self.assertEqual(command[6:], ["-w", "8", "-h", "4"])
		self.assertNotIn("--unlimited", command)
		self.assertEqual(run.call_args.kwargs["input"], self.SVG_IMAGE)
		self.assertEqual(run.call_args.kwargs["timeout"], 3)
		self.assertFalse(run.call_args.kwargs["check"])
		self.assertTrue(run.call_args.kwargs["start_new_session"])

	def test_svg_render_bounds_respect_pixel_limit(self):
		with mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=self._converter_result(self._png_bytes()),
		) as run:
			optimize_image(
				self.SVG_IMAGE,
				max_width=1000,
				max_height=1000,
				max_image_pixels=100,
			)

		self.assertEqual(run.call_args.args[0][6:], ["-w", "10", "-h", "10"])

	def test_missing_svg_converter_is_reported_once(self):
		with mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=FileNotFoundError,
		), self.assertLogs("utils.image_utils", level="WARNING") as captured:
			encoded = optimize_image(self.SVG_IMAGE)

		self.assertIsNone(encoded)
		self.assertEqual(len(captured.output), 1)
		self.assertIn("install librsvg2-tools", captured.output[0])

	def test_svg_converter_timeout_is_reported(self):
		with mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=subprocess.TimeoutExpired(["rsvg-convert"], 2),
		), self.assertLogs("utils.image_utils", level="WARNING") as captured:
			encoded = optimize_image(self.SVG_IMAGE, svg_timeout=2)

		self.assertIsNone(encoded)
		self.assertIn("timed out after 2 seconds", captured.output[0])

	def test_svg_converter_error_is_sanitized(self):
		with mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=self._converter_result(b"", returncode=2, stderr=b"parse\x00\nerror"),
		), self.assertLogs("utils.image_utils", level="WARNING") as captured:
			encoded = optimize_image(self.SVG_IMAGE)

		self.assertIsNone(encoded)
		self.assertIn("status 2: parse error", captured.output[0])
		self.assertNotIn("\x00", captured.output[0])

	def test_oversized_svg_intermediate_is_rejected(self):
		with mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=self._converter_result(b"x" * 1025),
		), self.assertLogs("utils.image_utils", level="WARNING") as captured:
			encoded = optimize_image(self.SVG_IMAGE, max_intermediate_bytes=1024)

		self.assertIsNone(encoded)
		self.assertIn("1024-byte intermediate limit", captured.output[0])

	def test_cached_svg_failure_has_one_url_aware_warning(self):
		with tempfile.TemporaryDirectory() as directory, mock.patch(
			"utils.image_utils.subprocess.run",
			side_effect=FileNotFoundError,
		) as run, self.assertLogs("utils.image_utils", level="WARNING") as captured:
			cached_url = fetch_and_cache_image(
				"https://example.com/logo.svg",
				self.SVG_IMAGE,
				cache_dir=directory,
				timeout=30,
				svg_timeout=4,
			)
			cache_contents = os.listdir(directory)

		self.assertIsNone(cached_url)
		self.assertEqual(cache_contents, [])
		self.assertEqual(len(captured.output), 1)
		self.assertIn("https://example.com/logo.svg", captured.output[0])
		self.assertIn("install librsvg2-tools", captured.output[0])
		self.assertEqual(run.call_args.kwargs["timeout"], 4)

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
