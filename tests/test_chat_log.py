import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chat_log
import config
from answer import BotReply


def reply():
    return BotReply("partial", "תשובה", sources=[], missing="x", debug={
        "calls": 2, "retry_reason": "no_claims", "style_retry": "used", "removed_claims": [{"text": "t"}], "pii_masked": {"id": 1, "phone": 0, "email": 0},
        "retrieval": {"groups": [{"url": "https://www.fnx.co.il/a/", "section": "איך?", "distance": 0.2, "topic": "t", "size": 1}]},
        "raw_calls": [{"reply": {"secret": "model json"}}], "model": {"found": True}})


class TestChatLog(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "logs" / "chat.jsonl"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("PHOENIX_CHAT_LOG", None)

    def tearDown(self):
        self.env.stop()

    def test_off_by_default_nothing_is_created(self):
        self.assertFalse(config.CHAT_LOG_ENABLED)
        self.assertFalse(chat_log.enabled())
        self.assertFalse(chat_log.log_turn("שאלה", reply(), path=self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_only_the_environment_variable_turns_it_on(self):
        with mock.patch.dict(os.environ, {"PHOENIX_CHAT_LOG": "1"}):
            self.assertTrue(chat_log.enabled())
        with mock.patch.dict(os.environ, {"PHOENIX_CHAT_LOG": "0"}):
            self.assertFalse(chat_log.enabled())

    def test_enabled_writes_one_masked_line_per_turn(self):
        with mock.patch.dict(os.environ, {"PHOENIX_CHAT_LOG": "1"}):
            self.assertTrue(chat_log.log_turn("הנייד שלי 0541234567 ומייל a@b.com", reply(), path=self.path))
            chat_log.log_turn("שאלה שנייה", reply(), path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        rec = json.loads(lines[0])
        self.assertEqual(rec["question"], f"הנייד שלי {config.PII_PLACEHOLDERS['phone']} ומייל {config.PII_PLACEHOLDERS['email']}")
        self.assertNotIn("0541234567", lines[0])
        self.assertNotIn("a@b.com", lines[0])

    def test_the_record_has_only_the_agreed_fields_and_no_raw_model_json(self):
        rec = chat_log.build_record("שאלה", reply())
        self.assertEqual(set(rec), {"ts", "question", "status", "groups", "llm_calls", "retry", "style_retry", "removed_claims",
                                   "pii_masked", "error"})
        self.assertEqual(rec["groups"], [{"url": "https://www.fnx.co.il/a/", "section": "איך?", "distance": 0.2}])
        self.assertEqual((rec["status"], rec["llm_calls"], rec["removed_claims"]), ("partial", 2, 1))
        self.assertNotIn("secret", json.dumps(rec, ensure_ascii=False))
        self.assertNotIn("תשובה", json.dumps(rec, ensure_ascii=False))                # the answer text is not logged either


if __name__ == "__main__":
    unittest.main()
