import unittest

import verify as v

SRC1 = ("כדי להגיש תביעה יש למלא את הטופס באתר או לשלוח הודעה למספר 054-9893455. "
        "הטיפול בתביעה נמשך עד 30 ימי עבודה. ניתן לפנות גם בטלפון 3455* או בכתובת https://www.fnx.co.il/claims-customer-service/ "
        "ולכתוב למייל Healthd@Fnx.co.il. הסכום המרבי הוא 5,000 ש\"ח.")
SRC2 = "ביטוח רכב: תביעה מוגשת דרך האתר בלבד, ותשובה מתקבלת תוך 14 ימים."
SOURCES = [{"n": 1, "text": SRC1}, {"n": 2, "text": SRC2}]


def cite(n, quote):
    return {"source": n, "quote": quote}


class TestQuotes(unittest.TestCase):
    def check(self, n, quote):
        return v.verify("", [cite(n, quote)], SOURCES).citations[0]

    def test_verbatim_quote_is_valid(self):
        c = self.check(1, "יש למלא את הטופס באתר או לשלוח הודעה")
        self.assertTrue(c.ok)
        self.assertEqual(c.source, 1)

    def test_punctuation_quotes_and_niqqud_are_ignored(self):
        self.assertTrue(self.check(1, "הטיפול בתביעה נמשך עד 30 ימי עבודה.").ok)
        self.assertTrue(self.check(1, "\"יש למלא\" את הטופס, באתר").ok)
        self.assertTrue(self.check(1, "יֵשׁ לְמַלֵּא אֶת הַטּוֹפֶס".replace("יֵשׁ לְמַלֵּא אֶת הַטּוֹפֶס", "יש למלא את הטופס")).ok)

    def test_ellipsis_is_rejected_even_if_both_parts_exist(self):
        for q in ("יש למלא את הטופס ... לשלוח הודעה", "יש למלא את הטופס… לשלוח הודעה", "[...] הטופס באתר"):
            c = self.check(1, q)
            self.assertEqual((c.ok, c.reason), (False, "ellipsis"), q)

    def test_changed_word_or_skipped_word_is_rejected(self):
        self.assertEqual(self.check(1, "יש למלא את הטופס בדואר").reason, "not_in_source")
        self.assertEqual(self.check(1, "יש לשלוח את הטופס באתר").reason, "not_in_source")     # not one contiguous run
        self.assertEqual(self.check(1, "יש את הטופס").reason, "not_in_source")

    def test_empty_short_and_bad_source_number(self):
        self.assertEqual(self.check(1, "").reason, "empty_quote")
        self.assertEqual(self.check(1, "הטופס").reason, "too_short")
        self.assertEqual(self.check(9, "יש למלא את הטופס").reason, "bad_source_number")
        out = v.verify("", [{"source": "x", "quote": "יש למלא"}, {"quote": "אין מקור"}], SOURCES)
        self.assertTrue(all(c.reason == "bad_source_number" for c in out.citations))

    def test_quote_from_another_source_is_reattributed_not_trusted(self):
        c = self.check(1, "תביעה מוגשת דרך האתר בלבד")           # really in source 2
        self.assertTrue(c.ok)
        self.assertEqual((c.n, c.source), (1, 2))
        self.assertIn("re-attributed", c.note)

    def test_long_quote_is_accepted_with_a_note(self):
        text = " ".join(f"מילה{i}" for i in range(30))
        out = v.verify("", [cite(1, text)], [{"n": 1, "text": text}])
        self.assertTrue(out.citations[0].ok)
        self.assertIn("30 words", out.citations[0].note)

    def test_used_sources_are_the_validly_cited_ones_only(self):
        out = v.verify("", [cite(1, "יש למלא את הטופס"), cite(2, "משהו שלא כתוב כאן בכלל")], SOURCES)
        self.assertEqual(out.used_sources, [1])


class TestTruncatedLastWord(unittest.TestCase):
    SRC = [{"n": 1, "text": "ולהוסיף כתובת דוא\"ל עדכנית. אחר כך שולחים את הטופס למוקד הארצי של החברה."}]

    def check(self, quote):
        return v.verify("", [cite(1, quote)], self.SRC).citations[0]

    def test_a_cut_last_word_that_is_a_prefix_passes(self):
        self.assertTrue(self.check("ולהוסיף כתובת דוא").ok)               # דוא from דוא"ל
        self.assertTrue(self.check("אחר כך שולחים את הטופס למו").ok)       # למו from למוקד

    def test_the_exact_quote_still_passes(self):
        self.assertTrue(self.check("ולהוסיף כתובת דוא\"ל עדכנית").ok)

    def test_a_changed_word_in_the_middle_is_rejected_even_if_the_last_word_is_cut(self):
        for quote in ("ולהוסיף כתובות דוא", "ולהוסיף כתובת אימייל דוא", "אחר כך שולחים את המסמך למו"):
            got = self.check(quote)
            self.assertEqual((got.ok, got.reason), (False, "not_in_source"), quote)

    def test_the_cut_word_must_be_a_prefix_of_at_least_two_characters(self):
        self.assertEqual(self.check("ולהוסיף כתובת ד").reason, "not_in_source")                 # 1 character
        self.assertEqual(self.check("ולהוסיף כתובת אל").reason, "not_in_source")                # not a prefix
        self.assertEqual(self.check("ולהוסיף כתובת ל\"ל").reason, "not_in_source")               # a suffix, not a prefix

    def test_only_the_last_word_may_be_cut_not_the_first_or_a_middle_one(self):
        self.assertEqual(self.check("להוסיף כתובת דוא\"ל").reason, "not_in_source")             # first word cut (source has ולהוסיף)
        self.assertEqual(self.check("ולהוסיף כתו דוא\"ל").reason, "not_in_source")

    def test_a_single_word_quote_cannot_be_a_prefix(self):
        self.assertEqual(self.check("דוא").reason, "too_short")
        src = [{"n": 1, "text": "דוא\"ל עדכני"}]
        self.assertFalse(v.verify_claims([{"text": "דוא", "source": 1, "quote": "דו"}], src).claims[0].ok)

    def test_the_live_case(self):
        src = [{"n": 1, "section": "x", "title": "y",
                "text": "לפני הגשת התביעה יש לוודא שברשותך המסמכים הבאים: טופס תביעה מלא הכולל פירוט סכום הקבלה ומספר קבלה, כתובת דוא\"ל עדכנית."}]
        quote = "לפני הגשת התביעה יש לוודא שברשותך המסמכים הבאים טופס תביעה מלא הכולל פירוט סכום הקבלה ומספר קבלה כתובת דוא"
        self.assertTrue(v.verify_claims([{"text": "יש לוודא טופס תביעה מלא הכולל פירוט סכום הקבלה ומספר קבלה", "source": 1, "quote": quote}], src).claims[0].ok)


class TestNumbersAndLinks(unittest.TestCase):
    def run_check(self, answer, quote="יש למלא את הטופס באתר", n=1):
        return v.verify(answer, [cite(n, quote)], SOURCES)

    def test_values_that_are_in_the_cited_source_pass(self):
        out = self.run_check("הטיפול נמשך עד 30 ימי עבודה, ניתן לשלוח ל-054-9893455 או להתקשר 3455*. הסכום עד 5,000 ש\"ח. "
                             "הכתובת https://www.fnx.co.il/claims-customer-service/ והמייל healthd@fnx.co.il")
        self.assertEqual((out.unsupported_numbers, out.unsupported_links, out.hint), ([], [], "ok"))

    def test_invented_number_is_reported(self):
        out = self.run_check("הטיפול נמשך עד 45 ימי עבודה")
        self.assertEqual(out.unsupported_numbers, ["45"])
        self.assertEqual(out.hint, "unsupported_values")

    def test_number_that_is_only_in_an_uncited_source_is_reported(self):
        out = self.run_check("התשובה מתקבלת תוך 14 ימים")          # 14 is in source 2, not cited
        self.assertEqual(out.unsupported_numbers, ["14"])
        out2 = v.verify("התשובה מתקבלת תוך 14 ימים", [cite(1, "יש למלא את הטופס"), cite(2, "ותשובה מתקבלת תוך 14 ימים")], SOURCES)
        self.assertEqual(out2.unsupported_numbers, [])

    def test_phone_in_another_format_is_still_the_same_phone(self):
        self.assertEqual(self.run_check("למספר 0549893455").unsupported_numbers, [])
        self.assertEqual(self.run_check("למספר 054-9893456").unsupported_numbers, ["054-9893456"])

    def test_thousands_separator(self):
        self.assertEqual(self.run_check("עד 5000 ש\"ח").unsupported_numbers, [])
        self.assertEqual(self.run_check("עד 6,000 ש\"ח").unsupported_numbers, ["6000"])

    def test_invented_link_and_email(self):
        out = self.run_check("ראו https://www.fnx.co.il/other/ ושלחו ל-fake@fnx.co.il")
        self.assertEqual(out.unsupported_links, ["https://www.fnx.co.il/other", "fake@fnx.co.il"])

    def test_link_trailing_slash_and_scheme_do_not_matter(self):
        self.assertEqual(self.run_check("ראו https://www.fnx.co.il/claims-customer-service").unsupported_links, [])
        self.assertEqual(self.run_check("ראו http://www.fnx.co.il/claims-customer-service/.").unsupported_links, [])

    def test_numbers_inside_a_link_are_not_checked_as_numbers(self):
        src = [{"n": 1, "text": "לפתיחת טופס היכנסו לכתובת https://forms.fnx.co.il/form?f=2000311 וצרפו מסמכים"}]
        out = v.verify("היכנסו לכתובת https://forms.fnx.co.il/form?f=2000311", [cite(1, "לפתיחת טופס היכנסו לכתובת")], src)
        self.assertEqual((out.unsupported_numbers, out.unsupported_links), ([], []))

    def test_list_numbering_in_the_answer_is_not_a_number(self):
        out = self.run_check("1. ממלאים טופס\n2. שולחים אותו")
        self.assertEqual(out.unsupported_numbers, [])

    def test_no_valid_citation_is_its_own_hint_even_when_values_are_fine(self):
        out = v.verify("הטיפול נמשך עד 30 ימי עבודה", [cite(1, "ציטוט שלא קיים במקור הזה")], SOURCES)
        self.assertEqual((out.hint, out.ok), ("no_valid_citation", False))
        out2 = v.verify("הטיפול נמשך עד 30 ימי עבודה", [], SOURCES)
        self.assertEqual(out2.hint, "no_valid_citation")

    def test_an_answer_without_values_and_a_good_citation_is_ok(self):
        out = self.run_check("יש למלא טופס באתר")
        self.assertTrue(out.ok)
        self.assertEqual(out.hint, "ok")


class TestWithThePromptSources(unittest.TestCase):
    def test_works_on_prompt_build_sources(self):
        import prompt
        from bot import retrieve as r
        g = r.Group(text="עמוד > איך מגישים תביעה?\nיש למלא את הטופס באתר ולצרף אישור עד 30 ימים",
                    url="https://www.fnx.co.il/health-insurance/x/", anchor="", page_title="עמוד",
                    section_title="איך מגישים תביעה?", topic="health-insurance", topic_name="ביטוח בריאות",
                    distance=0.2, short=False)
        built = prompt.build("שאלה", [g])
        out = v.verify("יש להגיש עד 30 ימים", [cite(1, "ולצרף אישור עד 30 ימים")], built.sources)
        self.assertTrue(out.ok, out)


if __name__ == "__main__":
    unittest.main()
