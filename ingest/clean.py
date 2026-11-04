"""Isolate the content area of a saved page and return it as a flat list of text blocks."""
import re
from collections import Counter
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString

import config

# Removed on purpose: navigation (menus, breadcrumbs, in-page anchors, tabs), scripts, buttons.
# header/footer are NOT removed: the h1/h2 headings live in <header> inside <main>.
_NOISE_TAGS = ["script", "style", "nav", "noscript", "svg", "button", "aside"]
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
_BLOCK_OWNERS = {"p", "li", "td", "th", "h1", "h2", "h3", "h4", "a"}  # their text is handled on their own
_INLINE = {"span", "b", "strong", "em", "i", "u", "small", "sup", "sub", "font", "mark", "br", "label"}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏﻿"))
_HAS_LETTER = re.compile(r"[A-Za-z֐-׿]")
_INDICATORS = [re.compile(p) for p in config.INDICATOR_PATTERNS]


_LETTER_RUN = re.compile(r"(?<![א-ת])(?:[א-ת] ){3,}[א-ת](?![א-ת])")
_EMPTY_INLINE = ["span", "b", "strong", "i", "em", "u", "mark", "font", "small", "sup", "sub"]


def _norm(text: str) -> str:
    text = re.sub(r"\s+", " ", text.translate(_ZERO_WIDTH).replace("\xa0", " ")).strip()
    # safety net: a run of 4+ single Hebrew letters separated by spaces ("ל ע ד כ ן") is a word that was
    # split letter by letter; word boundaries inside such a run are unknowable, so the run is joined
    return _LETTER_RUN.sub(lambda m: m.group().replace(" ", ""), text)


def _drop_empty_inline(root) -> None:
    """Remove empty inline tags and merge the text nodes around them.

    Search-highlight widgets put an empty <b><span class="hightlight"></span></b> between every two
    letters; get_text(' ') then turns each letter into its own word. After removing the empty tags the
    letters are adjacent text nodes, smooth() merges them, and the real spaces are kept.
    """
    for tag in root.find_all(_EMPTY_INLINE):
        if not tag.get_text(strip=True) and not tag.find(["img", "input", "a", "svg"]):
            tag.decompose()
    root.smooth()


def is_indicator(text: str) -> bool:
    """Carousel position labels such as 'slide 2 of 3' carry no content."""
    return any(p.match(text) for p in _INDICATORS)


def _has_ancestor(el, names: set[str]) -> bool:
    return any(p.name in names for p in el.parents)


def _is_breadcrumb(tag) -> bool:
    cls = " ".join(tag.get("class", []))
    return "breadcrumb" in cls.lower() or "פירורי" in (tag.get("aria-label") or "")


def _link_target(a) -> str | None:
    """Address to show for a link, or None for in-page anchors, scripts and empty hrefs."""
    href = (a.get("href") or "").strip()
    if not href or href.startswith(("#", "javascript:")):
        return None
    if href.startswith(("tel:", "mailto:")):
        return href
    return urljoin(config.BASE_URL + "/", href)


def _label(a) -> str:
    return _norm(a.get_text(" ", strip=True)) or _norm(a.get("aria-label") or a.get("title") or "")


def _anchor_of(node) -> str:
    """Real page anchor for a heading: its own id, or the id of a wrapper up to 3 levels above it."""
    for el in [node, *list(node.parents)[:3]]:
        if el.get("id") and el.name not in ("main", "body", "html"):
            return el["id"]
    return ""


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _annotate_inline_links(root) -> None:
    """Links inside running text keep their address where it matters.

    - inside a list item: every link gets ' (address)' (steps often say 'fill in <the form>')
    - inside a paragraph or table cell: only tel:/mailto: whose label hides the number/address
    """
    for a in root.find_all("a", href=True):
        in_li = _has_ancestor(a, {"li"})
        if not (in_li or _has_ancestor(a, {"p", "td", "th"})) or _has_ancestor(a, {"a"}):
            continue
        href = a["href"].strip()
        text = a.get_text()
        if href.startswith(("tel:", "mailto:")):
            value = href.split(":", 1)[1]
            shown = (_digits(value) in _digits(text)) if href.startswith("tel:") else (value in text)
            if not shown:
                a.append(NavigableString(f" ({href})"))
        elif in_li:
            target = _link_target(a)
            if target and target not in text:
                a.append(NavigableString(f" ({target})"))


def clean_html(html: bytes | str) -> dict:
    """Return {"title", "blocks": [{"type", "text"}], "chars", "empty"}.

    Block types: h1-h4 (headings), p (paragraph / loose text), li (list item), row (table row),
    link (standalone link, rendered as 'label (address)').
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("main") or soup.body or soup
    # column names are read before buttons are removed: sortable headers keep their label in a <button>
    table_headers = {}
    for table in root.find_all("table"):
        first = table.find("tr")
        if first is not None and all(c.name == "th" for c in first.find_all(["td", "th"])):
            table_headers[id(table)] = [_norm(c.get_text(" ", strip=True)) for c in first.find_all(["td", "th"])]
    for tag in root(_NOISE_TAGS):
        tag.decompose()
    for tag in [t for t in root.find_all(True) if _is_breadcrumb(t)]:
        tag.decompose()
    _drop_empty_inline(root)
    _annotate_inline_links(root)

    blocks: list[dict] = []
    loose: dict[int, int] = {}  # id(container) -> index of its block in `blocks`

    for node in root.descendants:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            # loose text: not inside a paragraph/list/heading/table/link (e.g. a promo <span> in a <div>)
            if _has_ancestor(node, _BLOCK_OWNERS):
                continue
            text = _norm(str(node))
            if not text or not _HAS_LETTER.search(text) or is_indicator(text):
                continue
            container = next((p for p in node.parents if p.name not in _INLINE), root)
            if id(container) in loose:
                blocks[loose[id(container)]]["text"] += " " + text
            else:
                loose[id(container)] = len(blocks)
                blocks.append({"type": "p", "text": text})
            continue

        name = node.name
        if name in _HEADINGS or name == "p":
            if _has_ancestor(node, {"li", "td", "th"}):
                continue  # already part of its container's text
            text, kind = _norm(node.get_text(" ", strip=True)), name
        elif name == "li":
            # own text only (nested lists become their own blocks); the tree is not modified
            text, kind = _norm(" ".join(s for s in node.strings if s.find_parent("li") is node)), "li"
        elif name == "tr":
            cell_tags = node.find_all(["td", "th"])
            cells = [_norm(c.get_text(" ", strip=True)) for c in cell_tags]
            table = node.find_parent("table")
            headers = table_headers.get(id(table), [])
            if table is not None and node is table.find("tr") and all(c.name == "th" for c in cell_tags):
                continue  # the header row itself: its names are attached to every data row instead
            if headers and len(headers) == len(cells):
                # make columns explicit; skip cells that already carry their label
                # (row-header <th> cells hold the row's name and stay as they are)
                cells = [c if (not h or h in c or t.name == "th") else f"{h} {c}"
                         for h, c, t in zip(headers, cells, cell_tags)]
            text, kind = " | ".join(c for c in cells if c), "row"
        elif name == "a":
            if _has_ancestor(node, _BLOCK_OWNERS) or node.find(["h1", "h2", "h3", "h4", "p", "li"]):
                continue  # inline link, or a card link whose inner blocks are handled on their own
            target, label = _link_target(node), _label(node)
            if not target or not label or is_indicator(label):
                continue
            text, kind = f"{label} ({target})", "link"
        else:
            continue
        if text and not is_indicator(text):
            block = {"type": kind, "text": text}
            if name in _HEADINGS:
                block["anchor"] = _anchor_of(node)
            blocks.append(block)

    deduped: list[dict] = []
    for b in blocks:
        b["text"] = _norm(b["text"])
        if not deduped or deduped[-1]["text"] != b["text"]:
            deduped.append(b)
    blocks = deduped

    h1 = next((b["text"] for b in blocks if b["type"] == "h1"), None)
    title = h1 or (_norm(soup.title.get_text()) if soup.title else "")
    chars = sum(len(b["text"]) for b in blocks)
    return {"title": title, "blocks": blocks, "chars": chars, "empty": chars < config.MIN_PAGE_CHARS}


def strip_repeated(docs: list[dict]) -> list[dict]:
    """Remove non-heading blocks that appear on more than REPEAT_THRESHOLD of pages (banners).

    Disabled by default (config.STRIP_REPEATED_BLOCKS): judge it on the full corpus, not a sample.
    """
    nonempty = [d for d in docs if not d["empty"]]
    if not config.STRIP_REPEATED_BLOCKS or len(nonempty) < config.REPEAT_MIN_PAGES:
        return docs
    counts = Counter(t for d in nonempty for t in {b["text"] for b in d["blocks"]})
    limit = config.REPEAT_THRESHOLD * len(nonempty)
    for d in nonempty:
        d["blocks"] = [b for b in d["blocks"] if b["type"] in _HEADINGS or counts[b["text"]] <= limit]
        d["chars"] = sum(len(b["text"]) for b in d["blocks"])
        d["empty"] = d["chars"] < config.MIN_PAGE_CHARS
    return docs
