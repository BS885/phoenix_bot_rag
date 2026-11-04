"""Polite downloader: saves raw HTML bytes to disk and re-downloads only when lastmod changed.

Usage (hits the real site, run only with approval):
    python -m ingest.fetch --list              # discover + filter URLs, print counts, download nothing
    python -m ingest.fetch --sample 10         # download the 10 highest-priority pages
    python -m ingest.fetch --url <page-url>    # download one page
"""
import bootstrap  # noqa: F401  (must stay first: truststore)

import argparse
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

import requests

import config
from ingest import urls

log = logging.getLogger("fetch")
_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT


def get_with_retry(url: str) -> requests.Response:
    """GET with timeout and exponential backoff on transient errors (429/5xx/connection)."""
    last: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        try:
            r = _session.get(url, timeout=config.TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                last = requests.HTTPError(f"{r.status_code} for {url}")
            else:
                r.raise_for_status()  # 404/403 etc.: permanent, no retry
                return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        except requests.HTTPError as e:
            log.error("permanent failure %s: %s", url, e)
            raise
        if attempt < config.MAX_RETRIES - 1:
            time.sleep(config.REQUEST_DELAY_SEC * (2 ** attempt))
    log.error("gave up after %d attempts %s: %s", config.MAX_RETRIES, url, last)
    raise last  # type: ignore[misc]


def _filename(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"


def _load_index() -> dict:
    if config.FETCH_INDEX.exists():
        return json.loads(config.FETCH_INDEX.read_text(encoding="utf-8"))
    return {}


def _save_index(index: dict) -> None:
    config.FETCH_INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_gone(index: dict, active_urls: set[str]) -> tuple[dict, list[str], list[str]]:
    """Flag pages that are no longer in the sitemap (gone=true); un-flag pages that came back.

    The index builder skips gone pages. Nothing is deleted from disk or from the index file.
    Returns (index, newly_gone, revived).
    """
    newly_gone, revived = [], []
    for url, entry in index.items():
        if url in active_urls and entry.get("gone"):
            del entry["gone"]
            revived.append(url)
        elif url not in active_urls and not entry.get("gone"):
            entry["gone"] = True
            newly_gone.append(url)
    return index, newly_gone, revived


def discover_urls() -> list[dict]:
    """Read robots.txt and sitemap(s) from the site; return filtered, prioritized pages."""
    robots_text = get_with_retry(config.ROBOTS_URL).text
    robots = urls.make_robots(robots_text)
    queue = urls.sitemaps_from_robots(robots_text) or [config.FALLBACK_SITEMAP_URL]
    pages, done = [], set()
    while queue:
        sm = queue.pop(0)
        if sm in done:
            continue
        done.add(sm)
        time.sleep(config.REQUEST_DELAY_SEC)
        found, children = urls.parse_sitemap(get_with_retry(sm).text)
        pages += found
        queue += children
    return urls.select_urls(pages, robots)


def fetch_pages(pages: list[dict]) -> dict:
    """Download pages; skip those whose lastmod is unchanged. A failed page is logged and skipped."""
    config.RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_index()
    stats = {"downloaded": 0, "skipped": 0, "failed": 0}
    for page in pages:
        url, lastmod = page["url"], page.get("lastmod")
        entry = index.get(url)
        if entry and lastmod and entry.get("lastmod") == lastmod and (config.RAW_HTML_DIR / entry["file"]).exists():
            stats["skipped"] += 1
            continue
        try:
            r = get_with_retry(url)
            name = _filename(url)
            (config.RAW_HTML_DIR / name).write_bytes(r.content)  # raw bytes: encoding decided later
            index[url] = {
                "file": name,
                "lastmod": lastmod,
                "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "priority": page.get("priority"),
                "bytes": len(r.content),
            }
            _save_index(index)
            stats["downloaded"] += 1
        except Exception as e:  # log and continue the crawl
            stats["failed"] += 1  # already logged in get_with_retry (or a file error: log here)
            if not isinstance(e, requests.RequestException):
                log.error("failed %s: %s", url, e)
        time.sleep(config.REQUEST_DELAY_SEC)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--sample", type=int, nargs="?", const=config.SAMPLE_SIZE)
    g.add_argument("--url")
    g.add_argument("--mark-gone", action="store_true",
                   help="read the sitemap and flag saved pages that vanished (gone=true); downloads nothing")
    ap.add_argument("--match", help="keep only URLs containing this text (e.g. health)")
    args = ap.parse_args()

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=config.FETCH_LOG, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.url:
        url = urls.normalize_url(args.url)
        robots = urls.make_robots(get_with_retry(config.ROBOTS_URL).text)
        ok, reason = urls.fetchable(url, robots)
        if not ok:
            print(f"refused: {url} ({reason})")
            return
        print(fetch_pages([{"url": url, "lastmod": None, "priority": urls.priority(url)}]))
        return
    if args.mark_gone:
        active = {p["url"] for p in discover_urls()}
        index, newly_gone, revived = mark_gone(_load_index(), active)
        _save_index(index)
        print(f"newly gone: {len(newly_gone)}, revived: {len(revived)}")
        for u in newly_gone:
            print("  gone:", u)
        return
    pages = discover_urls()
    if args.match:
        pages = [p for p in pages if args.match in p["url"]]
    by_p: dict[int, int] = {}
    for p in pages:
        by_p[p["priority"]] = by_p.get(p["priority"], 0) + 1
    print(f"{len(pages)} URLs after filtering; by priority: {dict(sorted(by_p.items()))}")
    if args.sample:
        chosen = pages[: args.sample]
        for p in chosen:
            print(p["priority"], p["url"])
        print(fetch_pages(chosen))


if __name__ == "__main__":
    main()
