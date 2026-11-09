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


if __name__ == "__main__":
    unittest.main()
