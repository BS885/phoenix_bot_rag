"""Run the smoke questions (eval/smoke_questions.json) on the ACTIVE index and show the top-3 chunks.

Needs GEMINI_API_KEY (embeds each question). Report: logs/smoke_top3.md

Usage:  python scripts/ask_index.py [--k 3]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from index import build_index as bi  # noqa: E402
from index import embed  # noqa: E402

QUESTIONS = ROOT / "eval" / "smoke_questions.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3)
    args = ap.parse_args()

    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    col = bi.get_active_collection()  # stops here if the index and config disagree
    embedder = embed.make_embedder()
    cache = embed.EmbedCache(config.EMBED_CACHE_PATH)
    out = [f"# Smoke questions, top-{args.k} on collection `{col.name}` ({col.count()} chunks)", ""]
    hits = positives = 0
    for item in questions:
        q = item["q"]
        res = col.query(query_embeddings=[embed.embed_query(q, embedder, cache)], n_results=args.k,
                        include=["metadatas", "documents", "distances"])
        out += [f"## {q}", ""]
        found = False
        for rank, (md, doc, dist) in enumerate(zip(res["metadatas"][0], res["documents"][0], res["distances"][0]), 1):
            ok = bool(item["expect_url_contains"]) and item["expect_url_contains"] in md["url"]
            found = found or ok
            body = doc.split("\n", 1)[1].replace("\n", " / ") if "\n" in doc else doc
            out.append(f"{rank}. {'[match] ' if ok else ''}distance {dist:.3f} | {md['url'].replace(config.BASE_URL, '')} "
                       f"| {md['section_title'][:60]} | dups {md.get('dup_count', 1)}")
            out.append(f"   {body[:200]}")
        if item["expect_url_contains"]:
            positives += 1
            hits += found
        else:  # negative question: show what came back first, and how far it is
            md0, d0 = res["metadatas"][0][0], res["distances"][0][0]
            out.append(f"NEGATIVE: first result is {md0['url'].replace(config.BASE_URL, '')} | "
                       f"{md0['section_title'][:60]} at distance {d0:.3f}")
        out.append("")
    # negatives of the sanity set: first result and its distance
    sanity_neg = json.loads((ROOT / "eval" / "sanity_cases.json").read_text(encoding="utf-8"))["negatives"]
    out += ["## Negatives (eval/sanity_cases.json): first chunk and distance", "",
            "| query | kind | distance | words | short | first result |", "|---|---|---|---|---|---|"]
    for n in sanity_neg:
        r = col.query(query_embeddings=[embed.embed_query(n["query"], embedder, cache)], n_results=1,
                      include=["metadatas", "distances", "documents"])
        md0, d0, doc0 = r["metadatas"][0][0], r["distances"][0][0], r["documents"][0][0]
        body_words = len(doc0.split("\n", 1)[1].split()) if "\n" in doc0 else len(doc0.split())
        out.append(f"| {n['query']} | {n['kind']} | {d0:.3f} | {body_words} | {'yes' if md0.get('short') else 'no'} | "
                   f"{md0['url'].replace(config.BASE_URL, '')} > {md0['section_title'][:50]} |")
    out.append("")
    out.insert(2, f"top-{args.k} contains a matching page for {hits} of {positives} answerable questions "
                  f"(negatives: look at the distances).\n")
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "smoke_top3.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out[:3]))
    for line in out:
        if line.startswith("## ") or line.startswith("1. ") or line.startswith("| ") and "NEGATIVE" not in line:
            print(line[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main())
