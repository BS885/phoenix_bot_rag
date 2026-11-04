"""Coverage check: how much of the visible text of <main> survives clean_html().

Reads saved pages from data/raw_html (via index.json), writes the report to logs/coverage.md,
and prints only the pages whose coverage is below the threshold, with the text that is missing.

Usage:  python scripts/coverage_check.py [--threshold 0.9] [--max-missing 25]
"""
import argparse
import json
from collections import Counter
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402

import config  # noqa: E402
from ingest import clean, urls  # noqa: E402

_HIDDEN = ["script", "style", "noscript", "svg"]  # not visible text: excluded from the baseline
_HAS_CONTENT = re.compile(r"[\w֐-׿]")
_CONTAINERS = ("nav", "button", "aside", "form", "table", "li", "p", "h1", "h2", "h3", "h4")


def _intentional(node, text: str) -> bool:
    """Text that clean_html removes on purpose: navigation, buttons, breadcrumbs, indicators, bare digits."""
    if any(p.name in ("nav", "button", "aside") or clean._is_breadcrumb(p) for p in node.parents):
        return True
    return clean.is_indicator(text) or text.isdigit()


def main_segments(html: bytes) -> list[tuple[str, str, bool]]:
    """Visible text nodes of <main> as (normalized text, nearest container tag, removed_on_purpose)."""
    soup = BeautifulSoup(html.decode("utf-8", errors="replace"), "lxml")
    root = soup.find("main")
    if root is None:
        return []
    for tag in root(_HIDDEN):
        tag.decompose()
    segs = []
    for s in root.strings:
        text = clean._norm(str(s))
        if not text or not _HAS_CONTENT.search(text):
            continue
        parent = next((p.name for p in s.parents if p.name in _CONTAINERS), "other")
        segs.append((text, parent, _intentional(s, text)))
    return segs


def check_page(html: bytes) -> dict:
    segs = main_segments(html)
    extracted = " ".join(b["text"] for b in clean.clean_html(html)["blocks"])
    absent = [(t, c, on_purpose) for t, c, on_purpose in segs if t not in extracted]
    # coverage is computed on content text only: text removed on purpose is not counted as a loss
    content = [(t, c) for t, c, on_purpose in segs if not on_purpose]
    total = sum(len(t) for t, _ in content)
    missing = [(t, c) for t, c, on_purpose in absent if not on_purpose]
    lost = sum(len(t) for t, _ in missing)
    return {
        "main_chars": sum(len(t) for t, _, _ in segs),
        "content_chars": total,
        "extracted_chars": len(extracted),
        "removed_on_purpose_chars": sum(len(t) for t, _, on_purpose in absent if on_purpose),
        "removed_on_purpose": [(t, c) for t, c, on_purpose in absent if on_purpose],
        "coverage": (total - lost) / total if total else 1.0,
        "missing": missing,
    }


def render_missing(missing: list[tuple[str, str]], limit: int) -> list[str]:
    lines = [f"- `{c}` | {t[:140]}" for t, c in missing[:limit]]
    if len(missing) > limit:
        lines.append(f"- ... and {len(missing) - limit} more")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.9, help="report pages below this coverage")
    ap.add_argument("--max-missing", type=int, default=25)
    args = ap.parse_args()

    index = json.loads(config.FETCH_INDEX.read_text(encoding="utf-8"))
    # only pages that were downloaded: failed / 404 / gone entries have no saved HTML
    index = {u: e for u, e in index.items()
             if e.get("file") and e.get("status", "ok") == "ok" and not e.get("gone")
                and urls.is_allowed_url(u)}  # out-of-scope pages stay on disk but are ignored
    results = {}
    for url, entry in index.items():
        results[url] = check_page((config.RAW_HTML_DIR / entry["file"]).read_bytes())

    rows = ["| page | main chars | removed on purpose | content chars | extracted chars | coverage |",
            "|---|---|---|---|---|---|"]
    low = {u: r for u, r in results.items() if r["coverage"] < args.threshold}
    for u, r in results.items():
        flag = " **LOW**" if u in low else ""
        rows.append(f"| {u.replace(config.BASE_URL, '')} | {r['main_chars']} | {r['removed_on_purpose_chars']} | "
                    f"{r['content_chars']} | {r['extracted_chars']} | {r['coverage']:.0%}{flag} |")

    report = [f"# Coverage report (threshold {args.threshold:.0%})", "",
              f"{len(results)} pages checked, {len(low)} below threshold.", "",
              "Coverage = share of content text characters in `<main>` that appear in the output of `clean_html`.",
              "Text removed on purpose (nav, breadcrumbs, buttons, carousel indicators, bare digits) is counted",
              "separately and is not a loss. Missing text is listed with its nearest container tag.", ""]
    report += rows
    for u, r in low.items():
        report += ["", f"## {u.replace(config.BASE_URL, '')} ({r['coverage']:.0%})", ""]
        report += render_missing(r["missing"], args.max_missing)

    removed = Counter(item for r in results.values() for item in r["removed_on_purpose"])
    report += ["", "## Removed on purpose (text, nearest tag, number of occurrences across pages)", ""]
    report += [f"- `{c}` | {t[:100]} | x{n}" for (t, c), n in removed.most_common()]

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = config.LOG_DIR / "coverage.md"
    out.write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"{len(results)} pages, {len(low)} below {args.threshold:.0%}. Report: {out}")
    for u, r in low.items():
        print(f"\n{u.replace(config.BASE_URL, '')}  coverage {r['coverage']:.0%}")
        for line in render_missing(r["missing"], args.max_missing):
            print("  " + line)


if __name__ == "__main__":
    main()
