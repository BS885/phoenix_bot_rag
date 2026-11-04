"""Chunking statistics over the saved pages (data/raw_html). Writes logs/chunk_stats.md.

Usage:  python scripts/chunk_stats.py
"""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from ingest import chunk, clean  # noqa: E402


def words(chunk_: dict) -> int:
    return len(chunk_["text"].split())


def main() -> None:
    index = json.loads(config.FETCH_INDEX.read_text(encoding="utf-8"))
    rows, all_chunks = [], []
    for url, entry in index.items():
        doc = clean.clean_html((config.RAW_HTML_DIR / entry["file"]).read_bytes())
        chunks = [] if doc["empty"] else chunk.chunk_page(doc, url, entry)
        all_chunks += chunks
        w = [words(c) for c in chunks]
        rows.append((url.replace(config.BASE_URL, ""), doc["empty"], len(chunks),
                     sum(c["short"] for c in chunks), min(w, default=0),
                     int(statistics.median(w)) if w else 0, max(w, default=0)))

    w_all = [words(c) for c in all_chunks]
    over = [c for c in all_chunks if len(c["text"].split()) > config.MAX_CHUNK_WORDS + 15]  # +15: title line
    ids = [c["id"] for c in all_chunks]
    out = ["# Chunk statistics", "",
           f"- pages: {len(rows)} ({sum(r[1] for r in rows)} empty, not indexed)",
           f"- chunks: {len(all_chunks)}, short (< {config.MIN_CHUNK_WORDS} words in body): "
           f"{sum(c['short'] for c in all_chunks)}",
           f"- words per chunk (title line included): min {min(w_all)}, median {int(statistics.median(w_all))}, "
           f"mean {int(statistics.mean(w_all))}, max {max(w_all)}",
           f"- chunks over {config.MAX_CHUNK_WORDS} words (+ title line): {len(over)}",
           f"- duplicate ids: {len(ids) - len(set(ids))}; "
           f"duplicate texts: {len(all_chunks) - len({c['text'] for c in all_chunks})}",
           f"- by area: " + ", ".join(f"{a}={sum(c['area'] == a for c in all_chunks)}"
                                      for a in sorted({c['area'] for c in all_chunks})),
           "", "| page | empty | chunks | short | min | median | max |", "|---|---|---|---|---|---|---|"]
    out += [f"| {r[0]} | {'yes' if r[1] else ''} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |" for r in rows]
    out += ["", "## Chunk titles (section_title) per page", ""]
    for url, entry in index.items():
        titles = [c["section_title"] for c in all_chunks if c["url"] == url]
        out += [f"### {url.replace(config.BASE_URL, '')}"] + [f"- {t}" for t in titles] + [""]

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "chunk_stats.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out[:12]))


if __name__ == "__main__":
    main()
