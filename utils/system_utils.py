"""Configuration and preset loading helpers."""

import importlib
import importlib.machinery
import importlib.util
import logging
import os
import re
import sys


LOGGER = logging.getLogger(__name__)
_PRESET_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_OVERRIDE_VARS = (
	"SIMPLIFY_HTML",
	"TAGS_TO_STRIP",
	"TAGS_TO_UNWRAP",
	"ATTRIBUTES_TO_STRIP",
	"ALLOWED_HTML_TAGS",
	"ALLOWED_HTML_ATTRIBUTES",
	"SHORTEN_LINK_URLS",
	"SHORT_IMAGE_URLS",
	"MAX_IMAGE_ALT_LENGTH",
	"ASCII_ONLY",
	"MINIMAL_RESPONSE_HEADERS",
	"CAN_RENDER_INLINE_IMAGES",
	"RESIZE_IMAGES",
	"MAX_IMAGE_WIDTH",
	"MAX_IMAGE_HEIGHT",
	"CONVERT_IMAGES",
	"CONVERT_IMAGES_TO_FILETYPE",
	"DITHERING_ALGORITHM",
	"WEB_SIMULATOR_PROMPT_ADDENDUM",
	"CONVERT_CHARACTERS",
	"CONVERSION_TABLE",
)


class ConfigurationError(RuntimeError):
	"""Raised when GB-proxy cannot load a usable configuration."""


def _load_module_from_path(path):
	path = os.path.abspath(os.path.expanduser(path))
	if not os.path.isfile(path):
		raise ConfigurationError(f"Configuration file not found: {path}")

	loader = importlib.machinery.SourceFileLoader("_gb_proxy_user_config", path)
	spec = importlib.util.spec_from_file_location(
		"_gb_proxy_user_config",
		path,
		loader=loader,
	)
	if spec is None or spec.loader is None:
		raise ConfigurationError(f"Could not create a module for configuration: {path}")

	module = importlib.util.module_from_spec(spec)
	try:
		spec.loader.exec_module(module)
	except Exception as error:
		raise ConfigurationError(f"Could not load configuration {path}: {error}") from error

	module.CONFIG_PATH = path
	sys.modules["config"] = module
	return module


def apply_preset(config):
	"""Overlay the selected compatibility preset onto a config module."""
	if getattr(config, "_GB_PROXY_PRESET_APPLIED", False):
		return config

	preset_name = getattr(config, "PRESET", None)
	if not preset_name:
		config._GB_PROXY_PRESET_APPLIED = True
		return config
	if not isinstance(preset_name, str) or not _PRESET_NAME.fullmatch(preset_name):
		raise ConfigurationError(f"Invalid preset name: {preset_name!r}")

	module_name = f"presets.{preset_name}.{preset_name}"
	try:
		preset_module = importlib.import_module(module_name)
	except ModuleNotFoundError as error:
		if error.name == module_name or (error.name and module_name.startswith(error.name + ".")):
			raise ConfigurationError(f"Preset not found: {preset_name}") from error
		raise ConfigurationError(
			f"Could not import preset {preset_name}: missing dependency {error.name}"
		) from error
	except Exception as error:
		raise ConfigurationError(f"Could not load preset {preset_name}: {error}") from error

	changed = []
	for name in _OVERRIDE_VARS:
		if not hasattr(preset_module, name):
			continue
		value = getattr(preset_module, name)
		if not hasattr(config, name) or getattr(config, name) != value:
			setattr(config, name, value)
			changed.append(name)

	config._GB_PROXY_PRESET_APPLIED = True
	LOGGER.info("Loaded preset %s%s", preset_name, f" ({', '.join(changed)})" if changed else "")
	return config


def load_config(path):
	"""Load an explicit Python configuration file and apply its preset."""
	return apply_preset(_load_module_from_path(path))


def load_preset(config_path=None):
	"""Compatibility loader for callers that previously imported local config.py."""
	if config_path:
		return load_config(config_path)

	environment_path = os.environ.get("GB_PROXY_CONFIG")
	if environment_path:
		return load_config(environment_path)

	config = sys.modules.get("config")
	if config is None:
		try:
			config = importlib.import_module("config")
		except ModuleNotFoundError as error:
			if error.name == "config":
				raise ConfigurationError(
					"No configuration found; pass --config or set GB_PROXY_CONFIG"
				) from error
			raise
	return apply_preset(config)
