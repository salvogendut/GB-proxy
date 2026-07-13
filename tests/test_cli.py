import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from gb_proxy.cli import build_parser, main


class CommandLineTests(unittest.TestCase):
	def test_help_does_not_require_configuration(self):
		with self.assertRaises(SystemExit) as exit_context:
			build_parser().parse_args(["--help"])
		self.assertEqual(exit_context.exception.code, 0)

	def test_missing_configuration_returns_nonzero(self):
		result = main([
			"--config",
			"/definitely/missing/gb-proxy-config.py",
			"--check-config",
		])
		self.assertEqual(result, 2)

	def test_invalid_environment_port_is_a_command_line_error(self):
		with mock.patch.dict("os.environ", {"GB_PROXY_PORT": "not-a-port"}):
			with self.assertRaises(SystemExit) as exit_context:
				build_parser().parse_args([])
		self.assertEqual(exit_context.exception.code, 2)

	def test_invalid_log_level_returns_a_non_restartable_config_error(self):
		with mock.patch.dict(os.environ, {"GB_PROXY_LOG_LEVEL": "bogus"}):
			with mock.patch("gb_proxy.cli.load_config") as load_config:
				result = main(["--check-config"])

		self.assertEqual(result, 2)
		load_config.assert_not_called()

	def test_waitress_receives_the_client_body_and_thread_limits(self):
		captured = {}

		def serve(app, **kwargs):
			captured.update(kwargs)

		repository_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		with tempfile.TemporaryDirectory() as directory:
			with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
				sys.modules, {"waitress": SimpleNamespace(serve=serve)}
			):
				result = main([
					"--config",
					os.path.join(repository_root, "config.py.example"),
					"--cache-dir",
					os.path.join(directory, "cache"),
					"--state-dir",
					os.path.join(directory, "state"),
				])
		sys.modules.pop("config", None)

		self.assertEqual(result, 0)
		self.assertEqual(captured["threads"], 1)
		self.assertEqual(captured["max_request_body_size"], 1024 * 1024 + 1)


if __name__ == "__main__":
	unittest.main()
