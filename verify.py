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


# ---------- claims: one claim = text + source + quote (stage 5.4b) ----------

_PREFIX_LETTERS = "והבלמשכ"
# Truly gendered second-person forms (stylistic check only; a hit asks for ONE rephrase, it never removes a claim).
# "עליך", "בפניך", "שברשותך", "לך" and the like are NOT here: the site itself uses them and a claim may too. "את" is not here
# either: as the object marker ("את הטופס") it is one of the most common words. Recognised after one or two prefix letters.
_SINGULAR_FORMS = {"אתה", "תוכל", "תוכלי", "תרצה", "תרצי", "הינך", "הנך", "תצטרך", "תצטרכי", "ביכולתך"}
LINK_ONLY_MAX_WORDS = 6        # a source body with at most this many words outside links is a "heading + link" source


def stem(word: str) -> str:
    """Crude Hebrew stem for overlap: one leading prefix letter is dropped from words of 4+ letters."""
    return word[1:] if len(word) >= 4 and word[0] in _PREFIX_LETTERS else word


def content_words(text: str) -> list[str]:
    return [stem(w) for w in words(text) if len(w) > 1]


def overlap(text: str, quote: str) -> float:
    """Shared stems / the smaller of the two stem sets (0 when either is empty)."""
    tw, qw = set(content_words(text)), set(content_words(quote))
    if not tw or not qw:
        return 0.0
    return len(tw & qw) / min(len(tw), len(qw))


def singular_forms(text: str) -> list[str]:
    """Truly gendered second-person forms in a claim text ('אתה', 'תוכל', 'תרצי', 'הינך' ...)."""
    found = []
    for w in words(text):
        candidates = {w, w[1:] if len(w) > 2 and w[0] in _PREFIX_LETTERS else w,
                      w[2:] if len(w) > 3 and w[0] in _PREFIX_LETTERS and w[1] in _PREFIX_LETTERS else w}
        if candidates & _SINGULAR_FORMS:
            found.append(w)
    return found


def _has_word(token_list: list[str], word: str) -> bool:
    return any(t == word or (len(t) > 2 and t[0] in _PREFIX_LETTERS and t[1:] == word) for t in token_list)


def strength_words_added(text: str, quote: str) -> list[str]:
    """Strength words ('מומלץ', 'כדאי') in a claim text that are not in its quote: the claim would be softer / stronger
    than the source ('יש למלא' must not become 'מומלץ למלא')."""
    import config
    tw, qw = words(text), words(quote)
    return [w for w in config.CLAIM_STRENGTH_WORDS if _has_word(tw, w) and not _has_word(qw, w)]


def _unsupported(text: str, source_text: str) -> tuple[list[str], list[str]]:
    """(numbers, links+emails) of `text` that do not appear in `source_text`."""
    have = extract_values(source_text)
    have_numbers = set(have["numbers"])
    have_digits = {_digits(x) for x in have_numbers if len(_digits(x)) >= 7}
    have_links = {u.removeprefix("https://").removeprefix("http://") for u in have["links"]}
    have_emails = set(have["emails"])
    want = extract_values(text)
    bad_n = [n for n in dict.fromkeys(want["numbers"])
             if not (n in have_numbers or (len(_digits(n)) >= 7 and _digits(n) in have_digits))]
    bad_l = [u for u in dict.fromkeys(want["links"]) if u.removeprefix("https://").removeprefix("http://") not in have_links]
    bad_l += [m for m in dict.fromkeys(want["emails"]) if m not in have_emails]
    return bad_n, bad_l


def source_label(source: dict) -> str:
    """The label line the prompt shows above a source (prompt.build): 'page > section', or just the page title."""
    title, section = source.get("title", ""), source.get("section", "")
    if not title and not section:
        return ""
    return f"{title} > {section}" if section and section != title else (title or section)


def is_link_only(source_text: str) -> bool:
    """A source that is just a heading + link(s): few words once the links are taken out."""
    rest = _URL.sub(" ", source_text)
    return bool(_URL.search(source_text)) and len(words(rest)) <= LINK_ONLY_MAX_WORDS


@dataclass
class ClaimResult:
    text: str
    n: int
    quote: str
    ok: bool
    source: int = 0
    reason: str = ""
    note: str = ""
    style: str = ""             # a style issue that does not remove the claim (gendered form): shown as is unless rephrased
    final_text: str = ""        # text as shown (a link of a heading+link source is appended when the claim has none)


@dataclass
class ClaimsVerification:
    claims: list[ClaimResult] = field(default_factory=list)

    @property
    def valid(self) -> list[ClaimResult]:
        return [c for c in self.claims if c.ok]

    @property
    def dropped(self) -> list[ClaimResult]:
        return [c for c in self.claims if not c.ok]

    @property
    def used_sources(self) -> list[int]:
        return sorted({c.source for c in self.valid})


def verify_claims(claims: list[dict], sources: list[dict], *, min_overlap: float | None = None) -> ClaimsVerification:
    """Each claim is checked on its own: quote verbatim in its source (a heading+link source may be quoted by its title),
    enough word overlap between text and quote, no second-person singular in the text, and every number / phone / link
    of the text in the quote or in the source text. A failing claim is dropped; the others stay."""
    import config
    min_overlap = config.CLAIM_MIN_OVERLAP if min_overlap is None else min_overlap
    by_n = {s["n"]: s["text"] for s in sources}
    # What the model saw for a source is its label line ("page > section") and then the body: a quote may be a contiguous run
    # of that whole text (it may cross from the label into the body), but never a join of two distant parts.
    source_words = {s["n"]: words(source_label(s)) + words(s["text"]) for s in sources}
    link_only = {n for n, t in by_n.items() if is_link_only(t)}
    title_words = {s["n"]: [words(s.get("section", "")), words(s.get("title", "")),
                            words(f"{s.get('title', '')} {s.get('section', '')}")] for s in sources}
    out = ClaimsVerification()
    seen = set()
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text", "") or "").strip()
        quote = str(c.get("quote", "") or "")
        try:
            n = int(c.get("source"))
        except (TypeError, ValueError):
            out.claims.append(ClaimResult(text, 0, quote, False, reason="bad_source_number"))
            continue
        if not text:
            out.claims.append(ClaimResult(text, n, quote, False, reason="empty_text"))
            continue
        if text in seen:
            continue
        seen.add(text)
        chk = check_citation(n, quote, source_words)
        if not chk.ok and n in link_only and not _ELLIPSIS.search(quote):      # a heading+link source: the title is the quote
            qw = words(quote)
            if qw and any(_contains_run(tw, qw) for tw in title_words.get(n, [])):
                chk = Check(n, quote, True, source=n, note="quote is the title of a heading+link source")
        if not chk.ok:
            out.claims.append(ClaimResult(text, n, quote, False, reason=chk.reason))
            continue
        res = ClaimResult(text, n, quote, True, source=chk.source, note=chk.note, final_text=text)
        sing = singular_forms(text)
        soft = strength_words_added(text, quote)
        if soft:
            res.ok, res.reason, res.note = False, "strength_word", ", ".join(soft)
        elif overlap(text, quote) < min_overlap:
            res.ok, res.reason, res.note = False, "low_overlap", f"{overlap(text, quote):.2f} < {min_overlap}"
        else:
            bad_n, bad_l = _unsupported(text, by_n[chk.source])
            if bad_n or bad_l:
                res.ok, res.reason, res.note = False, "unsupported_value", ", ".join(bad_n + bad_l)
        if res.ok and sing:
            res.style = "singular_form: " + ", ".join(sing)
        if res.ok and chk.source in link_only and not _URL.search(text):          # the link comes from the source
            link = _URL.search(by_n[chk.source]).group(0).rstrip(".,;:!?")
            res.final_text = f"{text} ({link})"
        out.claims.append(res)
    return out
