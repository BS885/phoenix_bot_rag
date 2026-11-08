"""Run bot/retrieve.py on the questions of eval/ (smoke + sanity cases + negatives) on the ACTIVE index.

No language-model call. Embeds only questions that are not in the cache yet (counted against the daily quota).
Report: logs/retrieve_check.md

Usage:  python scripts/retrieve_check.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from bot import retrieve  # noqa: E402


def questions() -> list[tuple[str, str]]:
    smoke = json.loads((ROOT / "eval" / "smoke_questions.json").read_text(encoding="utf-8"))["questions"]
    sanity = json.loads((ROOT / "eval" / "sanity_cases.json").read_text(encoding="utf-8"))
    out = [("smoke", q["q"]) for q in smoke]
    out += [("sanity", c["query"]) for c in sanity["cases"] if c["query"].strip()]
    out += [("negative", n["query"]) for n in sanity["negatives"]]
    return out


def main() -> int:
    rt = retrieve.Retriever()
    lines = [f"# Retrieval check on `{rt.col.name}` ({rt.col.count()} chunks)", "",
             f"fetch {config.RETRIEVE_FETCH}, max groups {config.RETRIEVE_MAX_GROUPS}, min words {config.RETRIEVE_MIN_WORDS}, "
             f"group similarity {config.RETRIEVE_GROUP_MIN_SIMILARITY}, clarify delta {config.RETRIEVE_CLARIFY_DELTA} / "
             f"max distance {config.RETRIEVE_CLARIFY_MAX_DISTANCE}", ""]
    conflicts = 0
    for kind, q in questions():
        r = rt.retrieve(q)
        conflicts += r.product_conflict
        flags = []
        if r.detected_products:
            flags.append("product named: " + ", ".join(r.detected_products))
        if r.product_miss:
            flags.append("PRODUCT MISS")
        if r.product_conflict:
            flags.append("CLARIFY: " + " / ".join(retrieve.topic_name(t) for t in r.conflict_topics))
        lines += [f"## [{kind}] {q}", "",
                  f"fetched {r.fetched}, dropped short {r.dropped_short}, dropped other product {r.dropped_other_product}"
                  + (" | " + " | ".join(flags) if flags else ""), ""]
        for n, g in enumerate(r.groups, 1):
            body = g.text.split("\n", 1)[-1].replace("\n", " / ")
            lines.append(f"{n}. {g.distance:.3f} | {g.topic_name} | {g.url.replace(config.BASE_URL, '')} | "
                         f"{g.section_title[:60]} | group of {g.size}, {len(g.sources)} source(s){' | SHORT' if g.short else ''}")
            lines.append(f"   {body[:160]}")
        lines.append("")
    lines.insert(2, f"questions with a product conflict (clarify): {conflicts}\n")
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "retrieve_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:3]))
    for line in lines:
        if line.startswith("## ") or line.startswith("fetched") or line.startswith("1. "):
            print(line[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
