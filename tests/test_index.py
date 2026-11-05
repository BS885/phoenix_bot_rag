import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from index import build_index as bi
from index import embed
from ingest import fetch

DIM = 8


def fake_vector(text: str) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [(b - 127.5) / 127.5 for b in h[:DIM]]


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [fake_vector(t) for t in texts]


def make_chunk(url, section, body, area="product", n=0, page="עמוד"):
    return {"id": hashlib.sha1(f"{url}#{n}".encode()).hexdigest()[:20], "text": f"{page} > {section}\n{body}",
            "url": url, "page_title": page, "section_title": section, "section_id": "s", "anchor": "",
            "area": area, "short": False, "collected_at": "", "lastmod": "", "content_hash": "h"}


class IndexTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.object(config, "EMBED_DIM", DIM),
                   mock.patch.object(config, "INDEX_MANIFEST", self.tmp / "manifest.json"),
                   mock.patch.object(config, "CHROMA_DIR", self.tmp / "chroma"),
                   mock.patch.object(config, "EMBED_MIN_INTERVAL_SEC", 0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cache = embed.EmbedCache(":memory:")
        self.addCleanup(self.cache.close)


class TestTextsAndWarnings(IndexTestCase):
    def test_doc_input_uses_head_as_title(self):
        self.assertEqual(embed.build_doc_input("עמוד > מקטע\nגוף הטקסט"), "title: עמוד > מקטע | text: גוף הטקסט")

    def test_query_input_prefix(self):
        self.assertEqual(embed.build_query_input(" כמה זמן? "), "task: search result | query: כמה זמן?")

    def test_token_warning_for_oversized_text(self):
        small, big = "א" * 100, "א" * int(config.EMBED_MODEL_MAX_TOKENS * config.EMBED_CHARS_PER_TOKEN)
        warn = embed.token_warnings([small, big])
        self.assertEqual([i for i, _ in warn], [1])
        self.assertGreater(warn[0][1], config.EMBED_MODEL_MAX_TOKENS * config.EMBED_TOKEN_WARN_RATIO)


class TestCacheAndBatching(IndexTestCase):
    def test_second_run_is_all_cache_hits(self):
        texts = [f"טקסט {i}" for i in range(5)]
        e = FakeEmbedder()
        v1, s1 = embed.embed_inputs(texts, e, self.cache)
        v2, s2 = embed.embed_inputs(texts, e, self.cache)
        self.assertEqual(v1, v2)
        self.assertEqual((s1["misses"], s1["hits"]), (5, 0))
        self.assertEqual((s2["misses"], s2["hits"], s2["api_calls"]), (0, 5, 0))
        self.assertEqual(len(e.calls), 1)

    def test_progress_is_reported_and_a_failed_run_resumes_from_cache(self):
        texts = [f"טקסט {i}" for i in range(40)]
        seen = []

        class Failing(FakeEmbedder):
            def embed(self, texts_):
                if len(self.calls) == 2:
                    raise RuntimeError("quota")
                return super().embed(texts_)

        with self.assertRaises(RuntimeError):
            embed.embed_inputs(texts, Failing(), self.cache, progress=lambda *a: seen.append(a))
        self.assertEqual([a[2] for a in seen], [1, 2])  # progress per finished batch
        e = FakeEmbedder()
        _, stats = embed.embed_inputs(texts, e, self.cache)
        self.assertEqual(stats["hits"], seen[-1][0])   # what was finished is not sent again
        self.assertEqual(stats["hits"] + stats["misses"], 40)

    def test_duplicate_inputs_sent_once_and_order_kept(self):
        e = FakeEmbedder()
        vecs, stats = embed.embed_inputs(["א", "ב", "א"], e, self.cache)
        self.assertEqual(vecs[0], vecs[2])
        self.assertEqual(vecs[1], embed._as_float32(fake_vector("ב")))
        self.assertEqual(sum(len(c) for c in e.calls), 2)

    def test_template_or_dim_change_misses_the_cache(self):
        self.assertNotEqual(embed.cache_key("x"), embed.cache_key("y"))
        before = embed.cache_key("x")
        with mock.patch.object(config, "EMBED_DIM", DIM * 2):
            self.assertNotEqual(before, embed.cache_key("x"))
        with mock.patch.object(config, "EMBED_MODEL", "other-model"):
            self.assertNotEqual(before, embed.cache_key("x"))

    def test_batches_respect_size_and_token_budget(self):
        with mock.patch.object(config, "EMBED_BATCH", 3):
            self.assertEqual([len(b) for b in embed.make_batches(["x"] * 7)], [3, 3, 1])
        heavy = "א" * int(config.EMBED_BATCH_MAX_TOKENS_EST * config.EMBED_CHARS_PER_TOKEN * 0.6)
        self.assertEqual([len(b) for b in embed.make_batches([heavy, heavy, heavy])], [1, 1, 1])

    def test_wrong_shape_from_embedder_is_reported(self):
        class Aggregating:
            def embed(self, texts):
                return [fake_vector("one")]  # one vector for several texts
        with self.assertRaises(embed.EmbeddingError):
            embed.embed_inputs(["א", "ב"], Aggregating(), self.cache)

    def test_wrong_dimension_from_embedder_is_reported(self):
        class WrongDim:
            def embed(self, texts):
                return [[0.0] * (DIM + 1) for _ in texts]
        with self.assertRaises(embed.EmbeddingError):
            embed.embed_inputs(["א"], WrongDim(), self.cache)

    def test_retry_backoff_on_5xx_then_success(self):
        class Err(Exception):
            code = 503
        calls, waits = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise Err("UNAVAILABLE")
            return "ok"
        self.assertEqual(embed.with_retries(flaky, sleep=waits.append), "ok")
        self.assertEqual(waits, [config.EMBED_BACKOFF_SEC, config.EMBED_BACKOFF_SEC * 2])

    def test_per_minute_429_waits_then_stops_without_a_loop(self):
        class Err(Exception):
            code = 429
        calls, waits = [], []

        def always():
            calls.append(1)
            raise Err("RESOURCE_EXHAUSTED: quota exceeded for metric requests per minute")
        with self.assertRaises(embed.QuotaStop) as cm:
            embed.with_retries(always, sleep=waits.append)
        self.assertFalse(cm.exception.daily)
        self.assertEqual(waits, [config.EMBED_QUOTA_WAIT_SEC] * config.EMBED_QUOTA_RETRIES)
        self.assertEqual(len(calls), config.EMBED_QUOTA_RETRIES + 1)

    def test_daily_429_stops_at_once(self):
        class Err(Exception):
            code = 429
        calls, waits = [], []

        def daily():
            calls.append(1)
            raise Err("RESOURCE_EXHAUSTED: EmbedContentRequestsPerDay quota exceeded")
        with self.assertRaises(embed.QuotaStop) as cm:
            embed.with_retries(daily, sleep=waits.append)
        self.assertTrue(cm.exception.daily)
        self.assertEqual((len(calls), waits), (1, []))

    def test_non_retryable_error_not_retried(self):
        class Err(Exception):
            code = 400
        calls = []

        def bad():
            calls.append(1)
            raise Err("bad request")
        with self.assertRaises(Err):
            embed.with_retries(bad, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_max_retries(self):
        class Err(Exception):
            code = 503
        with self.assertRaises(embed.EmbeddingError):
            embed.with_retries(lambda: (_ for _ in ()).throw(Err("x")), sleep=lambda s: None)


class TestQuota(IndexTestCase):
    def test_rate_limiter_waits_for_texts_per_minute(self):
        now, slept = [0.0], []
        lim = embed.RateLimiter(rpm=100, tpm=10**9, clock=lambda: now[0],
                                sleep=lambda d: (slept.append(d), now.__setitem__(0, now[0] + d)))
        for _ in range(6):
            lim.acquire(16, 100)          # 96 texts fit in the first minute
        self.assertEqual(slept, [])
        lim.acquire(16, 100)              # would be 112 > 100: waits until the first event leaves the window
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 60)

    def test_rate_limiter_waits_for_tokens_per_minute(self):
        now, slept = [0.0], []
        lim = embed.RateLimiter(rpm=1000, tpm=30_000, clock=lambda: now[0],
                                sleep=lambda d: (slept.append(d), now.__setitem__(0, now[0] + d)))
        lim.acquire(1, 20_000)
        lim.acquire(1, 9_000)
        self.assertEqual(slept, [])
        lim.acquire(1, 5_000)             # 34,000 > 30,000
        self.assertEqual(len(slept), 1)

    def test_request_larger_than_the_quota_is_refused(self):
        with self.assertRaises(embed.EmbeddingError):
            embed.RateLimiter(rpm=100, tpm=30_000).acquire(101, 10)

    def test_daily_usage_persists_and_resets_on_a_new_day(self):
        path = self.tmp / "u.json"
        u = embed.DailyUsage(path, limit=1000, today="2026-11-05")
        u.set_used(152)
        u.add(8)
        self.assertEqual(embed.DailyUsage(path, limit=1000, today="2026-11-05").used, 160)
        self.assertEqual(embed.DailyUsage(path, limit=1000, today="2026-11-06").used, 0)
        self.assertEqual(embed.DailyUsage(path, limit=1000, today="2026-11-05").remaining, 840)

    def test_stops_at_the_daily_limit_and_keeps_what_was_done(self):
        path = Path(tempfile.mkdtemp()) / "u.json"
        usage = embed.DailyUsage(path, limit=40, today="d")
        inner = FakeEmbedder()
        th = embed.ThrottledEmbedder(inner, embed.RateLimiter(rpm=1000, tpm=10**9), usage)
        texts = [f"טקסט {i}" for i in range(100)]
        with self.assertRaises(embed.QuotaStop) as cm:
            embed.embed_inputs(texts, th, self.cache)
        self.assertTrue(cm.exception.daily)
        self.assertEqual((cm.exception.done, cm.exception.remaining), (32, 68))  # two batches of 16 fit in 40
        self.assertEqual(usage.used, 32)
        # tomorrow: only what is missing is sent
        th2 = embed.ThrottledEmbedder(FakeEmbedder(), embed.RateLimiter(rpm=1000, tpm=10**9),
                                      embed.DailyUsage(path, limit=1000, today="d2"))
        _, stats = embed.embed_inputs(texts, th2, self.cache)
        self.assertEqual((stats["hits"], stats["misses"]), (32, 68))


class TestDedupe(IndexTestCase):
    def test_identical_text_on_two_pages_merged_primary_by_area(self):
        body = "שלב ראשון ושלב שני"
        a = make_chunk("https://x/health-insurance/a/", "מה קורה", body, "product", page="מוצר")
        b = make_chunk("https://x/claims-customer-service/b/", "מה קורה", body, "claims", page="תביעות")
        out, st = bi.dedupe_across_pages([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], b["url"])
        self.assertEqual(out[0]["dup_count"], 2)
        self.assertEqual(out[0]["dup_urls"], a["url"])
        self.assertEqual(st["merged_away"], 1)

    def test_one_word_difference_not_merged(self):
        a = make_chunk("https://x/a/", "כותרת", "טקסט זהה כמעט")
        b = make_chunk("https://x/b/", "כותרת", "טקסט זהה כמעט מאוד")
        self.assertEqual(len(bi.dedupe_across_pages([a, b])[0]), 2)

    def test_page_title_does_not_affect_the_key(self):
        a = make_chunk("https://x/a/", "כותרת", "גוף", page="עמוד א")
        b = make_chunk("https://x/b/", "כותרת", "גוף", page="עמוד ב")
        self.assertEqual(bi.dedupe_key(a), bi.dedupe_key(b))

    def test_unique_chunk_gets_count_one(self):
        out, _ = bi.dedupe_across_pages([make_chunk("https://x/a/", "כותרת", "גוף")])
        self.assertEqual((out[0]["dup_count"], out[0]["dup_urls"]), (1, ""))

    def test_same_page_twice_does_not_list_itself(self):
        a = make_chunk("https://x/a/", "כותרת", "גוף", n=0)
        b = make_chunk("https://x/a/", "כותרת", "גוף", n=1)
        out, _ = bi.dedupe_across_pages([a, b])
        self.assertEqual(out[0]["dup_urls"], "")


class TestCollections(IndexTestCase):
    def chunks(self, n=3):
        return [make_chunk(f"https://x/p{i}/", f"מקטע {i}", f"גוף מספר {i} " * 3, n=i) for i in range(n)]

    def build(self, chunks):
        return bi.build(chunks, FakeEmbedder(), self.cache, bi.get_client(), sleep=lambda s: None)

    def test_build_activates_and_stores_no_none(self):
        res = self.build([dict(c, dup_count=1, dup_urls="") for c in self.chunks()])
        manifest = json.loads(config.INDEX_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["collection"], res["collection"])
        col = bi.get_active_collection()
        self.assertEqual(col.count(), 3)
        for md in col.get(include=["metadatas"])["metadatas"]:
            self.assertNotIn(None, md.values())
        self.assertEqual(col.metadata["distance"], "cosine")

    def test_rebuild_drops_vanished_chunks_and_prunes_old_collections(self):
        c = [dict(x, dup_count=1, dup_urls="") for x in self.chunks(3)]
        first = self.build(c)
        second = self.build(c[:2])  # one page vanished
        third = self.build(c[:2])
        col = bi.get_active_collection()
        self.assertEqual(col.count(), 2)
        ids = set(col.get()["ids"])
        self.assertNotIn(c[2]["id"], ids)
        names = {x.name for x in bi.get_client().list_collections()}
        self.assertNotIn(first["collection"], names)       # pruned
        self.assertIn(third["collection"], names)
        self.assertLessEqual(len(names), config.KEEP_OLD_COLLECTIONS + 1)
        self.assertIn(second["collection"], names | {second["collection"]})

    def test_failed_build_keeps_the_active_index_and_removes_partial_collection(self):
        c = [dict(x, dup_count=1, dup_urls="") for x in self.chunks(3)]
        good = self.build(c)
        manifest_before = config.INDEX_MANIFEST.read_text(encoding="utf-8")

        class Boom(FakeEmbedder):
            def embed(self, texts):
                raise RuntimeError("API down")
        with self.assertRaises(RuntimeError):
            bi.build(c, Boom(), embed.EmbedCache(":memory:"), bi.get_client(), sleep=lambda s: None)
        self.assertEqual(config.INDEX_MANIFEST.read_text(encoding="utf-8"), manifest_before)
        self.assertEqual(bi.get_active_collection().count(), 3)
        names = [x.name for x in bi.get_client().list_collections()]
        self.assertEqual(names, [good["collection"]])

    def test_verify_failure_removes_the_new_collection(self):
        c = [dict(x, dup_count=1, dup_urls="") for x in self.chunks(2)]
        with mock.patch.object(bi, "verify_collection", side_effect=RuntimeError("bad")):
            with self.assertRaises(RuntimeError):
                self.build(c)
        self.assertEqual(bi.get_client().list_collections(), [])
        self.assertFalse(config.INDEX_MANIFEST.exists())

    def test_model_mismatch_stops_startup(self):
        self.build([dict(x, dup_count=1, dup_urls="") for x in self.chunks(2)])
        bi.get_active_collection()  # fine now
        with mock.patch.object(config, "EMBED_MODEL", "gemini-embedding-001"):
            with self.assertRaises(bi.IndexMismatchError) as ctx:
                bi.get_active_collection()
        self.assertIn("embed_model", str(ctx.exception))

    def test_dimension_mismatch_stops_startup(self):
        self.build([dict(x, dup_count=1, dup_urls="") for x in self.chunks(2)])
        with mock.patch.object(config, "EMBED_DIM", DIM * 2):
            with self.assertRaises(bi.IndexMismatchError):
                bi.get_active_collection()

    def test_no_index_yet_is_a_clear_error(self):
        with self.assertRaises(bi.IndexMismatchError):
            bi.get_active_collection()

    def test_query_returns_the_nearest_chunk(self):
        c = [dict(x, dup_count=1, dup_urls="") for x in self.chunks(3)]
        self.build(c)
        col = bi.get_active_collection()
        target = embed.build_doc_input(c[1]["text"])
        res = col.query(query_embeddings=[fake_vector(target)], n_results=1)
        self.assertEqual(res["ids"][0][0], c[1]["id"])


class TestLoadAndGone(IndexTestCase):
    HTML = ("<html><body><main><h1>עמוד</h1><h2>מקטע</h2><p>" + "מילה " * 80 + "</p></main></body></html>").encode("utf-8")

    def write_index(self, entries):
        raw = self.tmp / "raw"
        raw.mkdir(exist_ok=True)
        index = {}
        for url, (name, gone) in entries.items():
            if name:
                (raw / name).write_bytes(self.HTML)
            index[url] = {"file": name or "missing.html", "lastmod": None, **({"gone": True} if gone else {})}
        path = raw / "index.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        return path

    def test_gone_pages_are_skipped(self):
        path = self.write_index({"https://www.fnx.co.il/a/": ("a.html", False), "https://www.fnx.co.il/b/": ("b.html", True)})
        chunks, rep = bi.load_chunks(path)
        self.assertEqual({c["url"] for c in chunks}, {"https://www.fnx.co.il/a/"})
        self.assertEqual(rep["gone"], 1)

    def test_missing_file_reported_not_fatal(self):
        path = self.write_index({"https://www.fnx.co.il/a/": (None, False)})
        chunks, rep = bi.load_chunks(path)
        self.assertEqual((chunks, rep["missing_file"]), ([], 1))

    def test_mark_gone_flags_and_revives(self):
        index = {"u1": {"file": "1"}, "u2": {"file": "2"}, "u3": {"file": "3", "gone": True}}
        index, newly_gone, revived = fetch.mark_gone(index, {"u1", "u3"})
        self.assertEqual(newly_gone, ["u2"])
        self.assertEqual(revived, ["u3"])
        self.assertTrue(index["u2"]["gone"])
        self.assertNotIn("gone", index["u1"])
        self.assertNotIn("gone", index["u3"])
        self.assertEqual(index["u2"]["file"], "2")  # nothing else is touched

    def test_gone_page_absent_from_new_index_after_mark_and_rebuild(self):
        a, b = "https://www.fnx.co.il/a/", "https://www.fnx.co.il/b/"
        path = self.write_index({a: ("a.html", False), b: ("b.html", False)})
        chunks, _ = bi.load_chunks(path)
        bi.build([dict(c, dup_count=1, dup_urls="") for c in chunks], FakeEmbedder(), self.cache, bi.get_client(),
                 sleep=lambda s: None)
        self.assertEqual({m["url"] for m in bi.get_active_collection().get(include=["metadatas"])["metadatas"]}, {a, b})
        index, _, _ = fetch.mark_gone(json.loads(path.read_text(encoding="utf-8")), {a})
        path.write_text(json.dumps(index), encoding="utf-8")
        chunks, _ = bi.load_chunks(path)
        bi.build([dict(c, dup_count=1, dup_urls="") for c in chunks], FakeEmbedder(), self.cache, bi.get_client(),
                 sleep=lambda s: None)
        self.assertEqual({m["url"] for m in bi.get_active_collection().get(include=["metadatas"])["metadatas"]}, {a})


class TestNearDuplicates(IndexTestCase):
    def test_close_vectors_on_different_pages_reported_same_page_ignored(self):
        a = make_chunk("https://x/a/", "א", "גוף", n=0)
        b = make_chunk("https://x/b/", "ב", "גוף אחר", n=1)
        c = make_chunk("https://x/a/", "ג", "גוף שלישי", n=2)
        vecs = [[1, 0, 0], [0.999, 0.01, 0], [1, 0.001, 0]]
        pairs = bi.near_duplicate_report([a, b, c], vecs, threshold=0.01)
        self.assertEqual({(p[1]["id"], p[2]["id"]) for p in pairs}, {(a["id"], b["id"]), (b["id"], c["id"])})


if __name__ == "__main__":
    unittest.main()


class TestScope(unittest.TestCase):
    CH = [{"area": "claims", "product": "claims-customer-service"}, {"area": "customer-service", "product": "customer-service"},
          {"area": "product", "product": "car-insurance"}, {"area": "product", "product": "gemel"}]

    def test_wave_a_is_claims_and_customer_service(self):
        self.assertEqual([c["product"] for c in bi.select_chunks(self.CH, wave="A")],
                         ["claims-customer-service", "customer-service"])

    def test_wave_by_segments_and_product_filter(self):
        self.assertEqual([c["product"] for c in bi.select_chunks(self.CH, wave="B")], ["car-insurance"])
        self.assertEqual([c["product"] for c in bi.select_chunks(self.CH, products=["gemel"])], ["gemel"])
        self.assertEqual(len(bi.select_chunks(self.CH)), 4)

    def test_quota_plan_counts_only_uncached_distinct_texts(self):
        usage = embed.DailyUsage(Path(tempfile.mkdtemp()) / "u.json", limit=10, today="d")
        usage.set_used(8)
        inputs = ["א", "ב", "ב", "ג"]
        plan = bi.quota_plan(inputs, {embed.cache_key("א")}, usage)
        self.assertEqual((plan["texts"], plan["remaining_today"], plan["fits_today"]), (2, 2, True))
        self.assertFalse(bi.quota_plan(inputs, set(), usage)["fits_today"])


class TestCumulative(IndexTestCase):
    def chunks(self):
        return [{**make_chunk(f"https://www.fnx.co.il/{p}/", "נושא", f"גוף {p}", n=i), "product": p}
                for i, p in enumerate(["health-insurance", "car-insurance", "gemel", "car-insurance"])]

    def key(self, c):
        return embed.cache_key(embed.build_doc_input(c["text"]))

    def test_add_cached_keeps_selected_plus_everything_in_the_cache(self):
        cs = self.chunks()
        selected = bi.select_chunks(cs, products=["health-insurance"])
        out = bi.add_cached(cs, selected, {self.key(cs[2])})          # gemel was embedded earlier
        self.assertEqual([c["product"] for c in out], ["health-insurance", "gemel"])
        self.assertEqual([c["id"] for c in out], [cs[0]["id"], cs[2]["id"]])  # original order, same ids

    def test_partial_build_does_not_drop_what_an_earlier_build_embedded(self):
        cs = self.chunks()
        e = FakeEmbedder()
        client = bi.get_client(self.tmp / "chroma")
        first = bi.build(bi.select_chunks(cs, products=["gemel"]), e, self.cache, client)
        self.assertEqual(client.get_collection(first["collection"], embedding_function=None).count(), 1)
        cached = self.cache.present([self.key(c) for c in cs])
        wanted = bi.add_cached(cs, bi.select_chunks(cs, products=["health-insurance"]), cached)
        second = bi.build(wanted, e, self.cache, client)
        col = client.get_collection(second["collection"], embedding_function=None)
        self.assertEqual(sorted(m["product"] for m in col.get(include=["metadatas"])["metadatas"]),
                         ["gemel", "health-insurance"])
        self.assertEqual(second["embed"]["misses"], 1)   # only health-insurance was new; gemel came from the cache

    def test_product_table_counts_uncached_distinct_texts_in_the_requested_order(self):
        cs = self.chunks()
        rows = bi.product_table(cs, {self.key(cs[2])}, order=("gemel", "health-insurance"))
        self.assertEqual([(r["product"], r["to_embed"]) for r in rows[:2]], [("gemel", 0), ("health-insurance", 1)])
        self.assertEqual((rows[-1]["to_embed"], rows[-1]["chunks"]), (1, 2))  # two identical car-insurance texts = 1 to embed
