import os
import sys
import tempfile
import unittest

from utils.system_utils import ConfigurationError, load_config


class ConfigurationLoadingTests(unittest.TestCase):
	def tearDown(self):
		sys.modules.pop("config", None)

	def test_explicit_configuration_path_is_loaded_and_aliased(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "service-config.py")
			with open(path, "w", encoding="utf-8") as config_file:
				config_file.write("PRESET = None\nENABLED_EXTENSIONS = []\n")

			config = load_config(path)

		self.assertEqual(config.ENABLED_EXTENSIONS, [])
		self.assertIs(sys.modules["config"], config)

	def test_missing_configuration_raises_startup_error(self):
		with self.assertRaises(ConfigurationError):
			load_config("/definitely/missing/gb-proxy-config.py")

	def test_invalid_preset_raises_startup_error(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "service-config.py")
			with open(path, "w", encoding="utf-8") as config_file:
				config_file.write("PRESET = 'does-not-exist'\nENABLED_EXTENSIONS = []\n")

			with self.assertRaises(ConfigurationError):
				load_config(path)


if __name__ == "__main__":
	unittest.main()
