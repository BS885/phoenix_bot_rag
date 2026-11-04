"""Pre-build analyses over the full saved corpus (no API, no network). Writes logs/prep_*.md.

Usage:  python scripts/prep_analysis.py
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from ingest import chunk, clean, urls  # noqa: E402

PHONE = re.compile(r"(?<!\d)(\*\d{3,4}|0\d{1,2}[-\s]?\d{7}|1[-\s]?7\d{2}[-\s]?\d{3}[-\s]?\d{3}|\b\d{3}-\d{7}\b)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
LINK = re.compile(r"\(https?://[^)]+\)|\(/[^)]+\)")
FORM = re.compile(r"טופס|טפסים|הגש|הגשה|שליחה|מייל|פקס|דוא")


def load():
    index = json.loads(config.FETCH_INDEX.read_text(encoding="utf-8"))
    docs = []
    for u, e in index.items():
        if not e.get("file") or e.get("status", "ok") != "ok" or e.get("gone") or not urls.is_allowed_url(u):
            continue
        raw = (config.RAW_HTML_DIR / e["file"]).read_bytes()
        docs.append((u, e, raw, clean.clean_html(raw)))
    return docs


def a_strip_repeated(docs):
    ne = [(u, d) for u, _, _, d in docs if not d["empty"]]
    counts = Counter(t for _, d in ne for t in {b["text"] for b in d["blocks"]})
    limit = config.REPEAT_THRESHOLD * len(ne)
    removed = {t: n for t, n in counts.items() if n > limit}
    typ = {b["text"]: b["type"] for _, d in ne for b in d["blocks"]}
    removed = {t: n for t, n in removed.items() if typ[t] not in clean._HEADINGS}
    chars = sum(len(t) * n for t, n in removed.items())
    total = sum(d["chars"] for _, d in ne)
    out = ["# strip_repeated on the full corpus (simulation, nothing changed)", "",
           f"pages (non-empty): {len(ne)} | threshold: >{config.REPEAT_THRESHOLD:.0%} = more than {limit:.0f} pages",
           f"distinct blocks it would remove: {len(removed)} | occurrences: {sum(removed.values())} | "
           f"chars: {chars} of {total} ({chars/total:.1%})", ""]
    risky = {t: n for t, n in removed.items() if PHONE.search(t) or EMAIL.search(t) or LINK.search(t) or FORM.search(t)}
    out += [f"## would DELETE phones/emails/links/forms: {len(risky)} blocks", ""]
    for t, n in sorted(risky.items(), key=lambda x: -x[1]):
        flags = [k for k, r in (("phone", PHONE), ("email", EMAIL), ("link", LINK), ("form", FORM)) if r.search(t)]
        out.append(f"- x{n} [{','.join(flags)}] {t[:160]!r}")
    # unique-to-corpus info: pages where the removed phone/email would disappear entirely
    carriers = defaultdict(set)
    for u, d in ne:
        for b in d["blocks"]:
            for m in PHONE.findall(b["text"]) + EMAIL.findall(b["text"]):
                carriers[m].add(b["text"] in removed)
    lost = [m for m, flags in carriers.items() if flags == {True}]
    out += ["", f"## phone/email values that exist ONLY inside removed blocks: {len(lost)}", ""] + [f"- {m}" for m in sorted(lost)]
    out += ["", "## the other removed blocks (no contact/form signal)", ""]
    for t, n in sorted(((t, n) for t, n in removed.items() if t not in risky), key=lambda x: -x[1])[:60]:
        out.append(f"- x{n} {t[:120]!r}")
    (ROOT / "logs" / "prep_strip_repeated.md").write_text("\n".join(out), encoding="utf-8")
    print(out[2], out[3], f"| risky blocks {len(risky)} | values only in removed blocks {len(lost)}", sep="\n")


NOISE_SELECTORS = [("a.mobile-tags-open-modal-link", "tag toggle"), ("span.article-tag-item", "article tag chip"),
                   ("a.btn-play", "video opener"), ("span.cat-label", "category label"),
                   ("a.btn.btn-primary", "CTA button"), ("a.link-arrow", "show-more link")]


def b_noise(docs):
    """Strings that exist in the HTML main area, absent from cleaned text, from known UI containers."""
    per = defaultdict(Counter)
    pages = defaultdict(set)
    for u, _, raw, d in docs:
        if d["empty"]:
            continue
        cleaned = "\n".join(b["text"] for b in d["blocks"])
        soup = BeautifulSoup(raw, "lxml")
        main = soup.find("main") or soup
        for sel, label in NOISE_SELECTORS:
            for el in main.select(sel):
                t = clean._norm(el.get_text(" ", strip=True))
                if t and t not in cleaned:
                    per[label][t] += 1
                    pages[(label, t)].add(u)
    out = ["# UI noise strings that cleaning does not extract (candidates for explicit removal)", ""]
    for label, c in per.items():
        out += [f"## {label}: {sum(c.values())} occurrences, {len(c)} distinct", ""]
        out += [f"- x{n} ({len(pages[(label, t)])} pages) {t[:90]!r}" for t, n in c.most_common(25)]
        out.append("")
    (ROOT / "logs" / "prep_ui_noise.md").write_text("\n".join(out), encoding="utf-8")
    print({k: (sum(v.values()), len(v)) for k, v in per.items()})


def c_iframes(docs):
    dom, pages, ex = Counter(), defaultdict(set), {}
    for u, _, raw, d in docs:
        soup = BeautifulSoup(raw, "lxml")
        for f in soup.find_all("iframe"):
            src = f.get("src") or f.get("data-src") or ""
            host = urlparse(src).netloc or ("(relative)" if src.startswith("/") else "(empty/other)")
            path = urlparse(src).path if host in ("(relative)",) or "fnx" in host else ""
            key = host + (" " + path.split("/")[1] if path.count("/") > 0 and path.split("/")[1] else "")
            dom[key] += 1
            pages[key].add(u)
            ex.setdefault(key, src[:110])
    out = ["# iframe src by domain (all saved in-scope pages)", "",
           f"pages with iframe: {len(set().union(*pages.values())) if pages else 0}", ""]
    out += [f"- {k}: {n} iframes, {len(pages[k])} pages, e.g. {ex[k]}" for k, n in dom.most_common()]
    (ROOT / "logs" / "prep_iframes.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[2:]))


def d_product(docs):
    segs = defaultdict(lambda: [0, 0, set()])
    for u, e, _, d in docs:
        if d["empty"]:
            continue
        seg = chunk.area_of(u)
        parts = [p for p in urlparse(u).path.split("/") if p]
        seg = parts[0] if parts else "(home)"
        cs = chunk.chunk_page(d, u, e)
        segs[seg][0] += 1
        segs[seg][1] += len(cs)
        segs[seg][2] |= {c["area"] for c in cs}
    has_field = any("product" in c for c in chunk.chunk_page(next(d for *_, d in docs if not d["empty"]), docs[0][0], {}))
    out = ["# product = first URL segment (proposal)", "",
           f"chunk metadata today has a `product` field: {has_field}", "",
           "| first segment | pages | chunks (before dedupe) | area values |", "|---|---|---|---|"]
    for s, (p, c, a) in sorted(segs.items(), key=lambda x: -x[1][1]):
        out.append(f"| {s} | {p} | {c} | {', '.join(sorted(a))} |")
    (ROOT / "logs" / "prep_product.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[2:]))


if __name__ == "__main__":
    import bootstrap  # noqa: F401
    docs = load()
    print("pages loaded:", len(docs))
    for fn in (a_strip_repeated, b_noise, c_iframes, d_product):
        print("\n==", fn.__name__)
        fn(docs)
