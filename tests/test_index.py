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

    def test_retry_backoff_on_429_then_success(self):
        class Err(Exception):
            code = 429
        calls, waits = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise Err("RESOURCE_EXHAUSTED")
            return "ok"
        self.assertEqual(embed.with_retries(flaky, sleep=waits.append), "ok")
        self.assertEqual(waits, [config.EMBED_BACKOFF_SEC, config.EMBED_BACKOFF_SEC * 2])

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
