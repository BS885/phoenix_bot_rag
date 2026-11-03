"""Split a cleaned page into chunks by h2/h3 headings; each chunk starts with 'page title > section title'."""
import hashlib
import re
from urllib.parse import urlparse

import config


def area_of(url: str) -> str:
    path = urlparse(url).path
    for prefix, area in config.AREA_BY_PREFIX.items():
        if path.startswith(prefix):
            return area
    return config.DEFAULT_AREA


def _words(text: str) -> int:
    return len(text.split())


def _sections(blocks: list[dict], page_title: str) -> list[dict]:
    """Group blocks into sections; a new section starts at every h2/h3 (h1 is the page title)."""
    sections = [{"title": page_title, "topic": "", "units": []}]
    h2 = ""
    for b in blocks:
        if b["type"] == "h1":
            continue
        if b["type"] == "h2":
            h2 = b["text"]
            sections.append({"title": h2, "topic": h2, "units": []})
        elif b["type"] == "h3":
            title = f"{h2} > {b['text']}" if h2 else b["text"]
            sections.append({"title": title, "topic": h2 or b["text"], "units": []})
        elif b["type"] == "h4":
            sections[-1]["units"].append(("p", [b["text"]]))
        elif b["type"] == "li":
            units = sections[-1]["units"]
            if units and units[-1][0] == "list":
                units[-1][1].append(b["text"])
            else:
                units.append(("list", [b["text"]]))
        else:
            sections[-1]["units"].append(("p", [b["text"]]))
    return [s for s in sections
            if s["units"] and not any(s["title"].startswith(h) for h in config.BOILERPLATE_HEADINGS)]


def _unit_text(unit) -> str:
    kind, items = unit
    return "\n".join(f"- {i}" for i in items) if kind == "list" else items[0]


_SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+")


def _split_paragraph(text: str, max_words: int, overlap: int) -> list[str]:
    """Cut an oversized paragraph between sentences; consecutive parts share `overlap` sentences."""
    sentences = _SENTENCE_END.split(text)
    parts, cur, size = [], [], 0
    for s in sentences:
        w = _words(s)
        if cur and size + w > max_words:
            parts.append(" ".join(cur))
            cur = cur[-overlap:] if overlap else []
            size = sum(_words(x) for x in cur)
        cur.append(s)
        size += w
    if cur:
        parts.append(" ".join(cur))
    return parts


def _split(units: list, max_words: int) -> list[list]:
    """Split by paragraphs; a list is never cut. A paragraph over max_words is cut between sentences."""
    groups, cur, size = [], [], 0
    for u in units:
        w = _words(_unit_text(u))
        if u[0] == "p" and w > max_words:
            if cur:
                groups.append(cur)
            groups += [[("p", [part])] for part in
                       _split_paragraph(u[1][0], max_words, config.CHUNK_OVERLAP_SENTENCES)]
            cur, size = [], 0
            continue
        if cur and size + w > max_words:
            groups.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += w
    if cur:
        groups.append(cur)
    return groups


def chunk_page(doc: dict, url: str, meta: dict | None = None) -> list[dict]:
    meta = meta or {}
    page_title = doc["title"] or url
    pieces = []  # [section_title, body_text, topic]; only pieces of the same topic (h2) may be merged
    for sec in _sections(doc["blocks"], page_title):
        for group in _split(sec["units"], config.MAX_CHUNK_WORDS):
            pieces.append([sec["title"], "\n".join(_unit_text(u) for u in group), sec["topic"]])

    # merge short pieces into the next one (or the previous one if it is the last)
    merged: list[list] = []
    i = 0
    while i < len(pieces):
        title, body, topic = pieces[i]
        # merge forward only within the same topic and while the result stays within MAX_CHUNK_WORDS
        while (_words(body) < config.MIN_CHUNK_WORDS and i + 1 < len(pieces)
               and (pieces[i + 1][2] == topic or topic == "")  # page intro may join the first section
               and _words(body) + _words(pieces[i + 1][1]) <= config.MAX_CHUNK_WORDS):
            i += 1
            nxt_title, nxt_body, _ = pieces[i]
            title = f"{title} / {nxt_title}" if nxt_title != title else title
            body = f"{body}\n{nxt_body}"
        merged.append([title, body, topic])
        i += 1
    if (len(merged) > 1 and _words(merged[-1][1]) < config.MIN_CHUNK_WORDS
            and merged[-1][2] == merged[-2][2]
            and _words(merged[-2][1]) + _words(merged[-1][1]) <= config.MAX_CHUNK_WORDS):
        t, b, _ = merged.pop()
        merged[-1][0] = f"{merged[-1][0]} / {t}" if t != merged[-1][0] else merged[-1][0]
        merged[-1][1] += "\n" + b

    page_hash = hashlib.sha1("\n".join(b["text"] for b in doc["blocks"]).encode("utf-8")).hexdigest()
    chunks = []
    for n, (title, body, _topic) in enumerate(merged):
        head = page_title if title == page_title else f"{page_title} > {title}"
        chunks.append({
            "id": hashlib.sha1(f"{url}#{n}".encode("utf-8")).hexdigest()[:20],
            "text": f"{head}\n{body}",
            "url": url,
            "page_title": page_title,
            "section_title": title,
            "area": area_of(url),
            "collected_at": meta.get("collected_at"),
            "lastmod": meta.get("lastmod"),
            "content_hash": page_hash,
        })
    return chunks
