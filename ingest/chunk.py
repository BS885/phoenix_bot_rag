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


def product_of(url: str) -> str:
    """First path segment of the URL ("" for the home page)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else ""


def _words(text: str) -> int:
    return len(text.split())


def _row_name(row_text: str) -> str:
    name = row_text.split(" | ")[0]
    for junk in config.ROW_TITLE_STRIP:
        name = name.replace(junk, "")
    return " ".join(name.split())


def _sections(blocks: list[dict], page_title: str, drop_link_only: bool = True) -> list[dict]:
    """Group blocks into sections; a new section starts at every h2/h3 (h1 is the page title).

    Inside a section, a block whose text already appeared there is dropped (pages often render the
    same steps twice, e.g. desktop and mobile), and so is a bare CTA label such as 'הגשת תביעה'.
    """
    def new(title, topic, anchor="", units=None, text=False):
        # has_text: the section holds something other than link blocks; seen: block texts so far
        return {"title": title, "topic": topic, "anchor": anchor, "units": units or [],
                "has_text": text, "seen": set()}

    sections = [new(page_title, "")]
    h2, h2_anchor = "", ""
    after_row = False
    for b in blocks:
        if b["type"] == "h1":
            continue
        if after_row and b["type"] not in ("row", "h2", "h3"):
            # text following a table must not be attached to the last row's chunk
            sections.append(new(h2 or page_title, h2, h2_anchor))
        after_row = b["type"] == "row"
        if b["type"] in ("p", "li", "link", "h4"):
            if b["text"] in config.CTA_LABELS or b["text"] in sections[-1]["seen"]:
                continue
            sections[-1]["seen"].add(b["text"])
        if b["type"] == "h2":
            h2, h2_anchor = b["text"], b.get("anchor", "")
            sections.append(new(h2, h2, h2_anchor))
        elif b["type"] == "h3":
            # each h3 is its own topic: sibling h3 (e.g. FAQ questions) are never merged
            title = f"{h2} > {b['text']}" if h2 else b["text"]
            sections.append(new(title, f"h3:{len(sections)}", b.get("anchor") or h2_anchor))
        elif b["type"] == "row":
            # one chunk per table row; the name cell is the title, cells carry their own column labels
            name = _row_name(b["text"])
            title = f"{h2} > {name}" if h2 else name
            sections.append(new(title, f"row:{len(sections)}", h2_anchor, [("p", [b["text"]])], text=True))
        elif b["type"] == "h4":
            sections[-1]["units"].append(("p", [b["text"]]))
            sections[-1]["has_text"] = True
        elif b["type"] == "li":
            units = sections[-1]["units"]
            sections[-1]["has_text"] = True
            if units and units[-1][0] == "list":
                units[-1][1].append(b["text"])
            else:
                units.append(("list", [b["text"]]))
        else:
            sections[-1]["units"].append(("p", [b["text"]]))
            if b["type"] != "link":
                sections[-1]["has_text"] = True
    keep = []
    for s in sections:
        if not s["units"] or any(s["title"].startswith(h) for h in config.BOILERPLATE_HEADINGS):
            continue
        if drop_link_only and not s["has_text"]:
            continue  # nothing but links, no sentence
        keep.append(s)
    return keep


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
    # piece: [section_title, body_text, topic, section_id, anchor]; only pieces of one topic (h2) may merge
    pieces = []
    seen_titles: dict[str, int] = {}
    area = area_of(url)
    product = product_of(url)
    drop_link_only = config.DROP_LINK_ONLY_SECTIONS and area not in config.LINK_ONLY_EXEMPT_AREAS
    for sec in _sections(doc["blocks"], page_title, drop_link_only):
        k = seen_titles[sec["title"]] = seen_titles.get(sec["title"], -1) + 1
        section_id = hashlib.sha1(f"{url}|{sec['title']}|{k}".encode("utf-8")).hexdigest()[:12]
        for group in _split(sec["units"], config.MAX_CHUNK_WORDS):
            pieces.append([sec["title"], "\n".join(_unit_text(u) for u in group), sec["topic"],
                           section_id, sec["anchor"]])

    # merge short pieces into the next one (or the previous one if it is the last);
    # a merged chunk keeps the section_id and anchor of its first section
    merged: list[list] = []
    i = 0
    while i < len(pieces):
        title, body, topic, section_id, anchor = pieces[i]
        # merge forward only within the same topic and while the result stays within MAX_CHUNK_WORDS
        while (_words(body) < config.MIN_CHUNK_WORDS and i + 1 < len(pieces)
               and (pieces[i + 1][2] == topic or topic == "")  # page intro may join the first section
               and _words(body) + _words(pieces[i + 1][1]) <= config.MAX_CHUNK_WORDS):
            i += 1
            nxt_title, nxt_body = pieces[i][0], pieces[i][1]
            if title == page_title:
                title = nxt_title  # the page intro joins the first section: do not repeat the page title
            elif nxt_title != title:
                title = f"{title} / {nxt_title}"
            body = f"{body}\n{nxt_body}"
        merged.append([title, body, topic, section_id, anchor])
        i += 1
    if (len(merged) > 1 and _words(merged[-1][1]) < config.MIN_CHUNK_WORDS
            and merged[-1][2] == merged[-2][2]
            and _words(merged[-2][1]) + _words(merged[-1][1]) <= config.MAX_CHUNK_WORDS):
        t, b = merged.pop()[:2]
        merged[-1][0] = f"{merged[-1][0]} / {t}" if t != merged[-1][0] else merged[-1][0]
        merged[-1][1] += "\n" + b

    page_hash = hashlib.sha1("\n".join(b["text"] for b in doc["blocks"]).encode("utf-8")).hexdigest()
    chunks = []
    for n, (title, body, _topic, section_id, anchor) in enumerate(merged):
        head = page_title if title == page_title else f"{page_title} > {title}"
        # metadata values must never be None (Chroma rejects them): missing values become ""
        chunks.append({
            "id": hashlib.sha1(f"{url}#{n}".encode("utf-8")).hexdigest()[:20],
            "text": f"{head}\n{body}",
            "url": url,
            "page_title": page_title,
            "section_title": title,
            "section_id": section_id,
            "anchor": anchor or "",  # real #id on the page, "" when the section has none
            "area": area,
            "product": product,
            "product_name": config.PRODUCT_NAMES.get(product, ""),
            "short": _words(body) < config.MIN_CHUNK_WORDS,  # could not be merged with a neighbour of its topic
            "collected_at": meta.get("collected_at") or "",
            "lastmod": meta.get("lastmod") or "",
            "content_hash": page_hash,
        })
    return chunks
