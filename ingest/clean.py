"""Isolate the content area of a saved page and return it as a flat list of text blocks."""
import re
from collections import Counter

from bs4 import BeautifulSoup

import config

# header/footer are NOT removed: the h1/h2 headings live in <header> inside <main>.
# We only look inside <main>, so the site header/footer are already excluded.
_NOISE_TAGS = ["script", "style", "nav", "noscript", "svg", "button", "aside"]
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏﻿"))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_ZERO_WIDTH).replace("\xa0", " ")).strip()


def _has_ancestor(el, names: set[str]) -> bool:
    return any(p.name in names for p in el.parents)


def clean_html(html: bytes | str) -> dict:
    """Return {"title", "blocks": [{"type", "text"}], "chars", "empty"}.

    Block types: h1-h4 (headings), p (paragraph), li (list item), row (table row).
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("main") or soup.body or soup
    for tag in root(_NOISE_TAGS):
        tag.decompose()

    blocks: list[dict] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
        name = el.name
        if name in _HEADINGS or name == "p":
            if _has_ancestor(el, {"li", "td", "th"}):
                continue  # already part of its container's text
            text = _norm(el.get_text(" ", strip=True))
            kind = name
        elif name == "li":
            # own text only (nested lists become their own blocks); the tree is not modified
            text = _norm(" ".join(s for s in el.strings if s.find_parent("li") is el))
            kind = "li"
        else:  # tr
            cells = [_norm(c.get_text(" ", strip=True)) for c in el.find_all(["td", "th"])]
            text = " | ".join(c for c in cells if c)
            kind = "row"
        if text and not (blocks and blocks[-1]["text"] == text):
            blocks.append({"type": kind, "text": text})

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
