"""Install a complete in-memory proxy configuration for tests."""

import sys
import types


def install_config(preset="geobench"):
	config = sys.modules.get("config")
	if config is not None:
		return config

	config = types.ModuleType("config")
	config.PRESET = preset
	config.ENABLED_EXTENSIONS = []
	config.WHITELISTED_DOMAINS = []
	config.SIMPLIFY_HTML = True
	config.TAGS_TO_UNWRAP = ["noscript"]
	config.TAGS_TO_STRIP = ["script", "link", "style", "source"]
	config.ATTRIBUTES_TO_STRIP = ["style", "onclick", "class"]
	config.ALLOWED_HTML_TAGS = None
	config.ALLOWED_HTML_ATTRIBUTES = None
	config.SHORTEN_LINK_URLS = False
	config.SHORT_IMAGE_URLS = False
	config.ASCII_ONLY = False
	config.MINIMAL_RESPONSE_HEADERS = False
	config.CAN_RENDER_INLINE_IMAGES = False
	config.RESIZE_IMAGES = True
	config.MAX_IMAGE_WIDTH = 512
	config.MAX_IMAGE_HEIGHT = 342
	config.CONVERT_IMAGES = True
	config.CONVERT_IMAGES_TO_FILETYPE = "gif"
	config.DITHERING_ALGORITHM = "FLOYDSTEINBERG"
	config.WEB_SIMULATOR_PROMPT_ADDENDUM = ""
	config.CONVERT_CHARACTERS = True
	config.CONVERSION_TABLE = {
		"\u2014": b"-",
		"\u00a0": b" ",
	}
	sys.modules["config"] = config
	return config
