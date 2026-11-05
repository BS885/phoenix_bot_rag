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
from ingest import chunk, clean, urls
from index import embed


class IndexMismatchError(Exception):
    """The active index was built with a different embedding setup than the current config."""


# ---------- loading ----------

def load_chunks(index_path=None) -> tuple[list[dict], dict]:
    """Chunks of every page in index.json that is not marked gone and not empty."""
    index_path = index_path or config.FETCH_INDEX
    index = json.loads(index_path.read_text(encoding="utf-8"))
    chunks, report = [], {"pages": len(index), "gone": 0, "empty": 0, "missing_file": 0, "no_content": 0, "out_of_scope": 0}
    for url, entry in index.items():
        if entry.get("gone"):
            report["gone"] += 1
            continue
        if not entry.get("file") or entry.get("status", "ok") != "ok":  # failed / 404 entries have no page
            report["no_content"] += 1
            continue
        if not urls.is_allowed_url(url):  # excluded by config after it was saved: kept on disk, not indexed
            report["out_of_scope"] += 1
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


# ---------- scope (build only some of the chunks) ----------

def select_chunks(chunks: list[dict], wave: str | None = None, areas=None, products=None) -> list[dict]:
    """Keep chunks of a wave (config.WAVES) and/or of the given areas / products (first URL segment).

    Applied AFTER the cross-page dedupe, so every chunk keeps exactly the id, text and metadata it will have in
    the full index: a later, wider build finds these vectors in the cache and only embeds what is new.
    """
    areas, products = set(areas or []), set(products or [])
    if wave:
        spec = config.WAVES[wave]
        areas |= set(spec.get("areas", ()))
        products |= set(spec.get("segments", ()))
    if not areas and not products:
        return chunks
    return [c for c in chunks if c["area"] in areas or c["product"] in products]


def add_cached(all_chunks: list[dict], selected: list[dict], cached_keys: set[str]) -> list[dict]:
    """Cumulative build: the selected chunks PLUS every chunk whose vector is already in the cache.

    A partial build therefore never drops what an earlier build already embedded. Original order is kept.
    """
    chosen = {c["id"] for c in selected}
    return [c for c in all_chunks
            if c["id"] in chosen or embed.cache_key(embed.build_doc_input(c["text"])) in cached_keys]


PRODUCT_ORDER = ("health-insurance", "life-insurance", "disability-and-incapacity-to-work", "car-insurance",
                 "apartment-insurance", "travel-insurance", "ishtalmutfund", "gemel", "pension-savings", "savings")


def product_table(chunks: list[dict], cached_keys: set[str], order=PRODUCT_ORDER) -> list[dict]:
    """Distinct texts (and estimated tokens) still to embed per product, after dedupe and without the cache."""
    rows: dict[str, dict] = {}
    seen: set[str] = set()
    for c in chunks:
        text = embed.build_doc_input(c["text"])
        key = embed.cache_key(text)
        r = rows.setdefault(c["product"], {"product": c["product"], "chunks": 0, "to_embed": 0, "tokens": 0})
        r["chunks"] += 1
        if key not in cached_keys and key not in seen:
            seen.add(key)
            r["to_embed"] += 1
            r["tokens"] += embed.estimate_tokens(text)
    first = [rows.pop(p) for p in order if p in rows]
    rest = sorted(rows.values(), key=lambda r: -r["to_embed"])
    other = {"product": f"other ({len(rest)} products)", "chunks": sum(r["chunks"] for r in rest),
             "to_embed": sum(r["to_embed"] for r in rest), "tokens": sum(r["tokens"] for r in rest)}
    return first + [other]


def plan_daily(chunks: list[dict], cached_keys: set[str], budget: int, order=None) -> dict:
    """Pick, in priority order, the chunks whose NEW texts fit in `budget` (texts for today).

    Products follow config.DAILY_PRODUCT_ORDER, then the others (largest first); within a product the chunk order
    is kept. The cut can fall in the middle of a product. Returns the chosen chunks, how many texts they need, how
    many texts are still missing afterwards and which products are done / partly done.
    """
    order = list(order or config.DAILY_PRODUCT_ORDER)
    by_product: dict[str, list[dict]] = {}
    for c in chunks:
        by_product.setdefault(c["product"], []).append(c)
    rest = sorted((p for p in by_product if p not in order), key=lambda p: -len(by_product[p]))
    chosen, new_keys, missing, status = [], set(), 0, {}
    for product in [p for p in order if p in by_product] + rest:
        todo_total = todo_taken = 0
        for c in by_product[product]:
            key = embed.cache_key(embed.build_doc_input(c["text"]))
            if key in cached_keys or key in new_keys:
                continue
            todo_total += 1
            if len(new_keys) < budget:
                new_keys.add(key)
                chosen.append(c)
                todo_taken += 1
            else:
                missing += 1
        if todo_total:
            status[product] = "done" if todo_taken == todo_total else ("partial" if todo_taken else "waiting")
    return {"chunks": chosen, "texts": len(new_keys), "missing": missing, "status": status}


def days_left(missing: int, rpd: int | None = None, reserve: int | None = None) -> int:
    """Whole days still needed to embed `missing` texts with the daily quota minus the reserve for questions."""
    per_day = (rpd or config.EMBED_RPD) - (config.EMBED_DAILY_RESERVE if reserve is None else reserve)
    return 0 if missing <= 0 else -(-missing // max(per_day, 1))


def scope_label(wave=None, areas=None, products=None) -> str:
    parts = ([f"wave {wave}"] if wave else []) + [f"area {a}" for a in areas or []] + [f"product {p}" for p in products or []]
    return ", ".join(parts) or "all"


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


def build_collection(client, name: str, chunks: list[dict], vectors: list[list[float]], scope: str = "all"):
    col = client.create_collection(
        name, configuration={"hnsw": {"space": "cosine"}},
        metadata={**collection_meta(), "scope": scope, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
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

def build(chunks: list[dict], embedder, cache, client, sleep=time.sleep, scope: str = "all") -> dict:
    """Embed, build a new collection, verify it, activate it, prune old ones. Returns a summary."""
    inputs = [embed.build_doc_input(c["text"]) for c in chunks]
    def progress(done, total, calls):
        if calls == 1 or calls % 10 == 0 or done >= total:
            print(f"  embedded {done}/{total} texts ({calls} API calls); saved to cache, a rerun resumes from here", flush=True)

    vectors, stats = embed.embed_inputs(inputs, embedder, cache, sleep=sleep, progress=progress)
    name = f"{config.COLLECTION_PREFIX}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"  # sorts chronologically
    try:
        col = build_collection(client, name, chunks, vectors, scope)
        verify_collection(col, len(chunks))
    except Exception:
        try:
            client.delete_collection(name)  # never leave a half-built collection behind
        except Exception:  # noqa: BLE001
            pass
        raise
    activate(name, {"chunks": len(chunks), "pages": len({c["url"] for c in chunks}), "scope": scope,
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    removed = prune_old(client, name)
    return {"collection": name, "embed": stats, "removed_collections": removed, "vectors": vectors}


def quota_plan(inputs: list[str], found_keys: set[str], usage) -> dict:
    """Texts / tokens still to embed for these inputs, against what is left of today's quota."""
    todo = list({embed.cache_key(t): t for t in inputs if embed.cache_key(t) not in found_keys}.values())
    tokens = sum(embed.estimate_tokens(t) for t in todo)
    return {"texts": len(todo), "tokens": tokens, "calls": len(embed.make_batches(todo)),
            "used_today": usage.used, "remaining_today": usage.remaining,
            "minutes": max(len(todo) / config.EMBED_RPM, tokens / config.EMBED_TPM),
            "fits_today": len(todo) <= usage.remaining}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API calls, nothing written")
    ap.add_argument("--limit", type=int, help="use only the first N chunks")
    ap.add_argument("--wave", choices=sorted(config.WAVES), help="build only the chunks of this wave (config.WAVES)")
    ap.add_argument("--area", action="append", help="build only this area (claims / customer-service / product); repeatable")
    ap.add_argument("--product", action="append", help="build only this product (first URL segment); repeatable")
    ap.add_argument("--no-cumulative", action="store_true",
                    help="build ONLY the selected scope (default: the scope plus every chunk already in the cache)")
    ap.add_argument("--daily", action="store_true",
                    help="embed in priority order (config.DAILY_PRODUCT_ORDER) until only --reserve texts of today's "
                         "quota are left; cumulative build; stops politely with a summary")
    ap.add_argument("--reserve", type=int, default=config.EMBED_DAILY_RESERVE,
                    help="--daily: texts of today's quota to leave free for questions")
    ap.add_argument("--by-product", action="store_true", help="print texts still to embed per product, then exit")
    ap.add_argument("--used-today", type=int, help="set today's used-texts counter (copy it from AI Studio)")
    args = ap.parse_args(argv)

    usage = embed.DailyUsage()
    if args.used_today is not None:
        usage.set_used(args.used_today)

    chunks, rep = load_chunks()
    print(f"pages: {rep['pages']} (gone: {rep['gone']}, empty: {rep['empty']}, missing file: {rep['missing_file']}, "
          f"no content: {rep['no_content']}, out of scope: {rep['out_of_scope']})")
    chunks, dd = dedupe_across_pages(chunks)
    print(f"chunks: {dd['before']} -> {dd['after']} after merging identical text across pages "
          f"({dd['groups_with_dups']} groups, {dd['merged_away']} removed)")
    cache = embed.EmbedCache(config.EMBED_CACHE_PATH)
    if args.by_product:
        cached = cache.present([embed.cache_key(embed.build_doc_input(c["text"])) for c in chunks])
        print(f"{'product':36} {'chunks':>7} {'to embed':>9} {'tokens(est)':>12}")
        for r in product_table(chunks, cached):
            print(f"{r['product']:36} {r['chunks']:>7} {r['to_embed']:>9} {r['tokens']:>12}")
        print(f"cached already: {len(cached)}; today {usage.used}/{config.EMBED_RPD} used")
        return 0
    daily = None
    if args.daily:
        cached = cache.present([embed.cache_key(embed.build_doc_input(c["text"])) for c in chunks])
        daily = plan_daily(chunks, cached, max(usage.remaining - args.reserve, 0))
        print(f"--daily: {usage.used}/{config.EMBED_RPD} used today, reserve {args.reserve} -> budget "
              f"{max(usage.remaining - args.reserve, 0)} texts; this run embeds {daily['texts']}, "
              f"{daily['missing']} stay missing (about {days_left(daily['missing'], reserve=args.reserve)} more day(s))")
        print("  products: " + ", ".join(f"{p} {st}" for p, st in daily["status"].items()))
        if not daily["chunks"]:
            print("nothing new to embed today" if daily["missing"] else "everything is already embedded")
            if daily["missing"]:
                return 0
        selected = daily["chunks"]
        chunks = add_cached(chunks, selected, cached)
        print(f"cumulative: {len(selected)} new chunks + {len(chunks) - len(selected)} already in the cache "
              f"-> {len(chunks)} chunks from {len({c['url'] for c in chunks})} pages")
        scope = "daily (priority order) + cached"
    else:
        scope = scope_label(args.wave, args.area, args.product)
    if scope != "all" and not args.daily:
        selected = select_chunks(chunks, args.wave, args.area, args.product)
        print(f"scope [{scope}]: {len(selected)} chunks from {len({c['url'] for c in selected})} pages")
        if args.no_cumulative:
            chunks = selected
        else:
            cached = cache.present([embed.cache_key(embed.build_doc_input(c["text"])) for c in chunks])
            chunks = add_cached(chunks, selected, cached)
            print(f"cumulative: + {len(chunks) - len(selected)} chunks already in the cache -> {len(chunks)} chunks "
                  f"from {len({c['url'] for c in chunks})} pages")
            scope += " + cached"
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

    keys = [embed.cache_key(t) for t in inputs]
    found = cache.get_many(list(set(keys)))
    plan = quota_plan(inputs, set(found), usage)
    print(f"embeddings: {len(set(keys))} distinct texts, {len(found)} cached, {plan['texts']} to compute "
          f"(~{plan['tokens']} tokens est., ~{plan['calls']} API calls)")
    print(f"quota: {plan['used_today']}/{config.EMBED_RPD} texts used today, {plan['remaining_today']} left; "
          f"limits {config.EMBED_RPM} texts/min, {config.EMBED_TPM} tokens/min -> about {plan['minutes']:.1f} min; "
          f"{'FITS today' if plan['fits_today'] else 'DOES NOT FIT today: stops at the limit, resumes tomorrow'}")
    if args.dry_run:
        return 0

    def waiting(delay):
        print(f"  rate limit: waiting {delay:.0f}s", flush=True)

    embedder = embed.ThrottledEmbedder(embed.GeminiEmbedder(), embed.RateLimiter(on_wait=waiting), usage)
    try:
        result = build(chunks, embedder, cache, get_client(), scope=scope)
    except embed.QuotaStop as stop:
        kind = "daily" if stop.daily else "per-minute"
        print(f"STOPPED ({kind} quota): {stop}")
        print(f"embedded in this run: {stop.done}; remaining: {stop.remaining} texts. Everything finished is in the "
              f"cache ({config.EMBED_CACHE_PATH.name}); no collection was built. Run the same command again "
              f"{'tomorrow (after midnight Pacific)' if stop.daily else 'in a few minutes'} to continue.")
        if daily is not None:
            print(f"SUMMARY: stopped early; {stop.remaining} texts of today's plan and {daily['missing']} beyond it "
                  f"are still missing; about {days_left(stop.remaining + daily['missing'], reserve=args.reserve)} more day(s)")
        return 3
    write_near_report(near_duplicate_report(chunks, result["vectors"]))
    print(f"built {result['collection']} [{scope}]: {len(chunks)} chunks | API calls {result['embed']['api_calls']}, "
          f"cache hits {result['embed']['hits']}, computed {result['embed']['misses']} | "
          f"removed old: {result['removed_collections']}")
    print(f"quota after the build: {usage.used}/{config.EMBED_RPD} texts used today, {usage.remaining} left")
    if daily is not None:
        print(f"SUMMARY: embedded {result['embed']['misses']} texts today; {daily['missing']} still missing in "
              f"{sum(1 for st in daily['status'].values() if st != 'done')} product(s); about "
              f"{days_left(daily['missing'], reserve=args.reserve)} more day(s) at {config.EMBED_RPD - args.reserve} texts/day")
    return 0


if __name__ == "__main__":
    sys.exit(main())
