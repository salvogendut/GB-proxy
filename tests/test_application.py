import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import requests

from gb_proxy.application import create_app, domain_matches
from tests.config_stub import install_config


class ApplicationFactoryTests(unittest.TestCase):
	def test_imports_do_not_require_config_or_write_runtime_files(self):
		repository_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
		with tempfile.TemporaryDirectory() as home:
			environment = os.environ.copy()
			environment.pop("GB_PROXY_CONFIG", None)
			environment.pop("GB_PROXY_CACHE_DIR", None)
			environment.pop("GB_PROXY_STATE_DIR", None)
			environment.update(
				HOME=home,
				PYTHONDONTWRITEBYTECODE="1",
				PYTHONPATH=repository_root,
			)
			result = subprocess.run(
				[sys.executable, "-c", "import proxy, utils.html_utils, utils.image_utils"],
				cwd=home,
				env=environment,
				capture_output=True,
				text=True,
				check=False,
			)

			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertEqual(os.listdir(home), [])

	def test_domain_matching_requires_a_label_boundary(self):
		self.assertTrue(domain_matches("reddit.com", "reddit.com"))
		self.assertTrue(domain_matches("old.reddit.com", "reddit.com"))
		self.assertFalse(domain_matches("evilreddit.com", "reddit.com"))

	def test_factory_uses_explicit_runtime_directories_and_advertised_url(self):
		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(),
				cache_dir=f"{directory}/cache",
				state_dir=f"{directory}/state",
				advertise_url="http://192.0.2.10:5001",
			)

		self.assertEqual(app.config["GB_PROXY_ADVERTISE_URL"], "http://192.0.2.10:5001")
		self.assertEqual(app.config["MACPROXY_HOST_AND_PORT"], "192.0.2.10:5001")

	def test_factory_does_not_clear_an_existing_cache(self):
		with tempfile.TemporaryDirectory() as directory:
			cache_dir = os.path.join(directory, "cache")
			os.makedirs(cache_dir)
			sentinel = os.path.join(cache_dir, "sentinel")
			with open(sentinel, "w", encoding="utf-8") as sentinel_file:
				sentinel_file.write("keep")

			create_app(
				install_config(),
				cache_dir=cache_dir,
				state_dir=os.path.join(directory, "state"),
			)

			self.assertTrue(os.path.isfile(sentinel))

	def test_oversized_upstream_response_is_rejected(self):
		with tempfile.TemporaryDirectory() as directory:
			config = install_config()
			original_limit = getattr(config, "MAX_UPSTREAM_RESPONSE_BYTES", None)
			config.MAX_UPSTREAM_RESPONSE_BYTES = 3
			upstream = SimpleNamespace(
				content=b"four",
				status_code=200,
				headers={"Content-Type": "text/plain"},
				url="http://example.com/",
			)
			app = create_app(
				config,
				cache_dir=directory,
				state_dir=directory,
				request_callable=lambda *args, **kwargs: upstream,
			)
			response = app.test_client().get("/", base_url="http://example.com")
			if original_limit is None:
				del config.MAX_UPSTREAM_RESPONSE_BYTES
			else:
				config.MAX_UPSTREAM_RESPONSE_BYTES = original_limit

		self.assertEqual(response.status_code, 502)

	def test_each_inbound_request_uses_and_closes_a_fresh_session(self):
		created_sessions = []

		class FakeSession:
			def __init__(self):
				self.closed = False
				created_sessions.append(self)

			def request(self, method, url, **kwargs):
				return SimpleNamespace(
					content=b"ok",
					status_code=200,
					headers={"Content-Type": "text/plain"},
					url=url,
				)

			def close(self):
				self.closed = True

		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				session_factory=FakeSession,
			)
			client = app.test_client()
			self.assertEqual(client.get("/one", base_url="http://example.com").status_code, 200)
			self.assertEqual(client.get("/two", base_url="http://example.com").status_code, 200)

		self.assertEqual(len(created_sessions), 2)
		self.assertTrue(all(session.closed for session in created_sessions))

	def test_upstream_timeout_returns_gateway_timeout(self):
		def timeout_request(*args, **kwargs):
			raise requests.Timeout("too slow")

		with tempfile.TemporaryDirectory() as directory:
			app = create_app(
				install_config(),
				cache_dir=directory,
				state_dir=directory,
				request_callable=timeout_request,
			)
			response = app.test_client().get("/", base_url="http://example.com")

		self.assertEqual(response.status_code, 504)


if __name__ == "__main__":
	unittest.main()
