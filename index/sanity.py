"""Pure logic of the embedding sanity check: resolving expected chunks and scoring (no API, no I/O)."""
import json
from pathlib import Path

import numpy as np

MIN_FILLED = 5          # filled queries required to run
MIN_SEPARATION = 3      # queries (non-coverage) needed for a meaningful separation figure


class CaseError(Exception):
    pass


def load_cases(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def filled(cases: list[dict]) -> list[dict]:
    return [c for c in cases if c.get("query", "").strip()]


def cos_dist(a, b) -> float:
    a, b = np.asarray(a, dtype="float64"), np.asarray(b, dtype="float64")
    return float(1.0 - a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def resolve_case(case: dict, chunks: list[dict]) -> list[tuple[str, int]]:
    """[(role, chunk index)] for every acceptable entry; each entry must match exactly one chunk."""
    out = []
    for a in case["acceptable"]:
        matches = [i for i, c in enumerate(chunks)
                   if (c["url"] == a["url"] or a["url"] in c.get("dup_urls", "").split("|"))
                   and a["section_title"] in c["section_title"]
                   and a.get("text_contains", "") in c["text"]]
        if len(matches) != 1:
            raise CaseError(f"{case['id']}: {len(matches)} chunks match {a['url']} / {a['section_title']!r} "
                            f"/ {a.get('text_contains', '')!r} (need exactly 1)")
        out.append((a.get("role", "secondary"), matches[0]))
    return out


def eligible(case: dict, resolved: list[tuple[str, int]]) -> list[int]:
    """top1: only primary chunks count; top3: every acceptable chunk counts."""
    if case["metric"] == "top1":
        return [i for role, i in resolved if role == "primary"]
    return [i for _, i in resolved]


def evaluate_case(case: dict, resolved, sample: list[int], q_vec, doc_vecs) -> dict:
    """sample[k] is the chunk index of doc_vecs[k]."""
    dists = [cos_dist(q_vec, d) for d in doc_vecs]
    pos = [sample.index(i) for i in eligible(case, resolved)]
    best = min(pos, key=lambda p: dists[p])
    rank = 1 + sum(d < dists[best] for d in dists)
    k = 1 if case["metric"] == "top1" else 3
    return {"id": case["id"], "type": case["type"], "metric": case["metric"], "distance": dists[best],
            "rank": rank, "hit": rank <= k, "nearest": int(np.argmin(dists)),
            "in_separation": bool(case.get("include_in_separation", True)) and case["type"] != "coverage"}


def evaluate_negative(q_vec, doc_vecs) -> dict:
    dists = [cos_dist(q_vec, d) for d in doc_vecs]
    j = int(np.argmin(dists))
    return {"distance": dists[j], "nearest": j}


def summarize(results: list[dict], negatives: list[dict]) -> dict:
    """Separation uses only non-coverage cases; hit rates are reported for all."""
    sep = [r for r in results if r["in_separation"]]
    neg_min = min((n["distance"] for n in negatives), default=float("nan"))
    sep_max = max((r["distance"] for r in sep), default=float("nan"))
    return {
        "hits": sum(r["hit"] for r in results), "cases": len(results),
        "separation_cases": len(sep), "separation_hits": sum(r["hit"] for r in sep),
        "paraphrase_max": sep_max, "negative_min": neg_min,
        "separated": bool(sep) and sep_max < neg_min, "margin": neg_min - sep_max,
        "small_separation_set": len(sep) < MIN_SEPARATION,
    }
