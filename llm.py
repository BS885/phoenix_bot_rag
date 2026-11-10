"""The only module that talks to the language model: generate(prompt, schema) -> dict.

Everything else (guardrails, rewrite, answer) calls generate() and never the SDK, so the provider can be
replaced (decision 11). Quota handling mirrors the embeddings: a per-minute window (requests + estimated
tokens), a per-model daily request counter, and an orderly stop on 429 (never a retry loop). A broken JSON or a
5xx is retried once; after that LLMError is raised and the caller answers with status "error", never "not_found".
"""
import json
import threading
import time
from pathlib import Path

import config
from index import embed


class LLMError(Exception):
    """The model could not give a usable answer (API down, broken JSON, quota). Maps to status 'error'."""


class LLMBusyError(LLMError):
    """The per-minute limiter would make the question wait longer than config.LLM_MAX_WAIT_SEC: answer "busy" (status error)."""

    def __init__(self, message: str, wait: float = 0.0):
        super().__init__(message)
        self.wait = wait


class LLMQuotaError(LLMError):
    def __init__(self, message: str, daily: bool):
        super().__init__(message)
        self.daily = daily


def check_model(model: str) -> str:
    if model in config.LLM_FORBIDDEN_MODELS:
        raise LLMError(f"model {model!r} is deprecated and must not be used")
    return model


class GeminiBackend:
    """One call to the Gemini API returning the raw JSON text."""

    def __init__(self, api_key: str | None = None):
        import bootstrap  # noqa: F401  (truststore, for NetFree)
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=api_key or embed.get_api_key(),
                                    http_options=types.HttpOptions(timeout=config.LLM_TIMEOUT_MS))

    def call(self, model: str, prompt: str, system: str | None, schema: dict) -> str:
        types = self._types
        cfg = types.GenerateContentConfig(system_instruction=system, temperature=config.LLM_TEMPERATURE, seed=config.LLM_SEED,
                                          response_mime_type="application/json", response_json_schema=schema)
        resp = self._client.models.generate_content(model=model, contents=prompt, config=cfg)
        return resp.text or ""


class LLM:
    def __init__(self, backend=None, sleep=time.sleep, clock=time.monotonic, usage_dir: Path | str | None = None,
                 rpm: int | None = None, tpm: int | None = None, rpd: int | None = None, on_wait=None):
        self._backend = backend
        self._sleep, self._clock, self._on_wait = sleep, clock, on_wait
        self._override = (rpm, tpm, rpd)   # tests: one set of limits for every model
        self._usage_dir = Path(usage_dir) if usage_dir else config.LLM_USAGE_PATH.parent
        self._limiters: dict[str, embed.RateLimiter] = {}
        self._usage: dict[str, embed.DailyUsage] = {}
        self._lock = threading.Lock()          # creation of per-model usage / limiter objects (shared by all threads)

    def limits(self, model: str) -> tuple[int, int, int]:
        """(requests/min, tokens/min, requests/day) the bot may use: 80% of RPM/TPM, the daily cap."""
        rpm, tpm, rpd = self._override
        lim = config.LLM_LIMITS.get(model, config.LLM_DEFAULT_LIMITS)
        return (rpm or max(int(lim["rpm"] * config.RATE_SAFETY), 1), tpm or int(lim["tpm"] * config.RATE_SAFETY),
                rpd or lim["rpd_cap"])

    @property
    def backend(self):
        if self._backend is None:
            self._backend = GeminiBackend()
        return self._backend

    def usage(self, model: str) -> "embed.DailyUsage":
        with self._lock:
            if model not in self._usage:
                safe = model.replace("/", "_")
                self._usage[model] = embed.DailyUsage(self._usage_dir / f"llm_usage_{safe}.json", limit=self.limits(model)[2])
            return self._usage[model]

    def _limiter(self, model: str) -> "embed.RateLimiter":
        with self._lock:
            if model not in self._limiters:
                self._limiters[model] = embed.RateLimiter(*self.limits(model)[:2], clock=self._clock, sleep=self._sleep,
                                                          on_wait=self._on_wait)
            return self._limiters[model]

    def _one_call(self, model: str, prompt: str, system: str | None, schema: dict) -> str:
        usage = self.usage(model)
        if usage.remaining < 1:
            raise LLMQuotaError(f"daily request limit for {model}: {usage.used}/{usage.limit} used today", daily=True)
        tokens = embed.estimate_tokens(prompt + (system or ""))

        def attempt():   # the limiter runs before EVERY attempt, retries included (Google counts each one)
            self._limiter(model).acquire(1, tokens, max_wait=config.LLM_MAX_WAIT_SEC)
            return self.backend.call(model, prompt, system, schema)

        try:
            text = embed.with_retries(attempt, self._sleep, attempts=config.LLM_RETRIES + 1)
        except embed.RateLimitBusy as busy:
            raise LLMBusyError(f"busy: {busy}", wait=busy.wait) from busy
        except embed.QuotaStop as stop:
            if stop.daily:
                usage.set_used(usage.limit)
            raise LLMQuotaError(str(stop), daily=stop.daily) from stop
        except embed.EmbeddingError as exc:  # gave up after retries on 5xx / timeouts
            usage.add(1)
            raise LLMError(f"model call failed: {exc}") from exc
        usage.add(1)
        return text

    def generate(self, prompt: str, schema: dict, *, system: str | None = None, model: str | None = None) -> dict:
        """JSON object that matches `schema` (required keys present). One retry on a broken JSON, then LLMError."""
        model = check_model(model or config.LLM_MODEL_ANSWER)
        last = ""
        for _ in range(config.LLM_RETRIES + 1):
            text = self._one_call(model, prompt, system, schema)
            try:
                data = json.loads(text)
                missing = [k for k in schema.get("required", []) if not isinstance(data, dict) or k not in data]
                if not missing:
                    return data
                last = f"missing keys {missing}"
            except ValueError as exc:
                last = f"invalid JSON ({exc})"
        raise LLMError(f"model returned unusable output twice: {last}")


_default: LLM | None = None
_default_lock = threading.Lock()


def generate(prompt: str, schema: dict, *, system: str | None = None, model: str | None = None) -> dict:
    """Module-level entry point used by the rest of the bot (CLAUDE.md contract)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = LLM()
    return _default.generate(prompt, schema, system=system, model=model)
