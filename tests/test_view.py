import unittest

import config
import view as v
from answer import BotReply
from bot import retrieve

H = "https://www.fnx.co.il"


def src(path="/claims-customer-service/claims-health-insurance/x/", title="הגשת תביעה", anchor="how",
        date="2026-11-04T10:00:00+00:00"):
    return {"title": title, "url": H + path, "anchor": anchor, "collected_at": date}


def reply(status="answered", text="יש למלא את הטופס באתר.", sources=None, missing="", **debug):
    return BotReply(status, text, sources=sources if sources is not None else [src()], missing=missing, debug=debug)


class TestSources(unittest.TestCase):
    def test_link_with_anchor_and_collection_date(self):
        vm = v.to_view(reply())
        self.assertEqual(len(vm.sources), 1)
        self.assertEqual(vm.sources[0].url, H + "/claims-customer-service/claims-health-insurance/x/#how")
        self.assertEqual(vm.sources[0].date, "04.11.2026")
        self.assertEqual(vm.sources[0].markdown(), f"[הגשת תביעה]({H}/claims-customer-service/claims-health-insurance/x/#how) · נאסף: 04.11.2026")
        self.assertEqual(vm.sources_note, config.SOURCES_NOTE)

    def test_no_anchor_no_date(self):
        vm = v.to_view(reply(sources=[src(anchor="", date="")]))
        self.assertEqual(vm.sources[0].url, H + "/claims-customer-service/claims-health-insurance/x/")
        self.assertEqual(vm.sources[0].markdown(), f"[הגשת תביעה]({vm.sources[0].url})")

    def test_only_http_links_are_ever_linked(self):
        bad = [{"title": "x", "url": "javascript:alert(1)"}, {"title": "y", "url": "data:text/html,<b>"},
               {"title": "z", "url": "/relative/path"}, {"title": "w", "url": ""}, src()]
        vm = v.to_view(reply(sources=bad))
        self.assertEqual([s.url.startswith(("http://", "https://")) for s in vm.sources], [True])

    def test_titles_cannot_break_the_markdown_link(self):
        vm = v.to_view(reply(sources=[src(title="כותרת [עם] (סוגריים) *וכוכבית*")]))
        md = vm.sources[0].markdown()
        self.assertTrue(md.startswith("[כותרת \\[עם\\] (סוגריים) \\*וכוכבית\\*]("), md)

    def test_date_formats(self):
        self.assertEqual(v.format_date("2026-11-04T10:00:00+00:00"), "04.11.2026")
        self.assertEqual(v.format_date("2026-11-04"), "04.11.2026")
        self.assertEqual(v.format_date("2026-11-04T10:00:00Z"), "04.11.2026")
        for bad in ("", "garbage", None):
            self.assertEqual(v.format_date(bad), "")

    def test_statuses_without_sources_show_none(self):
        for status in ("not_found", "out_of_scope", "advice", "greeting", "clarify", "error"):
            vm = v.to_view(reply(status, "טקסט", sources=[src()]))
            self.assertEqual((vm.sources, vm.sources_note), ([], ""), status)

    def test_personal_shows_its_source(self):
        vm = v.to_view(reply("personal", "x", sources=[{"title": "כל הדרכים ליצור איתנו קשר", "url": H + "/claims-customer-service/be-in-touch/",
                                                       "anchor": "", "collected_at": ""}]))
        self.assertEqual(len(vm.sources), 1)
        self.assertEqual(vm.sources[0].date, "")


class TestMissingAndNotes(unittest.TestCase):
    def test_missing_is_replaced_by_the_fixed_note_and_the_model_text_never_reaches_the_screen(self):
        model_text = "המקורות אינם מציינים אילו מסמכים לצרף"
        vm = v.to_view(reply("partial", missing=model_text))
        self.assertEqual(vm.notes, [config.MISSING_NOTE])
        self.assertNotIn(model_text, vm.body)
        self.assertNotIn(model_text, " ".join(vm.notes))
        self.assertEqual(config.MISSING_NOTE, "ייתכן שחלק מהפרטים שנשאלו אינם מופיעים במידע שאליו יש לי גישה. ניתן לפנות לשירות הלקוחות להשלמה.")

    def test_no_note_without_missing_or_for_other_statuses(self):
        self.assertEqual(v.to_view(reply("partial", missing="")).notes, [])
        self.assertEqual(v.to_view(reply("answered", missing="x")).notes, [])

    def test_footer_is_on_every_view(self):
        for status in v.KIND:
            self.assertEqual(v.to_view(reply(status)).footer, config.FOOTER_NOTE)

    def test_every_status_has_a_kind(self):
        self.assertEqual(set(v.KIND), {"answered", "partial", "not_found", "out_of_scope", "personal", "advice", "greeting",
                                       "clarify", "error"})
        self.assertEqual(v.to_view(reply("error")).kind, "warning")
        self.assertEqual(v.to_view(reply("greeting")).kind, "friendly")


class TestClarifyAndGreeting(unittest.TestCase):
    def test_options_are_the_conflict_topics_up_to_three(self):
        vm = v.to_view(reply("clarify", "מי?", sources=[], conflict_topics=["health-insurance", "car-insurance", "apartment-insurance", "travel-insurance"]))
        self.assertEqual([(o.label, o.topic) for o in vm.options],
                         [("ביטוח בריאות", "health-insurance"), ("ביטוח רכב", "car-insurance"), ("ביטוח דירה", "apartment-insurance")])

    def test_every_option_label_is_a_known_product_name(self):
        for slug in ("health-insurance", "car-insurance", "travel-insurance", "nursing", "liability"):
            vm = v.to_view(reply("clarify", "?", sources=[], conflict_topics=[slug, "life-insurance"]))
            self.assertEqual(vm.options[0].label, retrieve.topic_name(slug))

    def test_greeting_offers_examples_only_when_there_are_some(self):
        from unittest import mock
        with mock.patch.object(config, "EXAMPLE_QUESTIONS", ()):
            self.assertFalse(v.to_view(reply("greeting")).show_examples)
        with mock.patch.object(config, "EXAMPLE_QUESTIONS", ("שאלה",)):
            self.assertTrue(v.to_view(reply("greeting")).show_examples)


class TestEscaping(unittest.TestCase):
    def test_a_star_phone_does_not_start_italics(self):
        self.assertEqual(v.escape_markdown("אפשר להתקשר ל-3455* או 5432*"), "אפשר להתקשר ל-3455\\* או 5432\\*")

    def test_urls_stay_clickable_and_intact(self):
        text = "ראו https://a.co.il/x_y?z=1 ואז *מודגש* _x_"
        self.assertEqual(v.escape_markdown(text), "ראו https://a.co.il/x_y?z=1 ואז \\*מודגש\\* \\_x\\_")

    def test_html_and_markdown_syntax_is_neutralised(self):
        out = v.escape_markdown("<script>alert(1)</script> [x](javascript:y) # כותרת `code`")
        self.assertNotIn("<script>", out)
        self.assertIn("\\[x\\]", out)
        self.assertIn("\\`code\\`", out)

    def test_the_body_of_the_view_is_escaped(self):
        self.assertEqual(v.to_view(reply(text="טלפון 3455*")).body, "טלפון 3455\\*")


class TestQuestionValidation(unittest.TestCase):
    def test_empty_and_too_long(self):
        self.assertEqual(v.validate_question(""), config.EMPTY_QUESTION_TEXT)
        self.assertEqual(v.validate_question("   \n "), config.EMPTY_QUESTION_TEXT)
        self.assertEqual(v.validate_question("א" * (config.MAX_QUESTION_CHARS + 1)), config.TOO_LONG_TEXT)

    def test_boundary_and_normal(self):
        self.assertIsNone(v.validate_question("א" * config.MAX_QUESTION_CHARS))
        self.assertIsNone(v.validate_question("איך מגישים תביעה?"))
        self.assertEqual(config.MAX_QUESTION_CHARS, 500)


class TestDeveloperMode(unittest.TestCase):
    def test_dev_info_only_when_asked(self):
        r = reply("partial", missing="המודל אמר משהו", calls=1, guard={"kind": "none"})
        self.assertEqual(v.to_view(r).dev, {})
        dev = v.to_view(r, dev=True).dev
        self.assertEqual(dev["missing (model, not verified)"], "המודל אמר משהו")
        self.assertEqual(dev["calls"], 1)


class TestFixedTextsOfTheUi(unittest.TestCase):
    def test_no_digits_or_links_in_the_ui_texts(self):
        for name in ("MISSING_NOTE", "FOOTER_NOTE", "SOURCES_NOTE", "BUSY_TEXT", "EMPTY_QUESTION_TEXT", "TOO_LONG_TEXT", "OPENING_TEXT"):
            self.assertNotRegex(getattr(config, name), r"\d|https?://|www\.", name)


if __name__ == "__main__":
    unittest.main()
