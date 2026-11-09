import unittest

import config
import prompt as p
import verify as v

SRC1 = ("כדי להגיש תביעה יש למלא את הטופס באתר או לשלוח הודעה למספר 054-9893455. "
        "הטיפול בתביעה נמשך עד 30 ימי עבודה. ניתן לפנות גם בטלפון 3455* או בכתובת https://www.fnx.co.il/claims-customer-service/")
LINK_SRC = "הגשת תביעה (https://surgeryclaim.fnx.co.il/surgery/home)"
SOURCES = [{"n": 1, "text": SRC1, "section": "איך מגישים תביעה?", "title": "הגשת תביעה"},
           {"n": 2, "text": LINK_SRC, "section": "אישור לניתוח עתידי", "title": "להגיש תביעה"}]


def c(text, quote, source=1):
    return {"text": text, "source": source, "quote": quote}


def one(text, quote, source=1):
    res = v.verify_claims([c(text, quote, source)], SOURCES)
    return res.claims[0]


class TestSingularForms(unittest.TestCase):
    def test_forms_with_a_final_kaf_and_fixed_forms_are_found(self):
        for form in ("עליך", "בפניך", "שברשותך", "לך", "אליך", "שלך", "ולך", "ופנייתך"):
            self.assertEqual(v.singular_forms(f"יש לפנות {form} בהקדם"), [form], form)
        self.assertIn("אתה", v.singular_forms("אתה יכול להגיש"))

    def test_ordinary_words_ending_in_kaf_are_not_flagged(self):
        for ok in ("דרך האתר", "תוך שבוע", "בתוך שבוע", "מתוך הטופס", "המשך הטיפול", "במשך שנה", "כך עושים", "אך לא תמיד",
                   "אורך התקופה", "מערך השירות", "יש צורך באישור", "תהליך הגשת התביעה", "הוגשה עד תאריך מסוים",
                   "ותאריך היעד", "מסלול ארוך", "הליך מסודר", "ממשיך לפעול"):
            self.assertEqual(v.singular_forms(ok), [], ok)

    def test_plural_and_impersonal_text_is_clean(self):
        self.assertEqual(v.singular_forms("ניתן להגיש תביעה, ומומלץ לצרף מסמכים. אפשר לפנות לשירות הלקוחות."), [])

    def test_the_quote_may_copy_the_singular_but_the_text_may_not(self):
        quote = "עליך לפנות אלינו בטרם ביצוע הניתוח"
        src = [{"n": 1, "text": f"{quote}. עוד מידע כללי על הפוליסה.", "section": "x", "title": "y"}]
        ok = v.verify_claims([c("יש לפנות לחברה לפני ביצוע הניתוח", quote)], src).claims[0]
        self.assertTrue(ok.ok)
        bad = v.verify_claims([c("עליך לפנות לחברה לפני ביצוע הניתוח", quote)], src).claims[0]
        self.assertEqual((bad.ok, bad.reason), (False, "singular_form"))


class TestOverlap(unittest.TestCase):
    def test_threshold_lives_in_config(self):
        self.assertGreater(config.CLAIM_MIN_OVERLAP, 0)
        self.assertLess(config.CLAIM_MIN_OVERLAP, 1)

    def test_a_paraphrase_with_prefixes_still_overlaps(self):
        self.assertGreaterEqual(v.overlap("ניתן להגיש תביעה בטופס דיגיטלי", "אפשר למלא טופס תביעה דיגיטלי"), 0.5)

    def test_unrelated_text_does_not(self):
        self.assertLess(v.overlap("הפניקס חברה גדולה ויציבה", "יש למלא את הטופס באתר"), config.CLAIM_MIN_OVERLAP)

    def test_a_low_overlap_claim_is_rejected_and_a_matching_one_passes(self):
        self.assertEqual(one("הפניקס חברה גדולה ויציבה בענף", "יש למלא את הטופס באתר").reason, "low_overlap")
        self.assertTrue(one("יש למלא את הטופס באתר", "יש למלא את הטופס באתר").ok)

    def test_threshold_can_be_overridden(self):
        res = v.verify_claims([c("יש למלא טופס ולשלוח אותו בדואר רשום לכתובת הסניף", "יש למלא את הטופס באתר")], SOURCES,
                              min_overlap=0.9)
        self.assertEqual(res.claims[0].reason, "low_overlap")


class TestValuesInTheClaimText(unittest.TestCase):
    def test_values_in_the_quote_or_the_source_pass(self):
        self.assertTrue(one("הטיפול נמשך עד 30 ימי עבודה", "הטיפול בתביעה נמשך עד 30 ימי עבודה").ok)
        # 3455* is in the source, not in the quote
        self.assertTrue(one("הטיפול בתביעה נמשך עד 30 ימי עבודה, טלפון 3455*", "הטיפול בתביעה נמשך עד 30 ימי עבודה").ok)

    def test_an_invented_number_phone_or_link_fails_the_claim(self):
        for text in ("הטיפול בתביעה נמשך עד 45 ימי עבודה", "הטיפול בתביעה נמשך עד 30 ימי עבודה, טלפון 5432*",
                     "הטיפול בתביעה נמשך עד 30 ימי עבודה, ראו https://other.example.com/x"):
            got = one(text, "הטיפול בתביעה נמשך עד 30 ימי עבודה")
            self.assertEqual((got.ok, got.reason), (False, "unsupported_value"), text)

    def test_a_value_from_another_source_than_the_claims_own_fails(self):
        got = one("הטיפול בתביעה נמשך עד 30 ימי עבודה, ראו https://surgeryclaim.fnx.co.il/surgery/home",
                  "הטיפול בתביעה נמשך עד 30 ימי עבודה")
        self.assertEqual(got.reason, "unsupported_value")


class TestHeadingAndLinkSources(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(v.is_link_only(LINK_SRC))
        self.assertFalse(v.is_link_only(SRC1))
        self.assertFalse(v.is_link_only("טקסט קצר בלי קישור"))

    def test_the_section_title_is_a_valid_quote_and_the_link_is_added_from_the_source(self):
        got = one("לניתוח עתידי ניתן לבקש אישור מראש", "אישור לניתוח עתידי", 2)
        self.assertTrue(got.ok, got)
        self.assertEqual(got.final_text, "לניתוח עתידי ניתן לבקש אישור מראש (https://surgeryclaim.fnx.co.il/surgery/home)")

    def test_a_single_word_title_is_enough_for_a_link_only_source(self):
        src = [{"n": 1, "text": "אפליקציה (https://www.fnx.co.il/customer-service/app/)", "section": "אפליקציה", "title": "שירות"}]
        got = v.verify_claims([c("אפשר להוריד אפליקציה", "אפליקציה")], src).claims[0]
        self.assertTrue(got.ok, got)

    def test_the_page_title_works_too_and_a_made_up_quote_does_not(self):
        self.assertTrue(one("אפשר להגיש תביעה בקישור", "להגיש תביעה", 2).ok)
        self.assertEqual(one("אפשר להגיש תביעה בקישור", "בקשה לאישור טלפוני מיידי", 2).reason, "not_in_source")

    def test_an_ordinary_source_cannot_be_quoted_by_its_title(self):
        got = one("אפשר להגיש תביעה", "איך מגישים תביעה?", 1)         # the section title, but the source has body text
        self.assertFalse(got.ok)


class TestOneByOne(unittest.TestCase):
    def test_a_bad_claim_does_not_take_the_good_ones_down(self):
        res = v.verify_claims([c("יש למלא את הטופס באתר", "יש למלא את הטופס באתר"),
                               c("עליך לפנות", "יש למלא את הטופס באתר"),
                               c("הטיפול נמשך עד 30 ימי עבודה", "הטיפול בתביעה נמשך עד 30 ימי עבודה")], SOURCES)
        self.assertEqual([x.ok for x in res.claims], [True, False, True])
        self.assertEqual(res.used_sources, [1])
        self.assertEqual(len(res.dropped), 1)

    def test_bad_input_is_dropped_not_raised(self):
        res = v.verify_claims([{"text": "טקסט", "source": "x", "quote": "יש למלא"}, {"text": "", "source": 1, "quote": "a b"},
                               "not a dict", c("יש למלא את הטופס באתר", "יש למלא את הטופס באתר")], SOURCES)
        self.assertEqual([x.ok for x in res.claims], [False, False, True])

    def test_duplicate_claims_are_kept_once(self):
        same = c("יש למלא את הטופס באתר", "יש למלא את הטופס באתר")
        self.assertEqual(len(v.verify_claims([same, dict(same)], SOURCES).claims), 1)

    def test_ellipsis_and_reattribution_still_apply(self):
        self.assertEqual(one("יש למלא את הטופס", "יש למלא ... באתר").reason, "ellipsis")
        got = one("אפשר להגיש תביעה", "הטיפול בתביעה נמשך עד 30 ימי עבודה", 2)
        self.assertTrue(got.ok)
        self.assertEqual(got.source, 1)


class TestPromptSchema(unittest.TestCase):
    def test_schema_and_hint_use_claims(self):
        self.assertIn("claims", p.SCHEMA["required"])
        self.assertNotIn("answer", p.SCHEMA["properties"])
        self.assertEqual(set(p.SCHEMA["properties"]["claims"]["items"]["required"]), {"text", "source", "quote"})
        self.assertIn('"claims"', p.FORMAT_HINT)

    def test_rules_7_and_8_describe_the_claims_contract(self):
        rule = {n: next(l for l in p.SYSTEM.splitlines() if l.startswith(f"{n}. ")) for n in (7, 8)}
        for needle in ('"text"', "בלשון רבים או סתמית", "עליך", "בפניך", "שברשותך", 'רק שדה "quote" מעתיק'):
            self.assertIn(needle, rule[7])
        for needle in ('"claims"', '"text"', '"source"', '"quote"', "מילה במילה", "כותרת וקישור בלבד", "הכותרת היא הציטוט"):
            self.assertIn(needle, rule[8])


if __name__ == "__main__":
    unittest.main()
