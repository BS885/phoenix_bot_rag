import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"answered", "partial", "not_found", "out_of_scope", "personal", "advice", "greeting", "clarify", "error"}


class TestAnswerCases(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "eval" / "answer_cases.json").read_text(encoding="utf-8"))

    def test_every_case_has_a_query_and_known_statuses(self):
        for c in self.data["cases"]:
            self.assertTrue(c["query"].strip())
            self.assertTrue(c["expected_status"])
            self.assertTrue(set(c["expected_status"]) <= STATUSES, c["id"])

    def test_the_doctor_consultation_question_is_permanent_and_partial_or_clarify(self):
        case = next(c for c in self.data["cases"] if c["query"] == "לייעוץ הרופא, מה אני צריך לצרף?")
        self.assertEqual(set(case["expected_status"]), {"partial", "clarify"})
        sanity = json.loads((ROOT / "eval" / "sanity_cases.json").read_text(encoding="utf-8"))
        self.assertIn(case["query"], [c["query"] for c in sanity["cases"]])   # the same wording as q3


class TestA1(unittest.TestCase):
    def test_a1_requires_both_stages(self):
        import json
        cases = json.loads((ROOT / "eval" / "answer_cases.json").read_text(encoding="utf-8"))["cases"]
        a1 = next(c for c in cases if c["id"] == "a1")
        self.assertEqual(a1["query"], "יש לי ניתוח, מה אני צריך לעשות בשביל זה?")
        self.assertEqual(len(a1["must_mention"]), 2)
        before, urgent = a1["must_mention"]
        for alt in ("אישור לניתוח עתידי", "surgeryclaim.fnx.co.il", "בטרם ביצוע הניתוח"):
            self.assertIn(alt, before["any_of"])
        self.assertEqual(urgent["any_of"], ["3455*"])
        self.assertTrue(all(m["any_of"] for m in a1["must_mention"]))


class TestConversations(unittest.TestCase):
    def test_two_doctor_conversations_are_permanent_cases(self):
        import json
        convs = json.loads((ROOT / "eval" / "answer_cases.json").read_text(encoding="utf-8"))["conversations"]
        self.assertEqual([c["id"] for c in convs], ["c1", "c2"])
        c1, c2 = convs
        self.assertEqual([t["user"] for t in c1["turns"]], ["מה זה כיסוי אמבולטורי?", "יש שם ייעוץ עם רופא?"])
        self.assertEqual(c1["turns"][1]["expected_status"], ["answered"])
        self.assertEqual(len(c1["turns"][1]["must_mention"]), 2)
        self.assertEqual([t["user"] for t in c2["turns"]], ["מה הכיסוי לייעוץ עם רופא?", "ואם אני רוצה רופא שלא בהסדר?"])
        self.assertGreaterEqual(c2["turns"][0]["min_distinct_services"], 2)
        self.assertIn("not_found", c2["turns"][1]["forbidden_status"])
        for t in c2["turns"]:
            self.assertTrue(set(t["forbidden_words"]) >= {"מומלץ", "עדיף"})
            self.assertTrue(set(t["expected_status"]) <= STATUSES)


if __name__ == "__main__":
    unittest.main()
