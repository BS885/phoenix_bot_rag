import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

import config
from index import build_index as bi
from ingest import fetch

HTML = b"<html><body><main><h1>x</h1></main></body></html>"


class Resp:
    def __init__(self, status=200, content=HTML, headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


def pages(n, prefix="p", lastmod="2026-01-01"):
    return [{"url": f"https://www.fnx.co.il/{prefix}{i}/", "lastmod": lastmod, "priority": 1} for i in range(n)]


class CrawlCase(unittest.TestCase):
    """Temp directories, a scripted fake server, and a recorder instead of time.sleep."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for name, value in {"RAW_HTML_DIR": tmp / "raw", "FETCH_INDEX": tmp / "raw" / "index.json",
                            "LOG_DIR": tmp / "logs", "CRAWL_REPORT": tmp / "logs" / "crawl_report.md",
                            "MAX_RETRIES": 1}.items():
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tmp
        self.sleeps, self.calls, self.script = [], [], {}
        for target, kwargs in ((fetch.time, {"sleep": mock.Mock(side_effect=self.sleeps.append)}),):
            for attr, m in kwargs.items():
                p = mock.patch.object(target, attr, m)
                p.start()
                self.addCleanup(p.stop)
        p = mock.patch.object(fetch._session, "get", side_effect=self.fake_get)
        p.start()
        self.addCleanup(p.stop)

    def fake_get(self, url, timeout=None):
        self.calls.append(url)
        queue = self.script.get(url)
        if queue:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            return item if isinstance(item, Resp) else Resp(item)
        return Resp(200)

    def index(self):
        return json.loads(config.FETCH_INDEX.read_text(encoding="utf-8"))


class TestAtomicIndex(CrawlCase):
    def test_failed_replace_keeps_the_old_index_and_leaves_no_temp_file(self):
        config.FETCH_INDEX.parent.mkdir(parents=True, exist_ok=True)
        fetch._save_index({"old": {"file": "a.html"}})
        with mock.patch.object(fetch.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                fetch._save_index({"new": {}})
        self.assertEqual(self.index(), {"old": {"file": "a.html"}})
        self.assertEqual([p.name for p in config.FETCH_INDEX.parent.iterdir()], ["index.json"])

    def test_index_is_written_after_every_page(self):
        seen = []
        real = fetch._save_index
        with mock.patch.object(fetch, "_save_index", side_effect=lambda i: (seen.append(len(i)), real(i))):
            fetch.fetch_pages(pages(3))
        self.assertEqual(seen, [1, 2, 3])


class TestOutcomesAreRecorded(CrawlCase):
    def test_success_is_recorded_as_ok(self):
        fetch.fetch_pages(pages(1))
        e = next(iter(self.index().values()))
        self.assertEqual((e["status"], e["bytes"]), ("ok", len(HTML)))

    def test_404_recorded_as_not_found_and_not_retried(self):
        p = pages(1)
        self.script[p[0]["url"]] = [404]
        r1 = fetch.fetch_pages(p)
        self.assertEqual(len(r1["not_found"]), 1)
        self.assertEqual(self.index()[p[0]["url"]]["status"], "not_found")
        n = len(self.calls)
        r2 = fetch.fetch_pages(p)
        self.assertEqual((len(self.calls), len(r2["skipped"])), (n, 1))  # same lastmod: no new request

    def test_404_is_retried_when_lastmod_changes(self):
        p = pages(1)
        self.script[p[0]["url"]] = [404, 200]
        fetch.fetch_pages(p)
        changed = pages(1, lastmod="2026-06-01")
        r = fetch.fetch_pages(changed)
        self.assertEqual(len(r["downloaded"]), 1)
        self.assertEqual(self.index()[p[0]["url"]]["status"], "ok")

    def test_server_error_recorded_as_failed_and_retried_next_run(self):
        p = pages(3)
        self.script[p[1]["url"]] = [500, 200]
        r1 = fetch.fetch_pages(p)
        self.assertEqual([u for u, _ in r1["failed"]], [p[1]["url"]])
        e = self.index()[p[1]["url"]]
        self.assertEqual(e["status"], "failed")
        self.assertIn("500", e["last_error"])
        self.calls.clear()
        r2 = fetch.fetch_pages(p)
        self.assertEqual(self.calls, [p[1]["url"]])  # only the failed page is requested again
        self.assertEqual(len(r2["downloaded"]), 1)
        self.assertEqual(self.index()[p[1]["url"]]["status"], "ok")

    def test_failed_refresh_keeps_the_old_content(self):
        p = pages(1)
        fetch.fetch_pages(p)
        before = self.index()[p[0]["url"]]
        self.script[p[0]["url"]] = [500]
        fetch.fetch_pages(pages(1, lastmod="2026-09-09"))
        after = self.index()[p[0]["url"]]
        self.assertEqual((after["status"], after["file"], after["lastmod"]), ("ok", before["file"], before["lastmod"]))
        self.assertIn("last_error", after)

    def test_404_on_a_previously_downloaded_page_marks_it_gone(self):
        p = pages(1)
        fetch.fetch_pages(p)
        self.script[p[0]["url"]] = [404]
        fetch.fetch_pages(pages(1, lastmod="2026-09-09"))
        e = self.index()[p[0]["url"]]
        self.assertTrue(e["gone"])
        self.assertEqual(e["status"], "not_found")


class TestRetryAfterAndBlocking(CrawlCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(config, "MAX_RETRIES", 3).start()
        self.addCleanup(mock.patch.stopall)

    def test_retry_after_seconds_is_honored(self):
        url = "https://www.fnx.co.il/x/"
        self.script[url] = [Resp(429, headers={"Retry-After": "7"}), Resp(200)]
        r = fetch.get_with_retry(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sleeps, [7.0])  # the server's number, not our own backoff

    def test_backoff_used_without_retry_after(self):
        url = "https://www.fnx.co.il/x/"
        self.script[url] = [503, 200]
        fetch.get_with_retry(url)
        self.assertEqual(self.sleeps, [config.REQUEST_DELAY_SEC])

    def test_retry_after_longer_than_the_cap_is_a_block(self):
        url = "https://www.fnx.co.il/x/"
        self.script[url] = [Resp(429, headers={"Retry-After": "999"})]
        with self.assertRaises(fetch.RetryAfterTooLong) as ctx:
            fetch.get_with_retry(url)
        self.assertEqual(fetch.classify_error(ctx.exception), "blocked")
        self.assertEqual(self.sleeps, [])  # did not wait at all

    def test_persistent_429_is_classified_as_blocked(self):
        url = "https://www.fnx.co.il/x/"
        self.script[url] = [429]
        with self.assertRaises(requests.HTTPError) as ctx:
            fetch.get_with_retry(url)
        self.assertEqual(fetch.classify_error(ctx.exception), "blocked")

    def test_403_three_times_stops_the_crawl(self):
        p = pages(10)
        for page in p:
            self.script[page["url"]] = [403]
        res = fetch.fetch_pages(p)
        self.assertIn("refused", res["stopped"])
        self.assertEqual(len(res["blocked"]), config.CRAWL_MAX_BLOCK_SIGNALS)
        self.assertEqual(len(self.calls), config.CRAWL_MAX_BLOCK_SIGNALS)  # the other pages were never requested
        self.assertEqual(len(self.index()), config.CRAWL_MAX_BLOCK_SIGNALS)


class TestStopConditions(CrawlCase):
    def test_ten_failures_in_a_row_stop_the_crawl(self):
        p = pages(30)
        for page in p:
            self.script[page["url"]] = [500]
        res = fetch.fetch_pages(p)
        self.assertIn("in a row", res["stopped"])
        self.assertEqual(len(res["failed"]), config.CRAWL_MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(len(self.index()), config.CRAWL_MAX_CONSECUTIVE_FAILURES)

    def test_a_success_resets_the_consecutive_counter(self):
        p = pages(40)
        for i, page in enumerate(p):
            if i % 5 != 4:  # 4 failures, 1 success, repeat: never 10 in a row, 80% fail rate
                self.script[page["url"]] = [500]
        with mock.patch.object(config, "CRAWL_MAX_FAIL_RATE", 1.0):
            res = fetch.fetch_pages(p)
        self.assertIsNone(res["stopped"])

    def test_high_failure_rate_stops_after_enough_attempts(self):
        p = pages(60)
        for i, page in enumerate(p):
            if i % 3 == 0:  # every third page fails: 33% > 25%, never consecutive
                self.script[page["url"]] = [500]
        res = fetch.fetch_pages(p)
        self.assertIn("requests failed", res["stopped"])
        self.assertEqual(len(self.calls), config.CRAWL_MIN_ATTEMPTS_FOR_RATE)

    def test_404s_never_stop_the_crawl(self):
        p = pages(60)
        for page in p:
            self.script[page["url"]] = [404]
        res = fetch.fetch_pages(p)
        self.assertIsNone(res["stopped"])
        self.assertEqual(len(res["not_found"]), 60)

    def test_interrupted_crawl_resumes_where_it_stopped(self):
        p = pages(5)
        for page in p[2:]:
            self.script[page["url"]] = [500]
        with mock.patch.object(config, "CRAWL_MAX_CONSECUTIVE_FAILURES", 2):
            r1 = fetch.fetch_pages(p)
        self.assertIsNotNone(r1["stopped"])
        self.assertEqual(set(self.index()), {p[0]["url"], p[1]["url"], p[2]["url"], p[3]["url"]})
        self.script.clear()
        self.calls.clear()
        r2 = fetch.fetch_pages(p)
        self.assertEqual(sorted(self.calls), sorted(page["url"] for page in p[2:]))  # not the 2 good ones
        self.assertEqual(len(r2["downloaded"]), 3)


class TestPlanSelectionAndReport(CrawlCase):
    def test_wave_selection(self):
        sample = [{"url": "https://www.fnx.co.il/claims-customer-service/a/"},
                  {"url": "https://www.fnx.co.il/customer-service/b/"},
                  {"url": "https://www.fnx.co.il/car-insurance/c/"},
                  {"url": "https://www.fnx.co.il/gemel/d/"},
                  {"url": "https://www.fnx.co.il/fnx-insurance-ltd/e/"}]
        names = lambda ps: [fetch.first_segment(p["url"]) for p in ps]  # noqa: E731
        self.assertEqual(names(fetch.select_pages(sample, "A")), ["claims-customer-service", "customer-service"])
        self.assertEqual(names(fetch.select_pages(sample, "B")), ["car-insurance"])
        self.assertEqual(names(fetch.select_pages(sample, "C")), ["gemel"])
        self.assertEqual(len(fetch.select_pages(sample)), 5)
        self.assertEqual(names(fetch.select_pages(sample, segments=["gemel"])), ["gemel"])

    def test_wave_d_is_not_defined(self):
        self.assertEqual(sorted(config.WAVES), ["A", "B", "C"])

    def test_plan_splits_skip_retry_and_new(self):
        p = pages(4)
        self.script[p[1]["url"]] = [500]
        self.script[p[2]["url"]] = [404]
        fetch.fetch_pages(p[:3])
        plan = fetch.plan(p, fetch._load_index())
        self.assertEqual((len(plan["skip_ok"]), len(plan["skip_not_found"]), len(plan["retry_failed"]),
                          len(plan["new_or_changed"])), (1, 1, 1, 1))
        self.assertAlmostEqual(plan["estimate_minutes"], 2 * config.SECONDS_PER_PAGE_ESTIMATE / 60)

    def test_report_counts_per_area_and_lists_problems(self):
        p = [{"url": "https://www.fnx.co.il/claims-customer-service/a/", "lastmod": "x"},
             {"url": "https://www.fnx.co.il/claims-customer-service/b/", "lastmod": "x"},
             {"url": "https://www.fnx.co.il/claims-customer-service/אישור-לניתוח/", "lastmod": "x"},
             {"url": "https://www.fnx.co.il/car-insurance/c/", "lastmod": "x"}]
        self.script[p[1]["url"]] = [500]
        self.script[p[2]["url"]] = [404]
        res = fetch.fetch_pages(p[:3])  # the 4th page is never attempted
        text = fetch.write_report(res, p)
        self.assertTrue(config.CRAWL_REPORT.exists())
        self.assertIn("| claims |", text)
        self.assertIn("| product |", text)
        self.assertIn(p[1]["url"], text)           # listed under Failed
        self.assertIn("with a Hebrew slug: 1", text)
        row = [l for l in text.splitlines() if l.startswith("| product |")][0]
        self.assertTrue(row.rstrip(" |").endswith("1"))  # not_attempted = 1


class TestCommandLine(CrawlCase):
    def run_cli(self, *argv, discovered=None, index=None):
        if index is not None:
            config.FETCH_INDEX.parent.mkdir(parents=True, exist_ok=True)
            config.FETCH_INDEX.write_text(json.dumps(index), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(fetch, "discover_urls", return_value=discovered or pages(3)), \
                mock.patch.object(fetch, "fetch_pages", return_value={"downloaded": [], "skipped": [], "not_found": [],
                                                                     "failed": [], "blocked": [], "stopped": None,
                                                                     "seconds": 0}) as fp, \
                mock.patch.object(fetch, "write_report"), \
                mock.patch.object(fetch.logging, "basicConfig"), \
                mock.patch.object(sys, "argv", ["fetch", *argv]), redirect_stdout(buf):
            fetch.main()
        return buf.getvalue(), fp

    def test_dry_run_downloads_nothing_and_prints_the_plan(self):
        out, fp = self.run_cli("--crawl", "--segment", "p0", "--dry-run", discovered=[
            {"url": "https://www.fnx.co.il/p0/a/", "lastmod": "x", "priority": 1}])
        fp.assert_not_called()
        self.assertIn("1 pages selected", out)
        self.assertIn("minutes", out)

    def test_retry_failed_selects_only_failed_pages(self):
        found = pages(3)
        index = {found[1]["url"]: {"status": "failed", "lastmod": "2026-01-01"},
                 found[0]["url"]: {"status": "ok", "file": "a.html", "lastmod": "2026-01-01"}}
        _, fp = self.run_cli("--crawl", "--segment", "p1", "--segment", "p0", "--segment", "p2",
                             "--retry-failed", discovered=found, index=index)
        sent = fp.call_args[0][0]
        self.assertEqual([p["url"] for p in sent], [found[1]["url"]])

    def test_limit(self):
        _, fp = self.run_cli("--crawl", "--segment", "p0", "--segment", "p1", "--segment", "p2", "--limit", "2")
        self.assertEqual(len(fp.call_args[0][0]), 2)


class TestIndexBuilderIgnoresBrokenEntries(CrawlCase):
    def test_failed_and_not_found_entries_without_a_page_are_skipped(self):
        config.RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
        (config.RAW_HTML_DIR / "a.html").write_bytes(
            ("<html><body><main><h1>עמוד</h1><h2>מקטע</h2><p>" + "מילה " * 80 + "</p></main></body></html>").encode())
        index = {"https://www.fnx.co.il/ok/": {"file": "a.html", "status": "ok"},
                 "https://www.fnx.co.il/old-format/": {"file": "a.html"},  # entries written before 'status' existed
                 "https://www.fnx.co.il/failed/": {"status": "failed", "last_error": "x"},
                 "https://www.fnx.co.il/404/": {"status": "not_found"}}
        config.FETCH_INDEX.write_text(json.dumps(index), encoding="utf-8")
        chunks, rep = bi.load_chunks(config.FETCH_INDEX)
        self.assertEqual({c["url"] for c in chunks}, {"https://www.fnx.co.il/ok/", "https://www.fnx.co.il/old-format/"})
        self.assertEqual(rep["no_content"], 2)

    def test_pages_saved_before_they_were_excluded_are_not_indexed(self):
        config.RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
        (config.RAW_HTML_DIR / "a.html").write_bytes(
            ("<html><body><main><h1>עמוד</h1><h2>מקטע</h2><p>" + "מילה " * 80 + "</p></main></body></html>").encode())
        index = {"https://www.fnx.co.il/customer-service/a/": {"file": "a.html", "status": "ok"},
                 "https://www.fnx.co.il/claims-customer-service/class-actions/x/": {"file": "a.html", "status": "ok"},
                 "https://www.fnx.co.il/customer-service/terms-of-use/y/": {"file": "a.html", "status": "ok"}}
        config.FETCH_INDEX.write_text(json.dumps(index), encoding="utf-8")
        chunks, rep = bi.load_chunks(config.FETCH_INDEX)
        self.assertEqual({c["url"] for c in chunks}, {"https://www.fnx.co.il/customer-service/a/"})
        self.assertEqual(rep["out_of_scope"], 2)
        self.assertTrue((config.RAW_HTML_DIR / "a.html").exists())  # nothing is deleted


if __name__ == "__main__":
    unittest.main()
