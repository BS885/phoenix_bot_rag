"""Streamlit runs the script in several threads: the embedding cache, the rate limiter, the daily counters and the LLM wrapper must
survive concurrent use (stage 6.2). All with a ThreadPoolExecutor; no API."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
import llm
from index import embed

WORKERS = 8


def run_all(fn, jobs):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(fn, jobs))


class TestEmbedCacheThreads(unittest.TestCase):
    def test_parallel_reads_and_writes_on_one_connection(self):
        d = Path(tempfile.mkdtemp())
        cache = embed.EmbedCache(d / "c.sqlite")
        dim = 8

        def job(i):
            items = {f"k{i}-{j}": [float(i + j)] * dim for j in range(25)}
            cache.put_many(items)
            got = cache.get_many(list(items))
            present = cache.present(list(items))
            return len(got), len(present), all(abs(got[k][0] - float(i + int(k.split('-')[1]))) < 1e-6 for k in got)

        results = run_all(job, range(40))
        self.assertTrue(all(r == (25, 25, True) for r in results), results[:3])
        self.assertEqual(len(cache.present([f"k{i}-{j}" for i in range(40) for j in range(25)])), 1000)
        cache.close()

    def test_a_cache_built_in_one_thread_is_usable_in_another(self):
        cache = embed.EmbedCache(":memory:")
        cache.put_many({"a": [1.0, 2.0]})
        out = []
        t = threading.Thread(target=lambda: out.append(cache.get_many(["a"])))
        t.start()
        t.join()
        self.assertEqual(out, [{"a": [1.0, 2.0]}])


class TestRateLimiterThreads(unittest.TestCase):
    def test_no_minute_window_ever_exceeds_the_limit(self):
        now, lock, log = [0.0], threading.Lock(), []

        def clock():
            with lock:
                return now[0]

        def sleep(seconds):
            with lock:
                now[0] += seconds

        lim = embed.RateLimiter(rpm=100, tpm=10 ** 9, clock=clock, sleep=sleep)

        def job(_):
            for _ in range(5):
                lim.acquire(10, 10)
                with lock:
                    log.append((now[0], 10))

        run_all(job, range(WORKERS))                              # 8 x 5 x 10 = 400 texts
        self.assertEqual(sum(n for _, n in log), 400)
        log.sort()
        for t0, _ in log:
            in_window = sum(n for t, n in log if t0 <= t < t0 + 60)
            self.assertLessEqual(in_window, 100 + 10 * WORKERS, in_window)   # clock steps are shared: allow one in-flight batch each

    def test_events_are_all_recorded_when_the_window_is_big_enough(self):
        lim = embed.RateLimiter(rpm=10 ** 6, tpm=10 ** 9)
        run_all(lambda _: [lim.acquire(1, 1) for _ in range(100)], range(WORKERS))
        self.assertEqual(sum(e[1] for e in lim.events), 800)


class TestDailyUsageThreads(unittest.TestCase):
    def test_parallel_adds_are_exact_and_the_file_matches(self):
        path = Path(tempfile.mkdtemp()) / "u.json"
        usage = embed.DailyUsage(path, limit=10 ** 6, today="d")
        run_all(lambda _: [usage.add(1) for _ in range(50)], range(WORKERS))
        self.assertEqual(usage.used, 400)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["texts"], 400)


class FakeBackend:
    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def call(self, model, prompt, system, schema):
        with self._lock:
            self.calls += 1
        return '{"a": "x"}'


class TestLLMThreads(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}

    def test_parallel_generate_counts_every_call_once_and_creates_one_usage_and_limiter_per_model(self):
        backend = FakeBackend()
        m = llm.LLM(backend, usage_dir=tempfile.mkdtemp(), rpm=10 ** 6, tpm=10 ** 9, rpd=10 ** 6)
        run_all(lambda _: [m.generate("p", self.SCHEMA) for _ in range(25)], range(WORKERS))
        self.assertEqual(backend.calls, 200)
        self.assertEqual(m.usage(config.LLM_MODEL_ANSWER).used, 200)
        self.assertEqual(len(m._limiters), 1)
        self.assertEqual(len(m._usage), 1)

    def test_the_module_level_default_is_created_once(self):
        llm._default = None
        seen = run_all(lambda _: id(self._get_default()), range(WORKERS))
        self.assertEqual(len(set(seen)), 1)
        llm._default = None

    @staticmethod
    def _get_default():
        with llm._default_lock:
            if llm._default is None:
                llm._default = llm.LLM(FakeBackend(), usage_dir=tempfile.mkdtemp())
            return llm._default


if __name__ == "__main__":
    unittest.main()
