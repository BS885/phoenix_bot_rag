"""Embedding layer: Gemini client, sqlite cache, batching, retries and token warnings.

The embedder is injected (anything with .embed(list[str]) -> list[list[float]]), so everything
here is testable without the network.
"""
import hashlib
import math
import os
import sqlite3
import struct
import time
from pathlib import Path

import config


class EmbeddingError(Exception):
    pass


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


def is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc)
    return any(s in text for s in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "429", "503", "timed out"))


def with_retries(fn, sleep=time.sleep):
    """Call fn(); on 429/5xx wait with exponential backoff, up to EMBED_RETRIES attempts."""
    last = None
    for attempt in range(config.EMBED_RETRIES):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001  (SDK exception types vary by version)
            if not is_retryable(exc):
                raise
            last = exc
            if attempt < config.EMBED_RETRIES - 1:
                sleep(config.EMBED_BACKOFF_SEC * (2 ** attempt))
    raise EmbeddingError(f"gave up after {config.EMBED_RETRIES} attempts: {last}")


class GeminiEmbedder:
    def __init__(self, api_key: str | None = None, sleep=time.sleep):
        import bootstrap  # noqa: F401  (truststore, for NetFree)
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=api_key or get_api_key())
        self._sleep = sleep

    def embed(self, texts: list[str]) -> list[list[float]]:
        types = self._types
        contents = [types.Content(parts=[types.Part.from_text(text=t)]) for t in texts]  # one vector per text
        cfg = types.EmbedContentConfig(output_dimensionality=config.EMBED_DIM)

        def call():
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
                 sleep=time.sleep, clock=time.monotonic) -> tuple[list[list[float]], dict]:
    """Vectors in input order. Cached inputs never reach the API.

    stats: inputs, unique, hits, misses, api_calls.
    """
    keys = [cache_key(t) for t in inputs]
    found = cache.get_many(list(set(keys))) if cache else {}
    todo = sorted({i for i, k in enumerate(keys) if k not in found}, key=lambda i: i)
    seen_keys: set[str] = set()
    todo = [i for i in todo if not (keys[i] in seen_keys or seen_keys.add(keys[i]))]  # one call per distinct text
    api_calls, last_call = 0, None
    todo_texts = [inputs[i] for i in todo]
    for batch in make_batches(todo_texts):
        if last_call is not None:
            wait = config.EMBED_MIN_INTERVAL_SEC - (clock() - last_call)
            if wait > 0:
                sleep(wait)
        texts = [todo_texts[j] for j in batch]
        vectors = embedder.embed(texts)
        last_call = clock()
        if len(vectors) != len(texts):  # e.g. several inputs aggregated into one vector
            raise EmbeddingError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        if any(len(v) != config.EMBED_DIM for v in vectors):
            raise EmbeddingError(f"expected dimension {config.EMBED_DIM}")
        api_calls += 1
        # same precision as the cache (float32), so a first run and a cached run give identical vectors
        new = {keys[todo[j]]: _as_float32(v) for j, v in zip(batch, vectors)}
        found.update(new)
        if cache:
            cache.put_many(new)
    stats = {"inputs": len(inputs), "unique": len(set(keys)), "misses": len(todo),
             "hits": len(set(keys)) - len(todo), "api_calls": api_calls}
    return [found[k] for k in keys], stats


def embed_query(query: str, embedder, cache: EmbedCache | None = None) -> list[float]:
    return embed_inputs([build_query_input(query)], embedder, cache)[0][0]
