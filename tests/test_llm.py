import tempfile
import unittest
from pathlib import Path

import config
import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


class FakeBackend:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def call(self, model, prompt, system, schema):
        self.calls.append((model, prompt, system))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Err(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def make(backend, **kw):
    d = Path(tempfile.mkdtemp())
    return llm.LLM(backend, sleep=lambda s: None, usage_dir=d, **kw)


class TestGenerate(unittest.TestCase):
    def test_returns_parsed_json_and_uses_the_answer_model_by_default(self):
        b = FakeBackend(['{"a": "שלום"}'])
        out = make(b).generate("שאלה", SCHEMA, system="כללים")
        self.assertEqual(out, {"a": "שלום"})
        self.assertEqual(b.calls[0], (config.LLM_MODEL_ANSWER, "שאלה", "כללים"))

    def test_broken_json_is_retried_once_then_fails(self):
        b = FakeBackend(["not json", '{"a": "ok"}'])
        self.assertEqual(make(b).generate("p", SCHEMA), {"a": "ok"})
        b2 = FakeBackend(["not json", "{}"])
        with self.assertRaises(llm.LLMError):
            make(b2).generate("p", SCHEMA)
        self.assertEqual(len(b2.calls), 2)     # exactly one retry

    def test_5xx_is_retried_once_then_llm_error(self):
        b = FakeBackend([Err("unavailable", 503), '{"a": "x"}'])
        self.assertEqual(make(b).generate("p", SCHEMA), {"a": "x"})
        b2 = FakeBackend([Err("unavailable", 503), Err("unavailable", 503)])
        with self.assertRaises(llm.LLMError):
            make(b2).generate("p", SCHEMA)
        self.assertEqual(len(b2.calls), 2)

    def test_429_never_loops(self):
        b = FakeBackend([Err("RESOURCE_EXHAUSTED GenerateRequestsPerDay quota", 429)])
        with self.assertRaises(llm.LLMQuotaError) as cm:
            make(b).generate("p", SCHEMA)
        self.assertTrue(cm.exception.daily)
        self.assertEqual(len(b.calls), 1)
        # the minute quota waits a bounded number of times and then stops (not an endless loop)
        b2 = FakeBackend([Err("RESOURCE_EXHAUSTED per minute", 429)] * 10)
        with self.assertRaises(llm.LLMQuotaError) as cm2:
            make(b2).generate("p", SCHEMA)
        self.assertFalse(cm2.exception.daily)
        self.assertEqual(len(b2.calls), config.EMBED_QUOTA_RETRIES + 1)

    def test_daily_request_counter_stops_before_calling(self):
        b = FakeBackend(['{"a": "1"}', '{"a": "2"}', '{"a": "3"}'])
        m = make(b, rpd=2)
        m.generate("p", SCHEMA)
        m.generate("p", SCHEMA)
        with self.assertRaises(llm.LLMQuotaError):
            m.generate("p", SCHEMA)
        self.assertEqual(len(b.calls), 2)

    def test_per_minute_request_limit_waits(self):
        slept, now = [], [0.0]
        d = Path(tempfile.mkdtemp())
        m = llm.LLM(FakeBackend(['{"a": "x"}'] * 3), sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                    clock=lambda: now[0], usage_dir=d, rpm=2)
        for _ in range(3):
            m.generate("p", SCHEMA)
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 60)

    def test_deprecated_models_are_refused(self):
        for model in ("gemini-2.5-flash", "gemini-2.5-pro"):
            with self.assertRaises(llm.LLMError):
                make(FakeBackend([])).generate("p", SCHEMA, model=model)

    def test_configured_models_are_not_deprecated(self):
        for model in (config.LLM_MODEL_FAST, config.LLM_MODEL_ANSWER):
            self.assertNotIn(model, config.LLM_FORBIDDEN_MODELS)
            llm.check_model(model)

    def test_usage_is_kept_per_model(self):
        m = make(FakeBackend(['{"a": "1"}', '{"a": "2"}']))
        m.generate("p", SCHEMA, model=config.LLM_MODEL_FAST)
        m.generate("p", SCHEMA, model=config.LLM_MODEL_ANSWER)
        self.assertEqual(m.usage(config.LLM_MODEL_FAST).used, 1)
        self.assertEqual(m.usage(config.LLM_MODEL_ANSWER).used, 1)


if __name__ == "__main__":
    unittest.main()
