import json
import re
import unittest
from pathlib import Path

import config
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

    def test_receiving_phrases_are_coverage(self):
        for q in ("מה מקבלים על דיקור?", "מה אני מקבל על תרופות?", "מה אני מקבלת על ניתוח?", "כמה מקבלים על משקפיים?"):
            got = g.classify(q)
            self.assertEqual((got.kind, got.coverage), ("none", True), q)
        self.assertFalse(g.classify("איך מקבלים אישור לניתוח?").coverage)       # a how-to, not coverage

    def test_best_phrase_is_advice_only_without_a_procedure_verb(self):
        self.assertEqual(g.classify("איזו קרן הכי טובה?").kind, "advice")
        self.assertEqual(g.classify("מה הכי טוב לי?").kind, "advice")
        for q in ("מה הכי טוב לצרף לתביעה?", "מה הכי טוב להגיש לתביעה?", "מה הכי טוב למלא בטופס?"):
            self.assertEqual(g.classify(q).kind, "none", q)

    def test_strong_choice_verbs_stay_advice_even_with_a_procedure_verb(self):
        for q in ("איך כדאי לי לבחור בין שתי קרנות?", "האם לקנות ביטוח ולהגיש טופס?", "מה עדיף לצרף?",
                  "כדאי להצטרף ולהגיש בקשה?", "איפה להשקיע ולהגיש תביעה?"):
            self.assertEqual(g.classify(q).kind, "advice", q)

    def test_personal_reply_names_whatsapp_for_the_whatsapp_number_and_prefers_the_call_lines(self):
        text, _ = g.personal_reply()
        self.assertIn("בטלפון: 3455*", text)
        self.assertIn("5432*", text)
        self.assertIn("בוואטסאפ 054-9893455", text)
        self.assertNotIn("בטלפון 054", text)
        self.assertLess(text.index("3455*"), text.index("054-9893455"))      # the call line comes first

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


_NUMBERS = re.compile(r"\d[\d\-*]*|\*\d+")
_LINKS = re.compile(r"https?://[^\s)]+")


def index_texts():
    """All chunk texts and chunk urls of the active index, or None when there is no index on this machine."""
    try:
        from index import build_index as bi
        col = bi.get_active_collection()
        got = col.get(include=["documents", "metadatas"])
    except Exception:  # noqa: BLE001
        return None
    return "\n".join(got["documents"]), {m["url"] for m in got["metadatas"]}


class TestPersonalReply(unittest.TestCase):
    def test_the_reply_has_the_three_parts(self):
        text, sources = g.personal_reply()
        self.assertIn("אין לי גישה למידע אישי", text)                    # (1)
        self.assertIn("אם כבר הוגשה תביעה", text)                          # (2)
        self.assertIn("באזור האישי", text)
        self.assertIn("שירות הלקוחות", text)
        self.assertIn("אם טרם הוגשה תביעה", text)                          # (3)
        self.assertIn("איך מגישים תביעה", text)
        self.assertEqual(sources[0]["url"], config.PERSONAL_FACTS_SOURCE["page_url"])

    def test_no_opening_hours(self):
        text, _ = g.personal_reply()
        self.assertNotRegex(text, r"\d{1,2}:\d{2}")
        self.assertNotIn("שעות", text)

    def test_every_number_and_link_comes_from_the_declared_facts(self):
        text, _ = g.personal_reply()
        facts = " ".join(config.PERSONAL_FACTS.values())
        for token in _NUMBERS.findall(text) + _LINKS.findall(text):
            self.assertIn(token.rstrip(".,"), facts, token)

    def test_every_number_and_link_is_in_the_index(self):
        data = index_texts()
        if data is None:
            self.skipTest("no active index on this machine")
        corpus, urls = data
        text, _ = g.personal_reply()
        for token in _NUMBERS.findall(text) + _LINKS.findall(text):
            token = token.rstrip(".,")
            self.assertTrue(token in corpus or token in urls, f"{token} is not in the index")

    def test_a_personal_question_gets_this_reply_kind(self):
        for q in ("האם יאשרו לי את הניתוח?", "מה מצב התביעה שלי?"):
            self.assertEqual(g.classify(q).kind, "personal", q)


if __name__ == "__main__":
    unittest.main()
