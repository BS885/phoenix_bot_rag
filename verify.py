"""Verification of a model answer, in code (stage 5.4, plan section 6). No model call, no index.

verify(answer, citations, sources) -> Verification
  * every citation: the source number exists, the quote is non-empty, has no ellipsis, and appears in that source
    word for word and in one run (compared on words: niqqud, quote marks and edge punctuation are ignored)
    - a quote that is verbatim in ANOTHER source is re-attributed to it (kept, with a note)
  * every number, phone, price, date, link and e-mail in the answer appears in the sources that were validly cited
  * the caller decides what to do with the result (answer.py: no valid citation -> not_found, unsupported values -> partial
    answer without them or not_found); nothing here raises.
`sources` are the dicts of prompt.Prompt.sources ("n" and "text" are used): the very text the model saw.
"""
import re
from dataclasses import dataclass, field

QUOTE_MIN_WORDS = 2
QUOTE_MAX_WORDS = 25

_NIQQUD = re.compile(r"[֑-ׇ]")
_EDGE = " \t\r\n.,;:!?()[]{}<>\"'׳״“”‘’`-–—־"
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\s*\.\.\.\s*\]|\(\s*\.\.\.\s*\)")
_URL = re.compile(r"https?://[^\s)\]>\"'<]+|www\.[^\s)\]>\"'<]+", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")   # ASCII: a Hebrew prefix ("ל-") is not part of it
_NUMBER = re.compile(r"\*?\d(?:[\d,.\-/:]*\d)?\*?")
_LIST_MARKER = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")


def words(text: str) -> list[str]:
    """Comparison tokens: lower-cased, no niqqud, no edge punctuation / quote marks."""
    out = []
    for raw in _NIQQUD.sub("", text.lower()).split():
        w = raw.strip(_EDGE)
        if w:
            out.append(w)
    return out


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _canon_url(url: str) -> str:
    return url.strip(".,;:!?").rstrip("/").lower()


def _canon_number(tok: str) -> str:
    tok = tok.strip(".,;:-/")
    if re.fullmatch(r"\d{1,3}(,\d{3})+", tok):
        tok = tok.replace(",", "")
    return tok


def _digits(tok: str) -> str:
    return re.sub(r"\D", "", tok)


def extract_values(text: str) -> dict:
    """{"numbers": [...], "links": [...], "emails": [...]} found in a text (URLs and e-mails are taken out of the numbers)."""
    links = [_canon_url(m.group(0)) for m in _URL.finditer(text)]
    rest = _URL.sub(" ", text)
    emails = [m.group(0).lower() for m in _EMAIL.finditer(rest)]
    rest = _EMAIL.sub(" ", rest)
    rest = _LIST_MARKER.sub("", rest)
    numbers = [_canon_number(m.group(0)) for m in _NUMBER.finditer(rest)]
    return {"numbers": [n for n in numbers if n], "links": links, "emails": emails}


@dataclass
class Check:
    n: int                      # the source number the model gave
    quote: str
    ok: bool
    source: int = 0             # the source the quote really is in (differs from n when re-attributed)
    reason: str = ""            # why it failed
    note: str = ""              # remarks on an accepted quote


@dataclass
class Verification:
    citations: list[Check] = field(default_factory=list)
    used_sources: list[int] = field(default_factory=list)       # sources with at least one valid citation
    unsupported_numbers: list[str] = field(default_factory=list)
    unsupported_links: list[str] = field(default_factory=list)  # links and e-mails

    @property
    def has_valid_citation(self) -> bool:
        return bool(self.used_sources)

    @property
    def ok(self) -> bool:
        return self.has_valid_citation and not self.unsupported_numbers and not self.unsupported_links

    @property
    def hint(self) -> str:
        if not self.has_valid_citation:
            return "no_valid_citation"
        if self.unsupported_numbers or self.unsupported_links:
            return "unsupported_values"
        return "ok"


def check_citation(n: int, quote: str, source_words: dict[int, list[str]]) -> Check:
    q = str(quote or "")
    if n not in source_words:
        return Check(n, q, False, reason="bad_source_number")
    if not q.strip():
        return Check(n, q, False, reason="empty_quote")
    if _ELLIPSIS.search(q):
        return Check(n, q, False, reason="ellipsis")
    qw = words(q)
    if len(qw) < QUOTE_MIN_WORDS:
        return Check(n, q, False, reason="too_short")
    note = f"{len(qw)} words (rule: up to {QUOTE_MAX_WORDS})" if len(qw) > QUOTE_MAX_WORDS else ""
    if _contains_run(source_words[n], qw):
        return Check(n, q, True, source=n, note=note)
    for other, sw in source_words.items():
        if other != n and _contains_run(sw, qw):
            return Check(n, q, True, source=other, note=(note + "; " if note else "") + f"re-attributed from {n}")
    return Check(n, q, False, reason="not_in_source")


def verify(answer: str, citations: list[dict], sources: list[dict]) -> Verification:
    by_n = {s["n"]: s["text"] for s in sources}
    source_words = {n: words(t) for n, t in by_n.items()}
    out = Verification()
    for c in citations or []:
        try:
            n = int(c.get("source"))
        except (TypeError, ValueError, AttributeError):
            out.citations.append(Check(0, str(c.get("quote", "")) if isinstance(c, dict) else "", False,
                                       reason="bad_source_number"))
            continue
        out.citations.append(check_citation(n, c.get("quote", ""), source_words))
    out.used_sources = sorted({c.source for c in out.citations if c.ok})

    cited_text = "\n".join(by_n[n] for n in out.used_sources)
    have = extract_values(cited_text)
    have_numbers = set(have["numbers"])
    have_digits = {_digits(x) for x in have_numbers if len(_digits(x)) >= 7}            # phones in another format
    have_links = {u.removeprefix("https://").removeprefix("http://") for u in have["links"]}
    have_emails = set(have["emails"])
    want = extract_values(answer or "")
    for num in dict.fromkeys(want["numbers"]):
        if num in have_numbers or (len(_digits(num)) >= 7 and _digits(num) in have_digits):
            continue
        out.unsupported_numbers.append(num)
    for url in dict.fromkeys(want["links"]):
        if url.removeprefix("https://").removeprefix("http://") not in have_links:
            out.unsupported_links.append(url)
    for mail in dict.fromkeys(want["emails"]):
        if mail not in have_emails:
            out.unsupported_links.append(mail)
    return out
