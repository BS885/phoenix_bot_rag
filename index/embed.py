"""Embedding layer: Gemini client, sqlite cache, batching, retries and token warnings.

The embedder is injected (anything with .embed(list[str]) -> list[list[float]]), so everything
here is testable without the network.
"""
import hashlib
import json
import math
import os
import sqlite3
import struct
import time
from pathlib import Path

import config


class EmbeddingError(Exception):
    pass


class QuotaStop(EmbeddingError):
    """The quota is used up (daily) or keeps failing (per-minute): stop in an orderly way, the cache keeps the rest."""

    def __init__(self, message: str, daily: bool):
        super().__init__(message)
        self.daily = daily
        self.done = 0        # filled in by embed_inputs: texts embedded in this run
        self.remaining = 0   # texts still to embed


# ---------- text to embed ----------

def split_head(chunk_text: str) -> tuple[str, str]:
    """A chunk's text is 'page > section' on the first line, then the body."""
    head, _, body = chunk_text.partition("\n")
    return head, body


def build_doc_input(chunk_text: str) -> str:
    head, body = split_head(chunk_text)
    return config.EMBED_DOC_TEMPLATE.format(title=head or "none", body=body)


def build_query_input(query: str) -> str:
    return config.EMBED_QUERY_TEMPLATE.format(query=query.strip())


def estimate_tokens(text: str) -> int:
    """Conservative estimate (the API has no documented token counter for this model)."""
    return math.ceil(len(text) / config.EMBED_CHARS_PER_TOKEN)


def token_warnings(inputs: list[str]) -> list[tuple[int, int]]:
    """(index, estimated tokens) for every input near or above the model limit (it truncates silently)."""
    limit = config.EMBED_MODEL_MAX_TOKENS * config.EMBED_TOKEN_WARN_RATIO
    return [(i, t) for i, text in enumerate(inputs) if (t := estimate_tokens(text)) > limit]


# ---------- cache ----------

def cache_key(text: str) -> str:
    raw = f"{config.EMBED_MODEL}|{config.EMBED_DIM}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_float32(vec: list[float]) -> list[float]:
    return list(struct.unpack(f"{len(vec)}f", struct.pack(f"{len(vec)}f", *vec)))


class EmbedCache:
    """sqlite: key -> float32 vector. The key covers model, dimension and the final input text,
    so changing any of them (or the template) simply misses the cache."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS emb (key TEXT PRIMARY KEY, dim INTEGER, vec BLOB, created_at TEXT)")
        self.db.commit()

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        out = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            marks = ",".join("?" * len(part))
            for key, dim, blob in self.db.execute(f"SELECT key, dim, vec FROM emb WHERE key IN ({marks})", part):
                out[key] = list(struct.unpack(f"{dim}f", blob))
        return out

    def present(self, keys: list[str]) -> set[str]:
        """Which of these keys are cached (no vectors loaded)."""
        out: set[str] = set()
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            marks = ",".join("?" * len(part))
            out.update(k for (k,) in self.db.execute(f"SELECT key FROM emb WHERE key IN ({marks})", part))
        return out

    def put_many(self, items: dict[str, list[float]]) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.db.executemany(
            "INSERT OR REPLACE INTO emb VALUES (?, ?, ?, ?)",
            [(k, len(v), struct.pack(f"{len(v)}f", *v), now) for k, v in items.items()])
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# ---------- Gemini ----------

def get_api_key() -> str:
    """Read GEMINI_API_KEY from the environment, loading .env first. Called only when embedding."""
    from dotenv import load_dotenv
    load_dotenv(config.ROOT / ".env")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise EmbeddingError("GEMINI_API_KEY is missing: create .env (see .env.example) and run again")
    return key


def is_quota_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 429 or "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)


def is_daily_quota(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("perday", "per day", "per_day", "daily"))


def is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc)
    return any(k in text for k in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "429", "503", "timed out"))


def with_retries(fn, sleep=time.sleep, attempts: int | None = None):
    """Call fn(). 5xx: exponential backoff, up to EMBED_RETRIES attempts. 429: NEVER a retry loop.

    A daily 429 stops at once (QuotaStop). A per-minute 429 waits EMBED_QUOTA_WAIT_SEC and retries at most
    EMBED_QUOTA_RETRIES times, then stops.
    """
    attempts = attempts or config.EMBED_RETRIES
    last, quota_waits, attempt = None, 0, 0
    while attempt < attempts:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001  (SDK exception types vary by version)
            if isinstance(exc, QuotaStop):
                raise
            if is_quota_error(exc):
                if is_daily_quota(exc):
                    raise QuotaStop(f"daily quota reached: {str(exc)[:200]}", daily=True) from exc
                if quota_waits >= config.EMBED_QUOTA_RETRIES:
                    raise QuotaStop(f"per-minute quota still exceeded after {quota_waits} waits: {str(exc)[:200]}",
                                    daily=False) from exc
                quota_waits += 1
                sleep(config.EMBED_QUOTA_WAIT_SEC)
                continue
            if not is_retryable(exc):
                raise
            last = exc
            attempt += 1
            if attempt < attempts:
                sleep(config.EMBED_BACKOFF_SEC * (2 ** (attempt - 1)))
    raise EmbeddingError(f"gave up after {attempts} attempts: {last}")


# ---------- quota: per-minute window (texts + tokens) and per-day count ----------

class RateLimiter:
    """Sliding 60-second window over texts and estimated tokens. acquire() sleeps until the request fits."""

    def __init__(self, rpm: int | None = None, tpm: int | None = None, clock=time.monotonic, sleep=time.sleep,
                 on_wait=None):
        self.rpm, self.tpm = rpm or config.EMBED_RPM, tpm or config.EMBED_TPM
        self.clock, self.sleep, self.on_wait = clock, sleep, on_wait
        self.events: list[tuple[float, int, int]] = []

    def acquire(self, texts: int, tokens: int) -> float:
        """Wait until `texts` and `tokens` fit in the last minute, then record them. Returns seconds waited."""
        if texts > self.rpm or tokens > self.tpm:
            raise EmbeddingError(f"one request ({texts} texts, ~{tokens} tokens) exceeds the per-minute quota "
                                 f"({self.rpm} texts, {self.tpm} tokens)")
        waited = 0.0
        while True:
            now = self.clock()
            self.events = [e for e in self.events if now - e[0] < 60]
            if (sum(e[1] for e in self.events) + texts <= self.rpm
                    and sum(e[2] for e in self.events) + tokens <= self.tpm):
                self.events.append((now, texts, tokens))
                return waited
            delay = max(60 - (now - self.events[0][0]), 0) + 0.5
            if self.on_wait:
                self.on_wait(delay)
            self.sleep(delay)
            waited += delay


class DailyUsage:
    """Texts sent to the API today (Pacific date, when Google resets the daily quota). Persisted in a json file."""

    def __init__(self, path: Path | str | None = None, limit: int | None = None, today: str | None = None):
        self.path = Path(path or config.EMBED_USAGE_PATH)
        self.limit = limit or config.EMBED_RPD
        self.today = today or self.pacific_date()
        self.used = 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("date") == self.today:
                self.used = int(data.get("texts", 0))
        except (OSError, ValueError):
            pass

    @staticmethod
    def pacific_date() -> str:
        from datetime import datetime, timedelta, timezone
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001  (no tz database on this machine)
            return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def set_used(self, n: int) -> None:
        self.used = n
        self._save()

    def add(self, n: int) -> None:
        self.used += n
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": self.today, "texts": self.used, "limit": self.limit}), encoding="utf-8")
        tmp.replace(self.path)


class ThrottledEmbedder:
    """Wraps an embedder: per-minute limits by texts and tokens, a stop at the daily limit, usage persisted."""

    def __init__(self, inner, limiter: RateLimiter, usage: DailyUsage):
        self.inner, self.limiter, self.usage = inner, limiter, usage

    def embed(self, texts: list[str]) -> list[list[float]]:
        if len(texts) > self.usage.remaining:
            raise QuotaStop(f"daily limit: {self.usage.used}/{self.usage.limit} texts used today, "
                            f"{len(texts)} more requested", daily=True)
        tokens = sum(estimate_tokens(t) for t in texts)
        # The gate runs before EVERY attempt, retries (5xx, after a 429) included: Google counts each of them.
        gate = lambda: self.limiter.acquire(len(texts), tokens)  # noqa: E731
        if hasattr(self.inner, "gate"):
            self.inner.gate = gate
        else:
            gate()
        try:
            vectors = self.inner.embed(texts)
        except QuotaStop as exc:
            if exc.daily:
                self.usage.set_used(self.usage.limit)
            raise
        finally:
            if hasattr(self.inner, "gate"):
                self.inner.gate = None
        self.usage.add(len(texts))
        return vectors


def make_embedder(on_wait=None) -> ThrottledEmbedder:
    """The embedder every script should use: Gemini + quota limits."""
    return ThrottledEmbedder(GeminiEmbedder(), RateLimiter(on_wait=on_wait), DailyUsage())


class GeminiEmbedder:
    def __init__(self, api_key: str | None = None, sleep=time.sleep):
        import bootstrap  # noqa: F401  (truststore, for NetFree)
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=api_key or get_api_key())
        self._sleep = sleep
        self.gate = None   # set by ThrottledEmbedder: called before every attempt, retries included

    def embed(self, texts: list[str]) -> list[list[float]]:
        types = self._types
        contents = [types.Content(parts=[types.Part.from_text(text=t)]) for t in texts]  # one vector per text
        cfg = types.EmbedContentConfig(output_dimensionality=config.EMBED_DIM)

        def call():
            if self.gate:
                self.gate()
            return self._client.models.embed_content(model=config.EMBED_MODEL, contents=contents, config=cfg)

        result = with_retries(call, self._sleep)
        vectors = [list(e.values) for e in result.embeddings]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} embeddings, got {len(vectors)} "
                                 "(inputs may have been aggregated into one vector)")
        for v in vectors:
            if len(v) != config.EMBED_DIM:
                raise EmbeddingError(f"expected dimension {config.EMBED_DIM}, got {len(v)}")
        return vectors


# ---------- batching + cache ----------

def make_batches(inputs: list[str]) -> list[list[int]]:
    """Indices grouped by EMBED_BATCH and by the estimated token budget per request."""
    batches, cur, tokens = [], [], 0
    for i, text in enumerate(inputs):
        t = estimate_tokens(text)
        if cur and (len(cur) >= config.EMBED_BATCH or tokens + t > config.EMBED_BATCH_MAX_TOKENS_EST):
            batches.append(cur)
            cur, tokens = [], 0
        cur.append(i)
        tokens += t
    if cur:
        batches.append(cur)
    return batches


def embed_inputs(inputs: list[str], embedder, cache: EmbedCache | None,
                 sleep=time.sleep, clock=time.monotonic, progress=None) -> tuple[list[list[float]], dict]:
    """Vectors in input order. Cached inputs never reach the API.

    stats: inputs, unique, hits, misses, api_calls.
    Every batch is written to the cache right after its API call, so a stopped / failed run resumes from the
    cache on the next run. progress(done_texts, total_texts, api_calls) is called after each batch.
    """
    keys = [cache_key(t) for t in inputs]
    found = cache.get_many(list(set(keys))) if cache else {}
    todo = sorted({i for i, k in enumerate(keys) if k not in found}, key=lambda i: i)
    seen_keys: set[str] = set()
    todo = [i for i in todo if not (keys[i] in seen_keys or seen_keys.add(keys[i]))]  # one call per distinct text
    api_calls, last_call = 0, None
    todo_texts = [inputs[i] for i in todo]
    finished: list[int] = []
    for batch in make_batches(todo_texts):
        if last_call is not None:
            wait = config.EMBED_MIN_INTERVAL_SEC - (clock() - last_call)
            if wait > 0:
                sleep(wait)
        texts = [todo_texts[j] for j in batch]
        try:
            vectors = embedder.embed(texts)
        except QuotaStop as stop:  # everything finished so far is already in the cache
            stop.done = len(finished)
            stop.remaining = len(todo) - stop.done
            raise
        last_call = clock()
        if len(vectors) != len(texts):  # e.g. several inputs aggregated into one vector
            raise EmbeddingError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        if any(len(v) != config.EMBED_DIM for v in vectors):
            raise EmbeddingError(f"expected dimension {config.EMBED_DIM}")
        api_calls += 1
        # same precision as the cache (float32), so a first run and a cached run give identical vectors
        new = {keys[todo[j]]: _as_float32(v) for j, v in zip(batch, vectors)}
        found.update(new)
        finished.extend(batch)
        if cache:
            cache.put_many(new)
        if progress:
            progress(sum(1 for i in todo if keys[i] in found), len(todo), api_calls)
    stats = {"inputs": len(inputs), "unique": len(set(keys)), "misses": len(todo),
             "hits": len(set(keys)) - len(todo), "api_calls": api_calls}
    return [found[k] for k in keys], stats


def embed_query(query: str, embedder, cache: EmbedCache | None = None) -> list[float]:
    return embed_inputs([build_query_input(query)], embedder, cache)[0][0]
