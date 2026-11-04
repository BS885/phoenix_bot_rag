"""All project parameters in one place."""
from pathlib import Path

ROOT = Path(__file__).parent
RAW_HTML_DIR = ROOT / "data" / "raw_html"
FETCH_INDEX = RAW_HTML_DIR / "index.json"
LOG_DIR = ROOT / "logs"
FETCH_LOG = LOG_DIR / "fetch.log"

# --- crawling ---
BASE_URL = "https://www.fnx.co.il"
ALLOWED_HOST = "www.fnx.co.il"
ROBOTS_URL = BASE_URL + "/robots.txt"
FALLBACK_SITEMAP_URL = BASE_URL + "/sitemap.xml"
USER_AGENT = "PhoenixBotResearch/0.1"
REQUEST_DELAY_SEC = 1.5
TIMEOUT = (5, 30)  # (connect, read)
MAX_RETRIES = 3
SAMPLE_SIZE = 10

# --- URL filtering ---
EXCLUDED_PATH_PREFIXES = ("/en/", "/tags/", "/investor-relations/", "/career/", "/404/")
EXCLUDED_QUERY_PARAMS = ("page", "tag", "domain", "staff")
EXCLUDED_PATH_SUFFIXES = ("-old",)  # pages whose last path segment ends with this

# Lower number = higher priority. First matching prefix wins; others get DEFAULT_PRIORITY.
# Groups 2 and 3 (insurance products, pension/savings) are TBD until the real sitemap is reviewed.
PRIORITY_PREFIXES = {
    1: ("/claims-customer-service/", "/customer-service/"),
}
DEFAULT_PRIORITY = 3

# --- cleaning / chunking ---
INDICATOR_PATTERNS = (r"^שקופית(?: מספר)? \d+ מתוך \d+$", r"^slide \d+ of \d+$")  # carousel position labels
MIN_PAGE_CHARS = 200         # below this after cleaning the page is marked empty
STRIP_REPEATED_BLOCKS = False  # off: it also removes useful repeated lines (contact details) on a claims-heavy sample
# sections dropped entirely, matched by heading prefix: the contact box repeated on every page,
# and article teasers (titles only, no content)
BOILERPLATE_HEADINGS = ("כל הדרכים לדבר איתנו", "אולי יעניין אותך")
DROP_LINK_ONLY_SECTIONS = True  # a section made only of links, with no sentence, is not indexed
LINK_ONLY_EXEMPT_AREAS = ("claims",)  # ...except in these areas: there the links ARE the answer (claim forms)
CTA_LABELS = ("הגשת תביעה",)  # a block that is only this button label is dropped (the link block carries it)
ROW_TITLE_STRIP = ("מידע נוסף על המסלול",)  # UI text removed from a table row's name cell
REPEAT_THRESHOLD = 0.2       # block appearing in more than this share of pages is boilerplate
REPEAT_MIN_PAGES = 5          # do not strip repeated blocks on tiny corpora
MAX_CHUNK_WORDS = 250
MIN_CHUNK_WORDS = 60
CHUNK_OVERLAP_SENTENCES = 1   # only when a single paragraph over MAX_CHUNK_WORDS is cut
AREA_BY_PREFIX = {
    "/claims-customer-service/": "claims",
    "/customer-service/": "customer-service",
}
DEFAULT_AREA = "product"
