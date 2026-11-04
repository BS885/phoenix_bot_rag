"""Polite, resumable downloader: saves raw HTML bytes to disk and re-downloads only when lastmod changed.

Usage (hits the real site, run only with approval):
    python -m ingest.fetch --list                       # discover + filter URLs, print counts, download nothing
    python -m ingest.fetch --sample 10                  # download the 10 highest-priority pages
    python -m ingest.fetch --url <page-url>             # download one page (URL filter + robots.txt apply)
    python -m ingest.fetch --crawl --wave A --dry-run   # what a wave would do, no downloads
    python -m ingest.fetch --crawl --wave A             # crawl one wave (see config.WAVES)
    python -m ingest.fetch --crawl --wave A --retry-failed   # retry only the pages that failed before
    python -m ingest.fetch --mark-gone                  # flag saved pages that vanished from the sitemap

Safety: every outcome is written to index.json (atomically) after each page; the crawl stops by itself
on repeated 403/429, on a run of failures, or when too many pages fail.
"""
import bootstrap  # noqa: F401  (must stay first: truststore)

import argparse
import email.utils
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import config
from ingest import urls
from ingest.chunk import area_of

log = logging.getLogger("fetch")
_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT


class RetryAfterTooLong(requests.HTTPError):
    """The server asked us to wait longer than we are willing to: treated as a block."""


# ---------- one request ----------

def _retry_after_seconds(r) -> float | None:
    value = (r.headers or {}).get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:  # HTTP-date form
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def get_with_retry(url: str) -> requests.Response:
    """GET with timeout. 429/5xx/connection errors are retried with backoff; Retry-After is honored.

    404/403 etc. are permanent for this call (no retry). A Retry-After above config.MAX_RETRY_AFTER_SEC
    raises RetryAfterTooLong right away.
    """
    last: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        wait = config.REQUEST_DELAY_SEC * (2 ** attempt)
        try:
            r = _session.get(url, timeout=config.TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                last = requests.HTTPError(f"{r.status_code} for {url}", response=r)
                asked = _retry_after_seconds(r) if r.status_code in (429, 503) else None
                if asked is not None:
                    if asked > config.MAX_RETRY_AFTER_SEC:
                        log.error("Retry-After %ss is too long for %s", asked, url)
                        raise RetryAfterTooLong(f"{r.status_code} Retry-After {asked:.0f}s for {url}", response=r)
                    wait = max(asked, 0.0)
            else:
                r.raise_for_status()  # 404/403 etc.: permanent, no retry
                return r
        except RetryAfterTooLong:
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        except requests.HTTPError as e:
            log.error("permanent failure %s: %s", url, e)
            raise
        if attempt < config.MAX_RETRIES - 1:
            time.sleep(wait)
    log.error("gave up after %d attempts %s: %s", config.MAX_RETRIES, url, last)
    raise last  # type: ignore[misc]


def classify_error(exc: Exception) -> str:
    """'not_found' (permanent, never retried), 'blocked' (403/429: the server is refusing us), or 'failed'."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in (404, 410):
        return "not_found"
    if code in (403, 429) or isinstance(exc, RetryAfterTooLong):
        return "blocked"
    return "failed"


# ---------- index.json ----------

def _filename(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"


def _load_index() -> dict:
    if config.FETCH_INDEX.exists():
        return json.loads(config.FETCH_INDEX.read_text(encoding="utf-8"))
    return {}


def _save_index(index: dict) -> None:
    """Atomic: write a temp file next to the index, then replace. An interruption never leaves a half file."""
    tmp = config.FETCH_INDEX.with_name(config.FETCH_INDEX.name + ".tmp")
    try:
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, config.FETCH_INDEX)
    finally:
        if tmp.exists():
            tmp.unlink()


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


# ---------- discovery and selection ----------

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


def first_segment(url: str) -> str:
    parts = [s for s in urlparse(url).path.split("/") if s]
    return parts[0] if parts else ""


def select_pages(pages: list[dict], wave: str | None = None, areas=None, segments=None) -> list[dict]:
    """Keep pages that belong to a wave (config.WAVES) and/or to the given areas / first path segments."""
    areas, segments = set(areas or []), set(segments or [])
    if wave:
        spec = config.WAVES[wave]
        areas |= set(spec.get("areas", ()))
        segments |= set(spec.get("segments", ()))
    if not areas and not segments:
        return list(pages)
    return [p for p in pages if area_of(p["url"]) in areas or first_segment(p["url"]) in segments]


def plan(pages: list[dict], index: dict) -> dict:
    """What a run would do: split the pages into skip / retry / new, with a time estimate."""
    out = {"skip_ok": [], "skip_not_found": [], "retry_failed": [], "new_or_changed": []}
    for p in pages:
        e = index.get(p["url"])
        if e and p.get("lastmod") and e.get("lastmod") == p["lastmod"]:
            if e.get("status") == "not_found":
                out["skip_not_found"].append(p)
                continue
            if e.get("status", "ok") == "ok" and e.get("file") and (config.RAW_HTML_DIR / e["file"]).exists():
                out["skip_ok"].append(p)
                continue
        (out["retry_failed"] if e and e.get("status") == "failed" else out["new_or_changed"]).append(p)
    todo = len(out["retry_failed"]) + len(out["new_or_changed"])
    out["to_download"] = todo
    out["estimate_minutes"] = todo * config.SECONDS_PER_PAGE_ESTIMATE / 60
    return out


# ---------- the crawl ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _msg(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def fetch_pages(pages: list[dict]) -> dict:
    """Download pages, one by one, writing every outcome to index.json.

    Skipped: unchanged pages already downloaded, and unchanged pages known to be 404.
    404/410 -> status 'not_found' (never retried until lastmod changes). Other errors -> 'failed'
    (retried on the next run). Stops by itself on blocking or too many failures.
    Returns {"downloaded": [...], "skipped": [...], "not_found": [...], "failed": [...], "blocked": [...],
             "stopped": reason or None, "seconds": float}; each list holds URLs (failed/blocked: (url, error)).
    """
    config.RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    index = _load_index()
    res = {"downloaded": [], "skipped": [], "not_found": [], "failed": [], "blocked": [], "stopped": None}
    started = time.monotonic()
    attempts = hard_failures = consecutive = block_signals = 0

    for page in pages:
        url, lastmod = page["url"], page.get("lastmod")
        entry = index.get(url)
        if entry and lastmod and entry.get("lastmod") == lastmod:
            if entry.get("status") == "not_found":
                res["skipped"].append(url)
                continue
            if (entry.get("status", "ok") == "ok" and entry.get("file")
                    and (config.RAW_HTML_DIR / entry["file"]).exists()):
                res["skipped"].append(url)
                continue
        try:
            r = get_with_retry(url)
            name = _filename(url)
            (config.RAW_HTML_DIR / name).write_bytes(r.content)  # raw bytes: encoding decided later
            index[url] = {"file": name, "lastmod": lastmod, "collected_at": _now(), "status": "ok",
                          "priority": page.get("priority"), "bytes": len(r.content)}
            res["downloaded"].append(url)
            attempts += 1
            consecutive = 0
        except Exception as e:  # noqa: BLE001  record and continue: one bad page never stops the crawl
            kind, msg, prev = classify_error(e), _msg(e), index.get(url, {})
            if kind == "not_found":
                index[url] = {**prev, "status": "not_found", "lastmod": lastmod, "error": msg, "failed_at": _now()}
                if prev.get("file"):
                    index[url]["gone"] = True  # it was downloaded once and is gone now
                res["not_found"].append(url)
                log.info("not found %s", url)
            else:
                had_file = bool(prev.get("file"))
                index[url] = {**prev, "last_error": msg, "failed_at": _now()}
                if not had_file:  # a failed refresh of a good page keeps its content and status
                    index[url].update({"status": "failed", "lastmod": lastmod})
                res["blocked" if kind == "blocked" else "failed"].append((url, msg))
                if not isinstance(e, requests.RequestException):
                    log.error("failed %s: %s", url, msg)
                attempts += 1
                hard_failures += 1
                consecutive += 1
                block_signals += kind == "blocked"
                reason = None
                if block_signals >= config.CRAWL_MAX_BLOCK_SIGNALS:
                    reason = f"the server refused us {block_signals} times (403/429 or a long Retry-After)"
                elif consecutive >= config.CRAWL_MAX_CONSECUTIVE_FAILURES:
                    reason = f"{consecutive} failures in a row"
                elif (attempts >= config.CRAWL_MIN_ATTEMPTS_FOR_RATE
                      and hard_failures / attempts > config.CRAWL_MAX_FAIL_RATE):
                    reason = f"{hard_failures} of {attempts} requests failed (> {config.CRAWL_MAX_FAIL_RATE:.0%})"
                if reason:
                    res["stopped"] = reason
                    log.error("crawl stopped: %s", reason)
                    _save_index(index)
                    break
        _save_index(index)  # after every page: an interruption loses nothing
        time.sleep(config.REQUEST_DELAY_SEC)
    res["seconds"] = time.monotonic() - started
    return res


def write_report(res: dict, pages: list[dict], title: str = "Crawl report") -> str:
    """logs/crawl_report.md: outcome per area and the list of problem pages."""
    by_area: dict[str, dict[str, int]] = {}
    status_of = {}
    for u in res["downloaded"]:
        status_of[u] = "downloaded"
    for u in res["skipped"]:
        status_of[u] = "skipped"
    for u in res["not_found"]:
        status_of[u] = "not_found"
    for u, _ in res["failed"]:
        status_of[u] = "failed"
    for u, _ in res["blocked"]:
        status_of[u] = "blocked"
    cols = ["downloaded", "skipped", "not_found", "failed", "blocked", "not_attempted"]
    for p in pages:
        row = by_area.setdefault(area_of(p["url"]), dict.fromkeys(cols, 0))
        row[status_of.get(p["url"], "not_attempted")] += 1
    lines = [f"# {title}", "", f"{len(pages)} pages selected; {res.get('seconds', 0) / 60:.1f} minutes.", ""]
    if res.get("stopped"):
        lines += [f"**STOPPED:** {res['stopped']}", ""]
    lines += ["| area | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for area, row in sorted(by_area.items()):
        lines.append(f"| {area} | " + " | ".join(str(row[c]) for c in cols) + " |")
    if res["failed"] or res["blocked"]:
        lines += ["", "## Failed (retry with --retry-failed)", ""]
        lines += [f"- {u} | {m}" for u, m in res["failed"] + res["blocked"]]
    if res["not_found"]:
        hebrew = [u for u in res["not_found"] if any("֐" <= ch <= "׿" for ch in u)]
        lines += ["", f"## Not found (404): {len(res['not_found'])}, with a Hebrew slug: {len(hebrew)}", ""]
        lines += [f"- {u}" for u in res["not_found"]]
    text = "\n".join(lines) + "\n"
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    config.CRAWL_REPORT.write_text(text, encoding="utf-8")
    return text


def counts(res: dict) -> dict:
    """Short numeric summary of a fetch_pages result."""
    out = {k: len(res[k]) for k in ("downloaded", "skipped", "not_found", "failed", "blocked")}
    out["stopped"] = res.get("stopped")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--sample", type=int, nargs="?", const=config.SAMPLE_SIZE)
    g.add_argument("--url")
    g.add_argument("--crawl", action="store_true", help="download the selected pages (use --wave/--area/--segment)")
    g.add_argument("--mark-gone", action="store_true",
                   help="read the sitemap and flag saved pages that vanished (gone=true); downloads nothing")
    ap.add_argument("--match", help="keep only URLs containing this text (e.g. health)")
    ap.add_argument("--wave", choices=sorted(config.WAVES), help="A, B or C, as defined in config.WAVES")
    ap.add_argument("--area", action="append", help="claims | customer-service | product (repeatable)")
    ap.add_argument("--segment", action="append", help="first path segment, e.g. gemel (repeatable)")
    ap.add_argument("--limit", type=int, help="at most N pages")
    ap.add_argument("--dry-run", action="store_true", help="with --crawl: show the plan, download nothing")
    ap.add_argument("--retry-failed", action="store_true", help="with --crawl: only pages that failed before")
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
        print(counts(fetch_pages([{"url": url, "lastmod": None, "priority": urls.priority(url)}])))
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
    if args.crawl:
        pages = select_pages(pages, args.wave, args.area, args.segment)
        index = _load_index()
        if args.retry_failed:
            pages = [p for p in pages if index.get(p["url"], {}).get("status") == "failed"]
        if args.limit:
            pages = pages[: args.limit]
        p = plan(pages, index)
        print(f"{len(pages)} pages selected: {len(p['skip_ok'])} already downloaded, "
              f"{len(p['skip_not_found'])} known 404, {len(p['retry_failed'])} to retry, "
              f"{len(p['new_or_changed'])} new or changed; ~{p['estimate_minutes']:.0f} minutes")
        if args.dry_run:
            return
        res = fetch_pages(pages)
        write_report(res, pages, f"Crawl report: wave {args.wave or '-'}")
        print(counts(res), f"| report: {config.CRAWL_REPORT}")
        return
    by_p: dict[int, int] = {}
    for p in pages:
        by_p[p["priority"]] = by_p.get(p["priority"], 0) + 1
    print(f"{len(pages)} URLs after filtering; by priority: {dict(sorted(by_p.items()))}")
    if args.sample:
        chosen = pages[: args.sample]
        for p in chosen:
            print(p["priority"], p["url"])
        print(counts(fetch_pages(chosen)))


if __name__ == "__main__":
    main()
