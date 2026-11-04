"""Embedding sanity check (needs GEMINI_API_KEY except with --check-cases).

Checks, in this order:
  0. batch consistency: a text embedded alone equals the same text embedded inside a batch
     (guards against inputs being truncated or aggregated when sent together)
  1. the cases in eval/sanity_cases.json: for each query the best acceptable chunk must rank in the
     top-1 (clear questions, only 'primary' chunks) or top-3 (ambiguous questions, any acceptable chunk)
     among the sample; coverage questions are reported but excluded from the separation calculation
  2. separation: the farthest correct case must be closer than the nearest negative
  3. with vs without the query template (gemini-embedding-2 has no task_type: the template is the task)

Report: logs/embed_sanity.md

Usage:
    python scripts/embed_sanity.py --check-cases    # resolve and print the question -> chunks mapping, no API
    python scripts/embed_sanity.py [--sample 20]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import config  # noqa: E402
from index import build_index as bi  # noqa: E402
from index import embed, sanity  # noqa: E402

CASES = ROOT / "eval" / "sanity_cases.json"


def pick_sample(chunks: list[dict], required: set[int], n: int) -> list[int]:
    """Every acceptable chunk plus evenly spaced others (deterministic), at least n in total."""
    chosen = sorted(required)
    rest = [i for i in range(len(chunks)) if i not in required]
    need = max(0, n - len(chosen))
    if need and rest:
        step = max(1, len(rest) // need)
        chosen += rest[::step][:need]
    return sorted(chosen)


def short(chunk: dict) -> str:
    return f"{chunk['url'].replace(config.BASE_URL, '')} | {chunk['section_title'][:45]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--check-cases", action="store_true", help="only resolve and print the mapping (no API)")
    args = ap.parse_args()

    cases_file = sanity.load_cases(CASES)
    cases = sanity.filled(cases_file["cases"])
    negatives = cases_file["negatives"]
    if len(cases) < sanity.MIN_FILLED:
        print(f"Only {len(cases)} queries are filled in {CASES.name}; need at least {sanity.MIN_FILLED}.")
        return 1

    chunks, _ = bi.load_chunks()
    chunks, _ = bi.dedupe_across_pages(chunks)
    try:
        resolved = {c["id"]: sanity.resolve_case(c, chunks) for c in cases}
    except sanity.CaseError as e:
        print("Mapping error:", e)
        return 1

    if args.check_cases:
        for c in cases:
            print(f"\n{c['id']} [{c['type']}, {c['metric']}] {c['query']}")
            for role, i in resolved[c["id"]]:
                print(f"   {role:9} #{i:03d} {short(chunks[i])} ({len(chunks[i]['text'].split())}w)")
        return 0

    required = {i for r in resolved.values() for _, i in r}
    sample = pick_sample(chunks, required, args.sample)
    docs = [embed.build_doc_input(chunks[i]["text"]) for i in sample]

    embedder = embed.GeminiEmbedder()
    cache = embed.EmbedCache(config.EMBED_CACHE_PATH)

    # 0. batch consistency (direct API calls, no cache)
    probe = docs[:5]
    single = [embedder.embed([t])[0] for t in probe]
    batched = embedder.embed(probe)
    batch_gap = max(sanity.cos_dist(a, b) for a, b in zip(single, batched))

    doc_vecs, s1 = embed.embed_inputs(docs, embedder, cache)
    texts = [c["query"] for c in cases] + [n["query"] for n in negatives]
    q_vecs, s2 = embed.embed_inputs([embed.build_query_input(t) for t in texts], embedder, cache)
    raw_vecs, s3 = embed.embed_inputs(texts, embedder, cache)  # without the template

    def score(vecs):
        res = [sanity.evaluate_case(c, resolved[c["id"]], sample, vecs[k], doc_vecs) for k, c in enumerate(cases)]
        neg = [sanity.evaluate_negative(vecs[len(cases) + k], doc_vecs) for k in range(len(negatives))]
        return res, neg, sanity.summarize(res, neg)

    res, neg, sm = score(q_vecs)
    raw_res, raw_neg, raw_sm = score(raw_vecs)

    out = ["# Embedding sanity check", "",
           f"model `{config.EMBED_MODEL}`, dim {config.EMBED_DIM}, {len(sample)} chunks in the sample, "
           f"{len(cases)} cases, {len(negatives)} negatives", "",
           "## 0. Batch consistency", "",
           f"max cosine distance, alone vs inside a batch of {len(probe)}: **{batch_gap:.5f}** "
           f"({'OK' if batch_gap < 1e-3 else 'PROBLEM: batching changes the vectors, set EMBED_BATCH=1'})", "",
           "## 1. Cases (with query template)", "",
           f"hits: **{sm['hits']}/{sm['cases']}** (top-1 for clear questions, top-3 for ambiguous ones)", "",
           "| id | type | metric | hit | rank | distance | nearest chunk in the sample |", "|---|---|---|---|---|---|---|"]
    for r in res:
        out.append(f"| {r['id']} | {r['type']} | {r['metric']} | {'yes' if r['hit'] else 'NO'} | {r['rank']} | "
                   f"{r['distance']:.3f} | {short(chunks[sample[r['nearest']]])} |")
    out += ["", "## 2. Separation (coverage questions excluded)", "",
            f"cases used: {sm['separation_cases']}, correct: {sm['separation_hits']}; farthest correct-case distance "
            f"{sm['paraphrase_max']:.3f}, nearest negative {sm['negative_min']:.3f} -> "
            f"**{'separated' if sm['separated'] else 'NOT separated (ranges overlap)'}** (margin {sm['margin']:+.3f})"]
    if sm["small_separation_set"]:
        out.append(f"\nNote: only {sm['separation_cases']} cases enter the separation figure (< {sanity.MIN_SEPARATION}); "
                   "treat it as indicative only.")
    out += ["", "| negative | kind | distance | nearest chunk | valid until crawled |", "|---|---|---|---|---|"]
    for n, r in zip(negatives, neg):
        out.append(f"| {n['query']} | {n['kind']} | {r['distance']:.3f} | {short(chunks[sample[r['nearest']]])} | "
                   f"{'yes' if n.get('valid_until_crawled') else ''} |")
    out += ["", "## Distance to every acceptable chunk (rank among the sample, 1 = nearest)", "",
            "| id | role | distance | rank | chunk |", "|---|---|---|---|---|"]
    for k, c in enumerate(cases):
        for role, idx in resolved[c["id"]]:
            d = [sanity.cos_dist(q_vecs[k], v) for v in doc_vecs]
            pos = sample.index(idx)
            out.append(f"| {c['id']} | {role} | {d[pos]:.3f} | {1 + sum(x < d[pos] for x in d)} | "
                       f"{short(chunks[idx])} ({len(chunks[idx]['text'].split())}w) |")
    out += ["", "## 3. Without the query template", "",
            f"hits: **{raw_sm['hits']}/{raw_sm['cases']}** (with template: {sm['hits']}/{sm['cases']}); "
            f"farthest correct {raw_sm['paraphrase_max']:.3f}, nearest negative {raw_sm['negative_min']:.3f}", "",
            "## API usage", "",
            f"chunks: {s1['misses']} computed, {s1['hits']} cached; queries: {s2['misses']} computed; "
            f"raw queries: {s3['misses']} computed"]
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "embed_sanity.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out[:14]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
