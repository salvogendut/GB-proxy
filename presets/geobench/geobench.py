"""GEOBENCH Browser.APP compatibility profile."""

SIMPLIFY_HTML = True

TAGS_TO_UNWRAP = [
	"article",
	"aside",
	"details",
	"figcaption",
	"figure",
	"footer",
	"header",
	"main",
	"nav",
	"noscript",
	"picture",
	"section",
	"summary",
]

TAGS_TO_STRIP = [
	"audio",
	"canvas",
	"embed",
	"iframe",
	"link",
	"object",
	"script",
	"source",
	"style",
	"template",
	"video",
]

ATTRIBUTES_TO_STRIP = [
	"aria-hidden",
	"background",
	"bgcolor",
	"class",
	"hidden",
	"id",
	"link",
	"onclick",
	"role",
	"style",
	"text",
	"vlink",
]

ALLOWED_HTML_TAGS = [
	"a",
	"b",
	"blockquote",
	"body",
	"br",
	"button",
	"caption",
	"code",
	"dd",
	"dl",
	"dt",
	"em",
	"form",
	"h1",
	"h2",
	"h3",
	"h4",
	"h5",
	"h6",
	"head",
	"hr",
	"html",
	"i",
	"img",
	"input",
	"label",
	"li",
	"ol",
	"option",
	"p",
	"pre",
	"select",
	"small",
	"strong",
	"table",
	"tbody",
	"td",
	"textarea",
	"tfoot",
	"th",
	"thead",
	"title",
	"tr",
	"tt",
	"u",
	"ul",
]

ALLOWED_HTML_ATTRIBUTES = {
	"a": ["href", "title"],
	"button": ["name", "type", "value"],
	"form": ["action", "method"],
	"img": ["alt", "src", "title"],
	"input": ["checked", "maxlength", "name", "size", "type", "value"],
	"option": ["selected", "value"],
	"select": ["name"],
	"td": ["colspan", "rowspan"],
	"textarea": ["cols", "name", "rows"],
	"th": ["colspan", "rowspan"],
}

SHORTEN_LINK_URLS = True
SHORT_IMAGE_URLS = True
# BROWSER.APP retains 23 characters of image alt text in its bounded tag parser.
MAX_IMAGE_ALT_LENGTH = 23
ASCII_ONLY = True
MINIMAL_RESPONSE_HEADERS = True

CAN_RENDER_INLINE_IMAGES = True
RESIZE_IMAGES = True
MAX_IMAGE_WIDTH = 160
MAX_IMAGE_HEIGHT = 96
CONVERT_IMAGES = True
CONVERT_IMAGES_TO_FILETYPE = "pic"
DITHERING_ALGORITHM = "FLOYDSTEINBERG"

CONVERT_CHARACTERS = True
