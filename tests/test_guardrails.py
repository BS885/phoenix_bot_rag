import json
import unittest
from pathlib import Path

import guardrails as g

CASES = json.loads((Path(__file__).resolve().parent.parent / "eval" / "guardrail_cases.json")
                   .read_text(encoding="utf-8"))["classes"]

EXPECTED = {            # class -> (kind, coverage flag)
    "coverage": ("none", True),
    "pure_personal": ("personal", False),
    "suspected_personal": ("none", False),
    "advice": ("advice", False),
    "greeting": ("greeting", False),
    "procedure": ("none", False),
}


class TestExamplesPerClass(unittest.TestCase):
    def test_every_class_has_five_examples_with_an_imprecise_one_and_a_tricky_one_somewhere(self):
        for name, items in CASES.items():
            self.assertGreaterEqual(len(items), 5, name)
            self.assertTrue(any(i.get("imprecise") for i in items), name)
        self.assertTrue(any(i.get("tricky") for items in CASES.values() for i in items))

    def test_each_example_gets_its_class(self):
        for name, items in CASES.items():
            kind, cov = EXPECTED[name]
            for item in items:
                got = g.classify(item["q"])
                self.assertEqual((got.kind, got.coverage), (kind, cov), f"{name}: {item['q']} -> {got}")

    def test_suspected_personal_flag_only_for_that_class(self):
        for name, items in CASES.items():
            for item in items:
                got = g.classify(item["q"])
                self.assertEqual(got.suspected_personal, name == "suspected_personal", f"{name}: {item['q']} -> {got}")


class TestRules(unittest.TestCase):
    def flags(self, q):
        x = g.classify(q)
        return x.kind, x.coverage, x.suspected_personal

    def test_doctor_consultation_procedure_is_not_advice(self):
        self.assertEqual(self.flags("כדאי לי לצרף מסמך לתביעה?"), ("none", False, False))

    def test_real_advice_with_the_same_phrase(self):
        self.assertEqual(g.classify("כדאי לי לקנות ביטוח נסיעות?").kind, "advice")

    def test_coverage_beats_pure_personal(self):
        got = g.classify("האם התביעה שלי מכוסה?")
        self.assertEqual((got.kind, got.coverage), ("none", True))

    def test_nine_digit_id_is_personal(self):
        self.assertEqual(g.classify("מספר הזהות 123456789 מה קורה").kind, "personal")

    def test_greeting_must_be_the_whole_question(self):
        self.assertEqual(g.classify("שלום").kind, "greeting")
        self.assertEqual(g.classify("שלום, מה כולל ביטוח בריאות?").kind, "none")
        self.assertEqual(g.classify("תודה, איך מגישים תביעה בביטוח רכב").kind, "none")

    def test_normalization(self):
        self.assertEqual(g.normalize("שָׁלוֹם!!"), "שלום")
        self.assertEqual(g.normalize('ת"ז'), "תז")
        self.assertEqual(g.normalize("הייייי"), "היי")

    def test_plain_questions_have_no_flags(self):
        for q in ("מה שער הדולר היום?", "איך מגישים תביעה?", "מה כולל ביטוח בריאות?"):
            self.assertEqual(self.flags(q), ("none", False, False), q)

    def test_matched_phrases_are_reported(self):
        self.assertIn("מכוסה", g.classify("האם דיקור סיני מכוסה?").matched)

    def test_choice_verb_beats_the_how_to_word_but_a_plain_how_to_stays_a_procedure(self):
        self.assertEqual(g.classify("איך כדאי לי לבחור בין שתי קרנות?").kind, "advice")
        self.assertEqual(g.classify("איך מגישים תביעה?").kind, "none")
        self.assertEqual(g.classify("האם יאשרו לי את הניתוח?").kind, "personal")

    def test_pending_owner_cases_are_recorded_and_not_part_of_the_classes(self):
        pending = json.loads((Path(__file__).resolve().parent.parent / "eval" / "guardrail_cases.json")
                             .read_text(encoding="utf-8"))["pending_owner_decision"]
        all_q = {i["q"] for items in CASES.values() for i in items}
        for p in pending:
            self.assertNotIn(p["q"], all_q)

    def test_the_five_owner_questions(self):
        self.assertEqual(self.flags("מה ההחזר שיש לי על דיקור סיני?"), ("none", True, False))
        self.assertEqual(self.flags("מה הכיסוי שלי על תרופות?"), ("none", True, False))
        self.assertEqual(self.flags("יש לי ניתוח, מה אני צריך לעשות בשביל זה?"), ("none", False, False))
        self.assertEqual(self.flags("לייעוץ הרופא, מה אני צריך לצרף?"), ("none", False, False))
        self.assertEqual(self.flags("מחלה קשה, ומה אני צריך לצרף בשביל התביעה?"), ("none", False, False))


if __name__ == "__main__":
    unittest.main()
