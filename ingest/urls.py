"""Sitemap parsing, URL filtering, robots.txt and priorities. Pure functions: no network here."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import config


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(xml_text: str) -> tuple[list[dict], list[str]]:
    """Return (pages, child_sitemaps). pages: [{"url", "lastmod"}]."""
    root = ET.fromstring(xml_text)
    pages, children = [], []
    for node in root:
        fields = {_local(c.tag): (c.text or "").strip() for c in node}
        if not fields.get("loc"):
            continue
        if _local(root.tag) == "sitemapindex":
            children.append(fields["loc"])
        else:
            pages.append({"url": fields["loc"], "lastmod": fields.get("lastmod") or None})
    return pages, children


def sitemaps_from_robots(robots_text: str) -> list[str]:
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_text)


def is_allowed_url(url: str) -> bool:
    p = urlparse(url)
    if p.hostname != config.ALLOWED_HOST:
        return False
    path = p.path if p.path.endswith("/") else p.path + "/"
    if any(path.startswith(x) for x in config.EXCLUDED_PATH_PREFIXES):
        return False
    if set(parse_qs(p.query)) & set(config.EXCLUDED_QUERY_PARAMS):
        return False
    segments = [s for s in p.path.split("/") if s]
    if segments and segments[-1].isdigit():  # single fund pages end with a number
        return False
    if segments and any(segments[-1].endswith(s) for s in config.EXCLUDED_PATH_SUFFIXES):
        return False
    return True


def priority(url: str) -> int:
    path = urlparse(url).path
    for level, prefixes in sorted(config.PRIORITY_PREFIXES.items()):
        if any(path.startswith(x) for x in prefixes):
            return level
    return config.DEFAULT_PRIORITY


def normalize_url(url: str) -> str:
    """Drop the #fragment: it is client-side only and never part of the request."""
    return urldefrag(url.strip())[0]


def fetchable(url: str, robots: RobotFileParser | None) -> tuple[bool, str]:
    """Same gate as the crawl: URL filter first, then robots.txt. Returns (ok, reason)."""
    if not is_allowed_url(url):
        return False, "excluded by the URL filter"
    if robots is not None and not robots.can_fetch(config.USER_AGENT, url):
        return False, "disallowed by robots.txt"
    return True, ""


def make_robots(robots_text: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    return rp


def select_urls(pages: list[dict], robots: RobotFileParser | None = None) -> list[dict]:
    """Filter, dedupe, check robots, attach priority, sort by priority then URL."""
    seen, out = set(), []
    for page in pages:
        url = page["url"]
        if url in seen or not is_allowed_url(url):
            continue
        if robots is not None and not robots.can_fetch(config.USER_AGENT, url):
            continue
        seen.add(url)
        out.append({**page, "priority": priority(url)})
    return sorted(out, key=lambda x: (x["priority"], x["url"]))
