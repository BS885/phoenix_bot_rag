"""Build the vector index from the saved HTML (data/raw_html). Never touches the website.

Always builds a NEW Chroma collection and activates it only after it was verified, so the active
index is never left half-built and stale chunks (changed or vanished pages) disappear on rebuild.

Usage:
    python -m index.build_index --dry-run     # counts, duplicates, token warnings; no API, no writes
    python -m index.build_index               # full build (needs GEMINI_API_KEY)
    python -m index.build_index --limit 20    # build from the first 20 chunks only (smoke test)
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime

import config
from ingest import chunk, clean
from index import embed


class IndexMismatchError(Exception):
    """The active index was built with a different embedding setup than the current config."""


# ---------- loading ----------

def load_chunks(index_path=None) -> tuple[list[dict], dict]:
    """Chunks of every page in index.json that is not marked gone and not empty."""
    index_path = index_path or config.FETCH_INDEX
    index = json.loads(index_path.read_text(encoding="utf-8"))
    chunks, report = [], {"pages": len(index), "gone": 0, "empty": 0, "missing_file": 0}
    for url, entry in index.items():
        if entry.get("gone"):
            report["gone"] += 1
            continue
        path = index_path.parent / entry["file"]
        if not path.exists():
            report["missing_file"] += 1
            continue
        doc = clean.clean_html(path.read_bytes())
        if doc["empty"]:
            report["empty"] += 1
            continue
        chunks += chunk.chunk_page(doc, url, entry)
    return chunks, report


# ---------- duplicates across pages ----------

def _norm(text: str) -> str:
    text = text.replace("״", '"').replace("”", '"').replace("“", '"').replace("’", "'").replace("׳", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def dedupe_key(c: dict) -> str:
    """Section title + body, without the 'page title > ...' head line, so the same block on two pages matches."""
    _, body = embed.split_head(c["text"])
    last_section = c["section_title"].split(" > ")[-1]
    return hashlib.sha1(_norm(f"{last_section}\n{body}").encode("utf-8")).hexdigest()


def dedupe_across_pages(chunks: list[dict]) -> tuple[list[dict], dict]:
    """Merge chunks whose text (without the page title) is identical on several pages.

    The primary page is chosen by area (claims, customer-service, product) and then by URL. The
    other URLs are kept in dup_urls ('|'-separated) so an answer can cite them. Near-identical
    chunks are NOT merged here (see near_duplicate_report).
    """
    groups: dict[str, list[dict]] = {}
    for c in chunks:
        groups.setdefault(dedupe_key(c), []).append(c)
    out = []
    for members in groups.values():
        members.sort(key=lambda c: (config.AREA_RANK.get(c["area"], 9), c["url"], c["id"]))
        primary = dict(members[0])
        others = sorted({m["url"] for m in members[1:] if m["url"] != primary["url"]})
        primary["dup_count"] = len(members)
        primary["dup_urls"] = "|".join(others)
        out.append(primary)
    order = {c["id"]: n for n, c in enumerate(chunks)}
    out.sort(key=lambda c: order[c["id"]])
    merged = len(chunks) - len(out)
    return out, {"before": len(chunks), "after": len(out), "merged_away": merged,
                 "groups_with_dups": sum(1 for m in groups.values() if len(m) > 1)}


# ---------- collection ----------

def collection_meta() -> dict:
    return {"embed_model": config.EMBED_MODEL, "embed_dim": config.EMBED_DIM, "distance": "cosine",
            "doc_template": config.EMBED_DOC_TEMPLATE, "query_template": config.EMBED_QUERY_TEMPLATE}


def clean_metadata(c: dict) -> dict:
    """Chroma metadata: scalars only, never None."""
    skip = {"text"}
    out = {}
    for k, v in c.items():
        if k in skip:
            continue
        out[k] = "" if v is None else v
    return out


def get_client(path=None):
    import chromadb
    return chromadb.PersistentClient(path=str(path or config.CHROMA_DIR))


def build_collection(client, name: str, chunks: list[dict], vectors: list[list[float]]):
    col = client.create_collection(
        name, configuration={"hnsw": {"space": "cosine"}},
        metadata={**collection_meta(), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        embedding_function=None)  # vectors are always supplied: never let Chroma download its own model
    for i in range(0, len(chunks), 100):
        part = slice(i, i + 100)
        col.upsert(ids=[c["id"] for c in chunks[part]], embeddings=vectors[part],
                   documents=[c["text"] for c in chunks[part]],
                   metadatas=[clean_metadata(c) for c in chunks[part]])
    return col


def verify_collection(col, expected: int) -> None:
    n = col.count()
    if n != expected:
        raise RuntimeError(f"collection has {n} chunks, expected {expected}")
    one = col.get(limit=1, include=["metadatas", "embeddings"])
    if len(one["embeddings"][0]) != config.EMBED_DIM:
        raise RuntimeError("stored vector has the wrong dimension")
    if any(v is None for v in one["metadatas"][0].values()):
        raise RuntimeError("metadata contains None")


def activate(name: str, stats: dict) -> None:
    config.INDEX_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.INDEX_MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps({"collection": name, **collection_meta(), **stats}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(config.INDEX_MANIFEST)  # atomic switch


def prune_old(client, active: str, keep: int | None = None) -> list[str]:
    keep = config.KEEP_OLD_COLLECTIONS if keep is None else keep
    names = sorted(c.name for c in client.list_collections() if c.name.startswith(config.COLLECTION_PREFIX + "_"))
    old = [n for n in names if n != active]
    doomed = old[:-keep] if keep else old
    for n in doomed:
        client.delete_collection(n)
    return doomed


def assert_index_compatible(meta: dict) -> None:
    """Stop the app when the index was built with another model/dimension/templates than the config."""
    current = collection_meta()
    for key in ("embed_model", "embed_dim", "distance", "doc_template", "query_template"):
        if meta.get(key) != current[key]:
            raise IndexMismatchError(
                f"the index was built with {key}={meta.get(key)!r} but the config says {current[key]!r}: "
                "rebuild the index (python -m index.build_index)")


def get_active_collection(client=None):
    """The collection named in the manifest, after the compatibility check."""
    if not config.INDEX_MANIFEST.exists():
        raise IndexMismatchError("no index yet: run python -m index.build_index")
    manifest = json.loads(config.INDEX_MANIFEST.read_text(encoding="utf-8"))
    assert_index_compatible(manifest)
    client = client or get_client()
    col = client.get_collection(manifest["collection"], embedding_function=None)
    assert_index_compatible(col.metadata or {})
    return col


# ---------- reports ----------

def near_duplicate_report(chunks: list[dict], vectors: list[list[float]], threshold: float | None = None,
                          max_pairs: int = 200) -> list[tuple[float, dict, dict]]:
    """Pairs of chunks from DIFFERENT pages that are closer than the threshold (cosine distance).

    Report only: merging near-identical chunks is decided in stage 5 by recall@k.
    """
    import numpy as np
    threshold = config.DEDUP_NEAR_THRESHOLD if threshold is None else threshold
    m = np.asarray(vectors, dtype="float32")
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    pairs = []
    for start in range(0, len(m), 1000):  # blockwise, so a large corpus does not need an n x n matrix
        dist = 1.0 - m[start:start + 1000] @ m.T
        for r, row in enumerate(dist):
            i = start + r
            for j in np.nonzero(row < threshold)[0]:
                if j > i and chunks[i]["url"] != chunks[j]["url"]:
                    pairs.append((float(row[j]), chunks[i], chunks[j]))
    pairs.sort(key=lambda p: p[0])
    return pairs[:max_pairs]


def write_near_report(pairs) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"# Near-duplicate chunks across pages (cosine distance < {config.DEDUP_NEAR_THRESHOLD})", "",
             f"{len(pairs)} pairs (report only, nothing merged).", ""]
    for d, a, b in pairs:
        lines.append(f"- {d:.4f} | {a['url'].replace(config.BASE_URL, '')} [{a['section_title'][:40]}] "
                     f"<> {b['url'].replace(config.BASE_URL, '')} [{b['section_title'][:40]}]")
    (config.LOG_DIR / "near_duplicates.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- main ----------

def build(chunks: list[dict], embedder, cache, client, sleep=time.sleep) -> dict:
    """Embed, build a new collection, verify it, activate it, prune old ones. Returns a summary."""
    inputs = [embed.build_doc_input(c["text"]) for c in chunks]
    vectors, stats = embed.embed_inputs(inputs, embedder, cache, sleep=sleep)
    name = f"{config.COLLECTION_PREFIX}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"  # sorts chronologically
    try:
        col = build_collection(client, name, chunks, vectors)
        verify_collection(col, len(chunks))
    except Exception:
        try:
            client.delete_collection(name)  # never leave a half-built collection behind
        except Exception:  # noqa: BLE001
            pass
        raise
    activate(name, {"chunks": len(chunks), "pages": len({c["url"] for c in chunks}),
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    removed = prune_old(client, name)
    return {"collection": name, "embed": stats, "removed_collections": removed, "vectors": vectors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API calls, nothing written")
    ap.add_argument("--limit", type=int, help="use only the first N chunks")
    args = ap.parse_args(argv)

    chunks, rep = load_chunks()
    print(f"pages: {rep['pages']} (gone: {rep['gone']}, empty: {rep['empty']}, missing file: {rep['missing_file']})")
    chunks, dd = dedupe_across_pages(chunks)
    print(f"chunks: {dd['before']} -> {dd['after']} after merging identical text across pages "
          f"({dd['groups_with_dups']} groups, {dd['merged_away']} removed)")
    if args.limit:
        chunks = chunks[:args.limit]
        print(f"--limit: {len(chunks)} chunks")

    inputs = [embed.build_doc_input(c["text"]) for c in chunks]
    warn = embed.token_warnings(inputs)
    for i, tokens in warn:
        print(f"WARNING: ~{tokens} tokens (limit {config.EMBED_MODEL_MAX_TOKENS}, silently truncated): "
              f"{chunks[i]['url']} [{chunks[i]['section_title'][:50]}]")
    longest = max((embed.estimate_tokens(t) for t in inputs), default=0)
    print(f"longest chunk ~{longest} tokens (estimate), warnings: {len(warn)}")

    cache = embed.EmbedCache(config.EMBED_CACHE_PATH)
    keys = [embed.cache_key(t) for t in inputs]
    found = cache.get_many(list(set(keys)))
    distinct_uncached = list({k: t for t, k in zip(inputs, keys) if k not in found}.values())
    calls = len(embed.make_batches(distinct_uncached))
    print(f"embeddings: {len(set(keys))} distinct texts, {len(found)} cached, "
          f"{len(distinct_uncached)} to compute (~{calls} API calls)")
    if args.dry_run:
        return 0

    result = build(chunks, embed.GeminiEmbedder(), cache, get_client())
    write_near_report(near_duplicate_report(chunks, result["vectors"]))
    print(f"built {result['collection']}: {len(chunks)} chunks | API calls {result['embed']['api_calls']}, "
          f"cache hits {result['embed']['hits']}, computed {result['embed']['misses']} | "
          f"removed old: {result['removed_collections']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
