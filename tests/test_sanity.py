import json
import unittest
from pathlib import Path

from index import sanity

CASES_FILE = Path(__file__).resolve().parent.parent / "eval" / "sanity_cases.json"


def chunk(url, section, text="גוף", dup_urls=""):
    return {"url": url, "section_title": section, "text": f"עמוד > {section}\n{text}", "dup_urls": dup_urls}


class TestResolve(unittest.TestCase):
    CHUNKS = [chunk("u1", "איך מגישים תביעה?", "טופס דיגיטלי"),
              chunk("u1", "איך מגישים תביעה?", "ניתוח דחוף"),
              chunk("u2", "מידע נוסף", dup_urls="u3|u4")]

    def case(self, *entries):
        return {"id": "t", "acceptable": list(entries)}

    def test_text_contains_disambiguates_repeated_titles(self):
        c = self.case({"role": "primary", "url": "u1", "section_title": "איך מגישים", "text_contains": "דחוף"})
        self.assertEqual(sanity.resolve_case(c, self.CHUNKS), [("primary", 1)])

    def test_ambiguous_entry_is_an_error(self):
        with self.assertRaises(sanity.CaseError):
            sanity.resolve_case(self.case({"url": "u1", "section_title": "איך מגישים"}), self.CHUNKS)

    def test_missing_entry_is_an_error(self):
        with self.assertRaises(sanity.CaseError):
            sanity.resolve_case(self.case({"url": "u9", "section_title": "x"}), self.CHUNKS)

    def test_entry_matches_a_merged_duplicate_by_dup_urls(self):
        c = self.case({"url": "u3", "section_title": "מידע נוסף"})
        self.assertEqual(sanity.resolve_case(c, self.CHUNKS), [("secondary", 2)])


class TestScoring(unittest.TestCase):
    # three chunks as orthogonal unit vectors; sample[k] is the chunk index of doc_vecs[k]
    DOCS = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    SAMPLE = [10, 11, 12]

    def case(self, metric, acceptable_idx_roles, type_="ambiguous", sep=True):
        return {"id": "c", "type": type_, "metric": metric, "include_in_separation": sep}, acceptable_idx_roles

    def run_case(self, metric, resolved, q_vec, type_="ambiguous", sep=True):
        case = {"id": "c", "type": type_, "metric": metric, "include_in_separation": sep}
        return sanity.evaluate_case(case, resolved, self.SAMPLE, q_vec, self.DOCS)

    def test_top3_any_acceptable_chunk_counts(self):
        r = self.run_case("top3", [("primary", 10), ("secondary", 11)], [0.1, 1, 0])  # nearest is chunk 11
        self.assertTrue(r["hit"])
        self.assertEqual(r["rank"], 1)

    def test_top1_only_primary_counts(self):
        # the query is closest to the secondary chunk, so a top-1 case must fail
        r = self.run_case("top1", [("primary", 10), ("secondary", 11)], [0.2, 1, 0])
        self.assertFalse(r["hit"])
        self.assertEqual(r["rank"], 2)

    def test_top1_primary_first_is_a_hit(self):
        self.assertTrue(self.run_case("top1", [("primary", 10)], [1, 0.1, 0])["hit"])

    def test_top3_hit_when_target_is_third(self):
        r = self.run_case("top3", [("primary", 12)], [1, 0.6, 0.3])
        self.assertEqual(r["rank"], 3)
        self.assertTrue(r["hit"])

    def test_coverage_case_is_excluded_from_separation(self):
        near = self.run_case("top3", [("primary", 10)], [1, 0, 0])
        far_coverage = self.run_case("top1", [("primary", 10)], [0, 1, 0], type_="coverage", sep=False)  # misses
        self.assertFalse(far_coverage["in_separation"])
        s = sanity.summarize([near, far_coverage], [{"distance": 0.5}])
        self.assertEqual(s["separation_cases"], 1)
        self.assertTrue(s["separated"])                 # the bad coverage case does not break the separation
        self.assertEqual((s["hits"], s["cases"]), (1, 2))  # but it is still counted in the hit rate

    def test_overlapping_ranges_are_reported(self):
        ok = self.run_case("top3", [("primary", 10)], [1, 1, 0])
        s = sanity.summarize([ok], [{"distance": 0.1}])
        self.assertFalse(s["separated"])

    def test_small_separation_set_is_flagged(self):
        r = self.run_case("top3", [("primary", 10)], [1, 0, 0])
        self.assertTrue(sanity.summarize([r, r], [{"distance": 0.9}])["small_separation_set"])

    def test_negative_reports_its_nearest_chunk(self):
        n = sanity.evaluate_negative([0, 0.2, 1], self.DOCS)
        self.assertEqual(n["nearest"], 2)


class TestCasesFile(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(CASES_FILE.read_text(encoding="utf-8"))

    def test_five_owner_queries_present_and_unchanged(self):
        queries = {c["id"]: c["query"] for c in self.data["cases"]}
        self.assertEqual(queries["q1"], "יש לי ניתוח, מה אני צריך לעשות בשביל זה?")
        self.assertEqual(queries["q2"], "מה ההחזר שיש לי על דיקור סיני?")
        self.assertEqual(queries["q3"], "לייעוץ הרופא, מה אני צריך לצרף?")
        self.assertEqual(queries["q4"], "מה הכיסוי שלי על תרופות?")
        self.assertEqual(queries["q5"], "מחלה קשה, ומה אני צריך לצרף בשביל התביעה?")

    def test_coverage_questions_marked_and_excluded(self):
        by_id = {c["id"]: c for c in self.data["cases"]}
        for qid in ("q2", "q4"):
            self.assertEqual(by_id[qid]["type"], "coverage")
            self.assertFalse(by_id[qid]["include_in_separation"])
        for qid in ("q1", "q3", "q5"):
            self.assertTrue(by_id[qid]["include_in_separation"])

    def test_metrics(self):
        by_id = {c["id"]: c for c in self.data["cases"]}
        self.assertEqual(by_id["q5"]["metric"], "top1")
        for qid in ("q1", "q2", "q3", "q4"):
            self.assertEqual(by_id[qid]["metric"], "top3")

    def test_every_case_has_a_primary_and_unique_id(self):
        ids = [c["id"] for c in self.data["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.data["cases"]:
            self.assertTrue(any(a["role"] == "primary" for a in c["acceptable"]), c["id"])

    def test_near_topic_negatives_present(self):
        near = [n for n in self.data["negatives"] if n["kind"] == "near_topic"]
        self.assertGreaterEqual(len(near), 4)
        # the car / apartment questions are answered by the crawled site and must not be negatives any more
        self.assertFalse(any("תאונת דרכים" in n["query"] or "ביטוח חובה" in n["query"] or "ביטוח דירה" in n["query"] for n in near))
        self.assertFalse(any(n.get("valid_until_crawled") for n in near))


if __name__ == "__main__":
    unittest.main()
