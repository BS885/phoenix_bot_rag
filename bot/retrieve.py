"""Retrieval: question -> a few groups of chunks for the prompt, plus a product-conflict signal (clarify).

Steps (all tunable in config.py, see "retrieval"):
  1. pull RETRIEVE_FETCH chunks from the active index (cosine distance),
  2. drop very short chunks (fewer than RETRIEVE_MIN_WORDS words and no digit / link / address),
  3. if the question names a product, drop chunks of other products (only when something matching remains),
  4. group chunks of the same topic + section title whose bodies are similar; keep the closest as the
     representative and every page in a sources list,
  5. keep RETRIEVE_MAX_GROUPS groups; flag a product conflict when the strong groups come from different
     products and the question names none (the caller asks ONE clarifying question, see CLAUDE.md).

The distance is NOT used to decide "out of scope" (decisions 32, 40): that is the model's call.
"""
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import config
from index import embed

_HAS_DIGIT = re.compile(r"\d")
_HAS_LINK = re.compile(r"https?://|tel:|mailto:|www\.|@")


# ---------- topic and product of a page / a question ----------

def topic_of(url: str) -> str:
    """Product slug of a page: first URL segment, or the second one for claims / customer-service pages."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return "general"
    first = parts[0]
    if first in ("claims-customer-service", "customer-service"):
        if len(parts) < 2:
            return "general"
        second = parts[1]
        topic = config.TOPIC_ALIASES.get(second, second)
        return topic if topic in config.PRODUCT_NAMES or topic in config.EXTRA_TOPIC_NAMES else "general"
    return first if first in config.PRODUCT_NAMES else "general"


def topic_name(topic: str) -> str:
    return config.PRODUCT_NAMES.get(topic) or config.EXTRA_TOPIC_NAMES.get(topic) or topic


def detect_products(question: str) -> list[str]:
    """Products the question names explicitly (config.PRODUCT_TERMS), in the order of the table."""
    q = question.replace("״", '"').replace("”", '"').replace("“", '"').replace("’", "'").replace("׳", "'")
    return [slug for slug, terms in config.PRODUCT_TERMS.items() if any(t in q for t in terms)]


# ---------- hits ----------

def _body(text: str) -> str:
    return text.split("\n", 1)[1] if "\n" in text else text


def _last_section(title: str) -> str:
    return re.sub(r"\s+", " ", title.split(" > ")[-1]).strip()


def is_very_short(hit: dict, min_words: int | None = None) -> bool:
    """A body with fewer words than the threshold, unless it carries a number or a link / address."""
    min_words = config.RETRIEVE_MIN_WORDS if min_words is None else min_words
    body = _body(hit["text"])
    if len(body.split()) >= min_words:
        return False
    return not (_HAS_DIGIT.search(body) or _HAS_LINK.search(body))


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 1.0


@dataclass
class Group:
    text: str                       # the representative chunk (closest to the question)
    url: str
    anchor: str
    page_title: str
    section_title: str
    topic: str
    topic_name: str
    distance: float
    short: bool
    sources: list[dict] = field(default_factory=list)   # [{"title", "url", "anchor"}], closest first
    size: int = 1                   # chunks merged into this group
    chunk_id: str = ""              # the representative chunk
    section_id: str = ""
    context: str = ""               # full section context (all parts by section_id), capped; filled by expand_context
    context_words: int = 0


@dataclass
class Retrieval:
    groups: list[Group]
    detected_products: list[str]
    product_conflict: bool = False
    conflict_topics: list[str] = field(default_factory=list)
    product_miss: bool = False      # the question names a product and nothing in the results matches it
    dropped_short: int = 0
    dropped_other_product: int = 0
    fetched: int = 0

    def debug(self) -> dict:
        return {"fetched": self.fetched, "dropped_short": self.dropped_short,
                "dropped_other_product": self.dropped_other_product, "detected_products": self.detected_products,
                "product_conflict": self.product_conflict, "conflict_topics": self.conflict_topics,
                "product_miss": self.product_miss,
                "groups": [{"url": g.url, "section": g.section_title, "topic": g.topic,
                            "distance": round(g.distance, 3), "size": g.size} for g in self.groups]}


def _source(hit: dict) -> dict:
    return {"title": hit["page_title"], "url": hit["url"], "anchor": hit.get("anchor", "")}


def process_hits(question: str, hits: list[dict], *, min_words: int | None = None,
                 max_groups: int | None = None) -> Retrieval:
    """Everything after the vector search; pure, so it is testable without an index."""
    max_groups = max_groups or config.RETRIEVE_MAX_GROUPS
    hits = sorted(hits, key=lambda h: h["distance"])
    for h in hits:
        h["topic"] = topic_of(h["url"])
    detected = detect_products(question)
    out = Retrieval(groups=[], detected_products=detected, fetched=len(hits))

    kept = [h for h in hits if not is_very_short(h, min_words)]
    out.dropped_short = len(hits) - len(kept)
    if not kept:               # never end up with nothing only because everything was short
        kept, out.dropped_short = hits[:1], len(hits) - min(len(hits), 1)

    if detected:
        match = [h for h in kept if h["topic"] in detected or h["topic"] == "general"]
        if any(h["topic"] in detected for h in match):
            out.dropped_other_product = len(kept) - len(match)
            kept = match
        else:
            out.product_miss = True

    groups: list[Group] = []
    for h in kept:
        key = (h["topic"], _last_section(h["section_title"]))
        for g in groups:
            if (g.topic, _last_section(g.section_title)) == key and \
                    _similarity(_body(g.text), _body(h["text"])) >= config.RETRIEVE_GROUP_MIN_SIMILARITY:
                g.size += 1
                for s in [_source(h)] + [{"title": "", "url": u, "anchor": ""} for u in h.get("dup_urls", "").split("|") if u]:
                    if s["url"] not in {x["url"] for x in g.sources}:
                        g.sources.append(s)
                break
        else:
            sources = [_source(h)] + [{"title": "", "url": u, "anchor": ""} for u in h.get("dup_urls", "").split("|") if u]
            groups.append(Group(text=h["text"], url=h["url"], anchor=h.get("anchor", ""), page_title=h["page_title"],
                                section_title=h["section_title"], topic=h["topic"], topic_name=topic_name(h["topic"]),
                                distance=h["distance"], short=bool(h.get("short")), sources=sources,
                                chunk_id=h.get("id", ""), section_id=h.get("section_id", "")))
    out.groups = groups[:max_groups]

    if not detected and out.groups:
        best = out.groups[0].distance
        strong = [g for g in out.groups if g.topic != "general"
                  and g.distance <= best + config.RETRIEVE_CLARIFY_DELTA
                  and g.distance <= config.RETRIEVE_CLARIFY_MAX_DISTANCE]
        topics = list(dict.fromkeys(g.topic for g in strong))
        if len(topics) >= 2:
            out.product_conflict, out.conflict_topics = True, topics
    return out


# ---------- full context by section_id ----------

def _nwords(text: str) -> int:
    return len(text.split())


def _strip_head(text: str) -> str:
    """Parts after the first repeat the 'page > section' header line; drop it."""
    return text.split("\n", 1)[1] if "\n" in text else text


def section_context(found: dict, siblings: list[dict], max_words: int) -> tuple[str, int]:
    """All parts of one section in page order, capped at max_words. The found chunk is always kept; when the section
    is longer, parts nearest to the found chunk are added first. Returns (text, words)."""
    parts = {s["id"]: s for s in siblings}
    parts.setdefault(found["id"], found)
    order = sorted(parts.values(), key=lambda s: (s.get("chunk_no", 0), s["id"]))
    pos = next(i for i, s in enumerate(order) if s["id"] == found["id"])
    chosen, total = {pos}, _nwords(order[pos]["text"])
    for i in sorted((i for i in range(len(order)) if i != pos), key=lambda i: (abs(i - pos), i)):
        w = _nwords(_strip_head(order[i]["text"]))
        if total + w > max_words:
            break               # keep the chosen parts contiguous around the found chunk
        chosen.add(i)
        total += w
    first = True
    texts = []
    for i in sorted(chosen):
        texts.append(order[i]["text"] if first else _strip_head(order[i]["text"]))
        first = False
    return "\n".join(texts), total


def expand_context(groups: list[Group], fetch_siblings, *, max_section: int | None = None,
                   max_total: int | None = None) -> None:
    """Fill group.context for each group, best first, within the total cap (the first group is never starved)."""
    max_section = max_section or config.FULL_CONTEXT_MAX_WORDS_SECTION
    left = max_total or config.FULL_CONTEXT_MAX_WORDS_TOTAL
    for g in groups:
        found = {"id": g.chunk_id, "text": g.text}
        sibs = fetch_siblings(g.section_id) if g.section_id else []
        for s in sibs:
            if s["id"] == g.chunk_id:
                found = s
        cap = min(max_section, left)
        g.context, g.context_words = section_context(found, sibs, max(cap, _nwords(g.text)))
        left = max(left - g.context_words, 0)


# ---------- the index ----------

def query_index(col, vector: list[float], n: int | None = None) -> list[dict]:
    n = n or config.RETRIEVE_FETCH
    res = col.query(query_embeddings=[vector], n_results=min(n, max(col.count(), 1)),
                    include=["metadatas", "documents", "distances"])
    hits = []
    for md, doc, dist in zip(res["metadatas"][0], res["documents"][0], res["distances"][0]):
        hits.append({**md, "text": doc, "distance": float(dist)})
    return hits


class Retriever:
    """Active index + embedder + cache. Build once, call retrieve(question) per question."""

    def __init__(self, col=None, embedder=None, cache=None):
        from index import build_index as bi
        self.col = col if col is not None else bi.get_active_collection()   # stops on a model / dim mismatch
        self.embedder = embedder if embedder is not None else embed.make_embedder()
        self.cache = cache if cache is not None else embed.EmbedCache(config.EMBED_CACHE_PATH)

    def siblings(self, section_id: str) -> list[dict]:
        res = self.col.get(where={"section_id": section_id}, include=["metadatas", "documents"])
        return [{"id": i, "text": d, "chunk_no": int(md.get("chunk_no", 0))}
                for i, d, md in zip(res["ids"], res["documents"], res["metadatas"])]

    def retrieve(self, question: str) -> Retrieval:
        vector = embed.embed_query(question, self.embedder, self.cache)
        out = process_hits(question, query_index(self.col, vector))
        expand_context(out.groups, self.siblings)
        return out
