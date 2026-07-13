"""Command-line entry point for GB-proxy."""

import argparse
import logging
import os
import socket
import sys

from werkzeug.serving import get_interface_ip

from gb_proxy import __version__
from gb_proxy.application import create_app
from utils.image_utils import default_cache_dir
from utils.system_utils import ConfigurationError, load_config


def _existing_default_config():
	explicit = os.environ.get("GB_PROXY_CONFIG")
	if explicit:
		return explicit
	local_config = os.path.abspath("config.py")
	if os.path.isfile(local_config):
		return local_config
	return "/etc/gb-proxy/config.py"


def _default_state_dir():
	configured = os.environ.get("GB_PROXY_STATE_DIR")
	if configured:
		return configured
	state_home = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
	return os.path.join(state_home, "gb-proxy")


def _display_host(bind_host):
	if bind_host == "0.0.0.0":
		return get_interface_ip(socket.AF_INET)
	if bind_host == "::":
		return get_interface_ip(socket.AF_INET6)
	return bind_host


def _advertise_url(advertise_host, bind_host, port):
	host = advertise_host or _display_host(bind_host)
	if host.startswith("http://"):
		return host.rstrip("/")
	if ":" in host and not host.startswith("["):
		host = f"[{host}]"
	return f"http://{host}:{port}"


def build_parser():
	parser = argparse.ArgumentParser(
		prog="gb-proxy",
		description="Transcoding HTTP proxy for GEOBENCH and legacy web clients",
	)
	parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
	parser.add_argument(
		"--config",
		default=_existing_default_config(),
		help="Python configuration file (default: GB_PROXY_CONFIG, ./config.py, or /etc/gb-proxy/config.py)",
	)
	parser.add_argument(
		"--host",
		default=os.environ.get("GB_PROXY_HOST", "127.0.0.1"),
		help="address on which to listen (default: 127.0.0.1)",
	)
	parser.add_argument(
		"--port",
		type=int,
		default=os.environ.get("GB_PROXY_PORT", "5001"),
		help="TCP port on which to listen (default: 5001)",
	)
	parser.add_argument(
		"--advertise-host",
		default=os.environ.get("GB_PROXY_ADVERTISE_HOST"),
		help="host or http URL embedded in rewritten links",
	)
	parser.add_argument(
		"--cache-dir",
		default=os.environ.get("GB_PROXY_CACHE_DIR", default_cache_dir()),
		help="directory for converted images",
	)
	parser.add_argument(
		"--state-dir",
		default=_default_state_dir(),
		help="directory for persistent extension state",
	)
	parser.add_argument(
		"--threads",
		type=int,
		default=os.environ.get("GB_PROXY_THREADS", "1"),
		help="number of server threads in the single worker process (default: 1)",
	)
	parser.add_argument(
		"--check-config",
		action="store_true",
		help="validate configuration and extensions, then exit",
	)
	return parser


def main(argv=None):
	arguments = build_parser().parse_args(argv)
	log_level_name = os.environ.get("GB_PROXY_LOG_LEVEL", "INFO").upper()
	log_level = getattr(logging, log_level_name, None)
	if not isinstance(log_level, int):
		logging.basicConfig(level=logging.ERROR)
		logging.error("GB_PROXY_LOG_LEVEL must name a standard Python logging level")
		return 2
	logging.basicConfig(
		level=log_level,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)

	if not 1 <= arguments.port <= 65535:
		logging.error("Port must be between 1 and 65535")
		return 2
	if arguments.threads < 1:
		logging.error("Thread count must be positive")
		return 2

	try:
		settings = load_config(arguments.config)
		app = create_app(
			settings,
			cache_dir=arguments.cache_dir,
			state_dir=arguments.state_dir,
			advertise_url=_advertise_url(
				arguments.advertise_host,
				arguments.host,
				arguments.port,
			),
		)
	except ConfigurationError as error:
		logging.error("%s", error)
		return 2
	except OSError as error:
		logging.error("Could not initialize runtime directories: %s", error)
		return 2

	if arguments.check_config:
		logging.info("Configuration is valid")
		return 0

	try:
		from waitress import serve
	except ImportError:
		logging.error("Waitress is required to run GB-proxy")
		return 1

	logging.info(
		"Starting GB-proxy %s on %s:%s with %s threads",
		__version__,
		arguments.host,
		arguments.port,
		arguments.threads,
	)
	serve(
		app,
		host=arguments.host,
		port=arguments.port,
		threads=arguments.threads,
		# Waitress 1.x rejects once the received size is >= this adjustment;
		# Flask's MAX_CONTENT_LENGTH allows a body exactly at the configured limit.
		max_request_body_size=app.config["MAX_CONTENT_LENGTH"] + 1,
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
