import re
import unittest

import answer as a
import config
import guardrails as g
import privacy as p

ID, PHONE, MAIL = config.PII_PLACEHOLDERS["id"], config.PII_PLACEHOLDERS["phone"], config.PII_PLACEHOLDERS["email"]
# All numbers below are made up.


class TestMasking(unittest.TestCase):
    def test_mobile_numbers_in_every_common_shape(self):
        for raw in ("054-1234567", "0541234567", "054 123 4567", "054-123-4567", "+972-54-123-4567", "972541234567",
                    "+972 54 1234567", "050-7654321"):
            out = p.mask(f"אפשר להתקשר ל-{raw} מחר")
            self.assertEqual(out.text, f"אפשר להתקשר ל-{PHONE} מחר", raw)
            self.assertEqual(out.found["phone"], 1, raw)

    def test_landline_numbers(self):
        for raw in ("03-1234567", "03-123-4567", "02 1234567", "+972-3-123-4567", "073-1234567" if False else "04-9876543"):
            self.assertEqual(p.mask(f"הטלפון {raw}").text, f"הטלפון {PHONE}", raw)

    def test_nine_digit_id(self):
        out = p.mask("מספר הזהות שלי 123456789 מה קורה")
        self.assertEqual(out.text, f"מספר הזהות שלי {ID} מה קורה")
        self.assertEqual(out.found["id"], 1)
        self.assertEqual(p.mask("012345678").text, ID)

    def test_id_after_a_cue_word_with_seven_or_eight_digits(self):
        for raw in ('ת"ז 1234567', "ת.ז. 12345678", "תעודת זהות: 7654321", "זהות 87654321"):
            out = p.mask(raw)
            self.assertIn(ID, out.text, raw)
            self.assertNotRegex(out.text, r"\d{5}", raw)

    def test_email(self):
        out = p.mask("שלחו ל-someone.name+x@example.co.il בבקשה")
        self.assertEqual(out.text, f"שלחו ל-{MAIL} בבקשה")
        self.assertEqual(out.found["email"], 1)

    def test_several_in_one_text_and_counts(self):
        out = p.mask("ת״ז 123456789, נייד 0541234567, מייל a.b@c.com ושוב 0521112222")
        self.assertEqual(out.found, {"id": 1, "phone": 2, "email": 1})
        self.assertNotRegex(out.text, r"\d{7}")
        self.assertTrue(out.any)

    def test_the_site_short_numbers_and_ordinary_numbers_are_left_alone(self):
        for text in ("אפשר להתקשר ל-3455* או ל-5432*", "המספר *8888 זמין", "הטיפול נמשך עד 30 ימי עבודה",
                     "הסכום 5,000 ₪ ו-1500 ₪", "בשנת 2026 בגיל 67", "קוד 12345", "מיקוד 7510001"):
            out = p.mask(text)
            self.assertEqual(out.text, text, text)
            self.assertFalse(out.any, text)

    def test_digits_glued_to_other_digits_or_words_are_not_an_id(self):
        self.assertEqual(p.mask("1234567890123").text, "1234567890123")       # 13 digits: not a 9-digit ID
        self.assertEqual(p.mask("abc123456789def").text, "abc123456789def")

    def test_masking_is_idempotent_and_safe_on_empty(self):
        once = p.mask("ת״ז 123456789 טלפון 0541234567").text
        self.assertEqual(p.mask(once).text, once)
        self.assertEqual(p.mask("").text, "")
        self.assertEqual(p.mask(None).text, "")

    def test_history_is_masked_and_other_keys_survive(self):
        hist = [{"role": "user", "text": "הטלפון שלי 0541234567", "status": None}, {"role": "bot", "text": "שלום", "status": "greeting"}]
        out = p.mask_history(hist)
        self.assertEqual(out[0]["text"], f"הטלפון שלי {PHONE}")
        self.assertEqual(out[1], hist[1])
        self.assertIn("0541234567", hist[0]["text"])                             # the input is not changed
        self.assertEqual(p.mask_history(None), [])


class TestGuardrailsSeeThePlaceholders(unittest.TestCase):
    def test_a_masked_id_or_phone_is_a_pure_personal_question(self):
        for q in (f"מספר הזהות שלי {ID} מה קורה", f"אני בטלפון {PHONE} מה מצב העניין", f"{ID} מה מצב התביעה"):
            self.assertEqual(g.classify(q).kind, "personal", q)

    def test_it_matches_what_the_mask_produces(self):
        masked = p.mask("מספר הזהות שלי 123456789 מה קורה").text
        self.assertEqual(g.classify(masked).kind, "personal")
        self.assertEqual(g.classify(p.mask("מה קורה עם הנייד 0541234567").text).kind, "personal")

    def test_a_how_to_question_with_a_placeholder_is_only_suspected_personal(self):
        got = g.classify(f"איך מעדכנים את הטלפון {PHONE} בפוליסה?")
        self.assertEqual((got.kind, got.suspected_personal), ("none", True))

    def test_email_placeholder_raises_only_the_suspected_flag(self):
        got = g.classify(f"איך מקבלים אישור למייל {MAIL}?")
        self.assertEqual((got.kind, got.suspected_personal), ("none", True))

    def test_plain_questions_are_unchanged(self):
        self.assertEqual(g.classify("איך מגישים תביעה?").kind, "none")
        self.assertFalse(g.classify("איך מגישים תביעה?").suspected_personal)


class RecordingGen:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt, schema, *, system=None, model=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class SpyRetriever:
    def __init__(self):
        self.questions = []

    def retrieve(self, question, products=None):
        self.questions.append(question)
        raise AssertionError("must not be reached for a personal question")


class TestNothingRawReachesTheApis(unittest.TestCase):
    RAW = ("123456789", "0541234567", "someone@example.com")

    def test_a_personal_question_is_answered_without_any_call_and_without_the_raw_values(self):
        gen = RecordingGen()
        out = a.answer("מספר הזהות שלי 123456789, מה מצב התביעה? הטלפון 0541234567", None, retriever=SpyRetriever(), generate=gen)
        self.assertEqual((out.status, gen.prompts), ("personal", []))
        self.assertEqual(out.debug["pii_masked"], {"id": 1, "phone": 1, "email": 0})

    def test_the_retriever_the_rewrite_and_the_answer_prompt_get_only_masked_text(self):
        seen = {}

        class Ret:
            def retrieve(self, question, products=None):
                seen["question"] = question
                from bot import retrieve as r
                hit = {"id": "i", "text": "הגשת תביעה > איך מגישים תביעה?\nיש למלא את הטופס באתר ולשלוח אותו. " * 3,
                       "url": "https://www.fnx.co.il/claims-customer-service/claims-health-insurance/x/",
                       "page_title": "הגשת תביעה", "section_title": "איך מגישים תביעה?", "anchor": "", "distance": 0.2,
                       "short": False, "dup_urls": "", "section_id": "s1"}
                return r.process_hits(question, [hit], products=products)

        hist = [{"role": "user", "text": "הנייד שלי 0541234567 ואני מבוטח בבריאות", "status": None},
                {"role": "bot", "text": "בסדר", "status": "answered"}]
        reply = {"found": True, "claims": [{"text": "יש למלא את הטופס באתר", "source": 1, "quote": "יש למלא את הטופס באתר"}],
                 "is_coverage": False, "is_personal": False, "out_of_scope": False, "missing": ""}
        gen = RecordingGen({"question": f"איך מגישים תביעה בביטוח בריאות? מייל {MAIL}"}, reply)
        out = a.answer("ומה לצרף? המייל שלי someone@example.com", hist, retriever=Ret(), generate=gen)
        everything = " ".join(gen.prompts) + " " + seen["question"]          # what is sent out (inputs), not the fake model's output
        for raw in self.RAW:
            self.assertNotIn(raw, everything, raw)
        self.assertIn(MAIL, seen["question"])
        self.assertEqual(out.debug["pii_masked"]["email"], 1)


if __name__ == "__main__":
    unittest.main()
