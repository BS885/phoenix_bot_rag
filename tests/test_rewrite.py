import unittest

import config
import guardrails
import llm
import rewrite as rw

HISTORY = [
    {"role": "user", "text": "איך מגישים תביעה בביטוח בריאות?"},
    {"role": "bot", "text": "ניתן להגיש תביעה דרך האתר או האפליקציה."},
]


class Gen:
    """A stand-in for llm.generate: records calls, returns or raises what it was given."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, prompt, schema, *, system=None, model=None):
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class TestWhenTheModelIsCalled(unittest.TestCase):
    def test_no_history_means_no_call(self):
        gen = Gen({"question": "x"})
        out = rw.rewrite("מה זה?", [], generate=gen)
        self.assertEqual((gen.calls, out.question, out.called_model), ([], "מה זה?", False))

    def test_standalone_long_question_with_history_means_no_call(self):
        gen = Gen({"question": "x"})
        q = "אני רוצה לדעת אילו מסמכים נדרשים להגשת תביעה על ניתוח בישראל"
        out = rw.rewrite(q, HISTORY, generate=gen)
        self.assertEqual((gen.calls, out.question), ([], q))

    def test_followup_calls_the_fast_model_once_with_the_short_prompt(self):
        gen = Gen({"question": "אילו מסמכים צריך לצרף לתביעה בביטוח בריאות?"})
        out = rw.rewrite("ומה לצרף?", HISTORY, generate=gen)
        self.assertEqual(len(gen.calls), 1)
        self.assertEqual(gen.calls[0]["model"], config.LLM_MODEL_FAST)
        self.assertEqual(gen.calls[0]["system"], config.REWRITE_SYSTEM)
        self.assertIn("ומה לצרף?", gen.calls[0]["prompt"])
        self.assertTrue(out.rewritten)
        self.assertEqual(out.question, "אילו מסמכים צריך לצרף לתביעה בביטוח בריאות?")

    def test_history_is_cut_to_the_last_turns(self):
        long_history = [{"role": "user", "text": f"הודעה {i}"} for i in range(10)]
        gen = Gen({"question": "שאלה עצמאית על ביטוח"})
        rw.rewrite("ומה אחר כך?", long_history, generate=gen)
        self.assertNotIn("הודעה 0", gen.calls[0]["prompt"])
        self.assertIn("הודעה 9", gen.calls[0]["prompt"])


class TestFailureKeepsTheOriginalQuestion(unittest.TestCase):
    def test_api_error(self):
        out = rw.rewrite("ומה לצרף?", HISTORY, generate=Gen(llm.LLMError("down")))
        self.assertEqual((out.question, out.rewritten, out.quota_error), ("ומה לצרף?", False, False))
        self.assertIn("model", out.error)

    def test_quota_error_is_flagged(self):
        out = rw.rewrite("ומה לצרף?", HISTORY, generate=Gen(llm.LLMQuotaError("daily", daily=True)))
        self.assertEqual((out.question, out.quota_error), ("ומה לצרף?", True))

    def test_numbers_added_by_the_model_reject_the_rewrite(self):
        out = rw.rewrite("ומה לצרף?", HISTORY, generate=Gen({"question": "מה לצרף לתביעה? מגיעים 5000 שקלים"}))
        self.assertEqual((out.question, out.rewritten), ("ומה לצרף?", False))
        self.assertIn("numbers", out.error)

    def test_numbers_from_the_history_are_allowed(self):
        hist = [{"role": "user", "text": "מה מספר הטלפון 5432*?"}]
        out = rw.rewrite("ומה השעות שלו?", hist, generate=Gen({"question": "מה שעות הפעילות של המוקד 5432*?"}))
        self.assertTrue(out.rewritten)

    def test_empty_or_too_long_rewrite_is_rejected(self):
        for bad in ("", "א" * (config.REWRITE_MAX_CHARS + 1)):
            out = rw.rewrite("ומה לצרף?", HISTORY, generate=Gen({"question": bad}))
            self.assertEqual((out.question, out.rewritten), ("ומה לצרף?", False))


class TestLinkingTermExamples(unittest.TestCase):
    """The rewrite may add a general linking term (no facts, no numbers): accepted."""

    def check(self, followup, rewritten):
        out = rw.rewrite(followup, HISTORY, generate=Gen({"question": rewritten}))
        self.assertTrue(out.rewritten, out.error)
        self.assertEqual(out.question, rewritten)

    def test_acupuncture(self):
        self.check("ומה לגבי דיקור סיני?", "מה הכיסוי בביטוח בריאות לדיקור סיני (רפואה משלימה)?")

    def test_glasses(self):
        self.check("ומשקפיים?", "האם ביטוח בריאות מכסה משקפיים (שירותי ראייה)?")

    def test_physio(self):
        self.check("ופיזיותרפיה?", "מה הכיסוי בביטוח בריאות לפיזיותרפיה (טיפולים פרא-רפואיים)?")

    def test_second_opinion(self):
        self.check("ואם אני רוצה חוות דעת נוספת?", "איך מקבלים חוות דעת נוספת (ייעוץ רפואי) בביטוח בריאות?")


class TestProductInheritance(unittest.TestCase):
    def test_followup_inherits_the_product_from_the_history(self):
        out = rw.rewrite("ומה לצרף?", HISTORY, generate=Gen(llm.LLMError("down")))
        self.assertEqual((out.products, out.inherited_product), (["health-insurance"], True))

    def test_a_product_named_now_beats_the_history(self):
        out = rw.rewrite("ובביטוח רכב?", HISTORY, generate=Gen(llm.LLMError("down")))
        self.assertEqual((out.products, out.inherited_product), (["car-insurance"], False))

    def test_no_product_anywhere(self):
        out = rw.rewrite("ומה לצרף?", [{"role": "user", "text": "שלום"}], generate=Gen(llm.LLMError("down")))
        self.assertEqual((out.products, out.inherited_product), ([], False))

    def test_the_rewrite_may_name_the_product(self):
        hist = [{"role": "user", "text": "שלום"}]
        out = rw.rewrite("ומה לצרף?", hist, generate=Gen({"question": "מה לצרף לתביעה בביטוח נסיעות?"}))
        self.assertEqual((out.products, out.inherited_product), (["travel-insurance"], False))


class TestGuardMerge(unittest.TestCase):
    def test_flags_of_both_questions_are_united(self):
        out = rw.rewrite("ומה לגבי דיקור סיני?", HISTORY,
                         generate=Gen({"question": "האם דיקור סיני מכוסה בביטוח בריאות?"}))
        self.assertTrue(out.guard.coverage)          # only the rewrite says "מכוסה"

    def test_personal_in_the_original_survives_a_neutral_rewrite(self):
        out = rw.rewrite("מה מצב התביעה שלי?", HISTORY, generate=Gen({"question": "מהו מצב תביעה בביטוח בריאות?"}))
        self.assertEqual(out.guard.kind, "personal")

    def test_merge_priority(self):
        a, b = guardrails.Guard(kind="advice"), guardrails.Guard(kind="personal")
        self.assertEqual(rw.merge_guards(a, b).kind, "personal")
        self.assertEqual(rw.merge_guards(guardrails.Guard(kind="greeting"), guardrails.Guard()).kind, "greeting")
        self.assertEqual(rw.merge_guards(guardrails.Guard(), guardrails.Guard(kind="greeting")).kind, "none")


if __name__ == "__main__":
    unittest.main()
