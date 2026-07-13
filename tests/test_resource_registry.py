import unittest
from unittest.mock import patch

from utils.resource_registry import (
	clear_resources,
	configure_resources,
	register_resource,
	resolve_resource,
)


class ResourceRegistryTests(unittest.TestCase):
	def setUp(self):
		clear_resources()
		configure_resources(max_entries=2, ttl_seconds=10)

	def tearDown(self):
		clear_resources()
		configure_resources(max_entries=4096, ttl_seconds=3600)

	def test_oldest_resource_is_evicted_at_capacity(self):
		first = register_resource("url", "https://example.com/first")
		second = register_resource("url", "https://example.com/second")
		third = register_resource("url", "https://example.com/third")

		self.assertIsNone(resolve_resource("url", first))
		self.assertEqual(resolve_resource("url", second).target, "https://example.com/second")
		self.assertEqual(resolve_resource("url", third).target, "https://example.com/third")

	def test_expired_resource_is_not_resolved(self):
		with patch("utils.resource_registry.time.monotonic", return_value=100):
			token = register_resource("image", "https://example.com/image.png")

		with patch("utils.resource_registry.time.monotonic", return_value=111):
			self.assertIsNone(resolve_resource("image", token))

	def test_oversized_inline_content_is_rejected(self):
		configure_resources(max_entries=2, ttl_seconds=10, max_content_bytes=3)
		with self.assertRaises(ValueError):
			register_resource("image", "inline-image:test", b"four")


if __name__ == "__main__":
	unittest.main()
