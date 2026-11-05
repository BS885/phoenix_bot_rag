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
EXCLUDED_PATH_PREFIXES = (
    "/en/", "/tags/", "/investor-relations/", "/career/", "/404/",
    # out of scope (decision 36): legal notices of class actions and the terms-of-use / privacy documents.
    # Already saved HTML is kept on disk, but these pages are not indexed and not fetched again.
    "/claims-customer-service/class-actions/", "/customer-service/terms-of-use/",
)
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
# UI noise: a paragraph block is removed ONLY when its whole text equals one of these (exact / full regex match);
# a longer block that merely contains the string is never touched. Headings (h1-h4) are never removed.
NOISE_BLOCKS_EXACT = (
    "תגיות", "תגיות:", "טעינת כתבות נוספות", "Play", "לקבלת הצעה", "קבלת הצעה",   # tag toggle, load-more, video, CTA
    "מה חדש",                                                                         # article tag chip
    # article category chips (the label printed above a teaser card)
    "ביטוח רכב", "ביטוח בריאות", "ביטוח לחיות מחמד", "חיסכון והשקעה", "ביטוח מקיף", "ביטוח נהג צעיר",
    "תכנון פרישה לגיל שלישי", "ביטוח מחלות קשות", "ביטוחים נוספים", "קופות גמל", "אובדן כושר עבודה ונכות",
    "קרן השתלמות לעצמאים", "ביטוח מבנה", "טיפים", "מושגים",
)
NOISE_BLOCK_PATTERNS = (
    r"הצגת \d+ שאלות נוספות",          # "show N more questions"
    r"סרטון סרטון .+",                   # video popup opener: "סרטון סרטון <video title>"
)
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
# `product` = first URL segment; PRODUCT_NAMES maps it to a readable Hebrew name (used in answers / clarify).
# A segment missing here gets product_name = "" (tests fail when the corpus has an unmapped segment).
PRODUCT_NAMES = {
    "car-insurance": "ביטוח רכב",
    "travel-insurance": "ביטוח נסיעות לחו\"ל",
    "apartment-insurance": "ביטוח דירה",
    "health-insurance": "ביטוח בריאות",
    "life-insurance": "ביטוח חיים",
    "disability-and-incapacity-to-work": "אובדן כושר עבודה ונכות",
    "mortgage-insurance": "ביטוח משכנתא",
    "pets-insurance": "ביטוח לחיות מחמד",
    "business-insurance": "ביטוח עסקים",
    "truck-insurance": "ביטוח משאיות",
    "marine_aviation_insurance": "ביטוח ימי ואווירי",
    "other-insurances": "ביטוחים נוספים",
    "pension-savings": "פנסיה וביטוח מנהלים",
    "pension_retirement": "פרישה וקצבה",
    "gemel": "קופות גמל",
    "ishtalmutfund": "קרנות השתלמות",
    "savings": "חיסכון והשקעות",
    "alternativeinvestments": "השקעות אלטרנטיביות",
    "loans": "הלוואות",
    "takanottashlum": "מידע למעסיקים (דיווח והפקדות)",
    "claims-customer-service": "תביעות ושירות לקוחות",
    "customer-service": "שירות לקוחות",
}

# --- embeddings / index (stage 3) ---
# gemini-embedding-2: no task_type; the task is expressed with a text template (per Google's docs).
EMBED_MODEL = "gemini-embedding-2"
EMBED_DIM = 768                 # recommended sizes: 768 / 1536 / 3072; truncated vectors are renormalized by the API
EMBED_DOC_TEMPLATE = "title: {title} | text: {body}"
EMBED_QUERY_TEMPLATE = "task: search result | query: {query}"
EMBED_BATCH = 16                # texts per request; raise only after the batch-consistency probe passes
EMBED_BATCH_MAX_TOKENS_EST = 6000   # estimated tokens per request, under the 8192 model limit
EMBED_MODEL_MAX_TOKENS = 8192   # inputs above this are truncated silently by the API
EMBED_CHARS_PER_TOKEN = 2.0     # conservative Hebrew estimate: there is no documented count_tokens for this model
EMBED_TOKEN_WARN_RATIO = 0.9    # warn when a text is estimated above this share of the limit
EMBED_RETRIES = 5
EMBED_BACKOFF_SEC = 2.0         # doubles on every retry (429 / 5xx)
EMBED_MIN_INTERVAL_SEC = 1.0    # minimum gap between API calls; free-tier quota not documented: check AI Studio
# Free-tier quota for gemini-embedding-2 (read from AI Studio by the owner): counted PER TEXT, not per request.
EMBED_RPM = 100                 # texts per minute
EMBED_TPM = 30_000              # tokens per minute (estimated with EMBED_CHARS_PER_TOKEN, i.e. conservatively)
EMBED_RPD = 1_000               # texts per day (resets at midnight Pacific time)
EMBED_QUOTA_WAIT_SEC = 65       # a per-minute 429: wait this long, retry at most EMBED_QUOTA_RETRIES times, then stop
EMBED_QUOTA_RETRIES = 2
EMBED_USAGE_PATH = ROOT / "data" / "embed_usage.json"
EMBED_CACHE_PATH = ROOT / "data" / "embed_cache.sqlite"
CHROMA_DIR = ROOT / "data" / "chroma_db"
COLLECTION_PREFIX = "chunks"
INDEX_MANIFEST = ROOT / "data" / "index_manifest.json"
KEEP_OLD_COLLECTIONS = 1        # previous collections kept for rollback
DEDUP_NEAR_THRESHOLD = 0.03     # cosine distance; near-duplicates are only REPORTED, never merged
AREA_RANK = {"claims": 0, "customer-service": 1, "product": 2}  # which page is "primary" for merged duplicates

# --- crawl safety and waves (stage 4) ---
CRAWL_MAX_CONSECUTIVE_FAILURES = 10   # failures in a row (404 does not count) -> stop
CRAWL_MAX_FAIL_RATE = 0.25            # share of failed requests, checked after CRAWL_MIN_ATTEMPTS_FOR_RATE
CRAWL_MIN_ATTEMPTS_FOR_RATE = 40
CRAWL_MAX_BLOCK_SIGNALS = 3           # 403 / 429 / too-long Retry-After seen this many times -> stop
MAX_RETRY_AFTER_SEC = 300             # longer waits are treated as a block
SECONDS_PER_PAGE_ESTIMATE = 2.3       # measured: 1.5 s delay + ~0.8 s request
CRAWL_REPORT = LOG_DIR / "crawl_report.md"
WAVES = {
    "A": {"areas": ("claims", "customer-service")},
    "B": {"segments": ("car-insurance", "travel-insurance", "apartment-insurance", "health-insurance",
                       "mortgage-insurance", "pets-insurance", "other-insurances", "life-insurance",
                       "business-insurance", "disability-and-incapacity-to-work", "marine_aviation_insurance",
                       "truck-insurance")},
    "C": {"segments": ("pension-savings", "gemel", "savings", "loans", "takanottashlum", "ishtalmutfund",
                       "pension_retirement", "alternativeinvestments")},
}  # wave D (corporate pages, calculators, ituran, ...) is deliberately not defined: out of scope
