import re
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


SRC_ = src()


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


class TestTranscript(unittest.TestCase):
    PHONE, ID, MAIL = (config.PII_PLACEHOLDERS[k] for k in ("phone", "id", "email"))
    RULE = "\u2501\u2501"

    def bot(self, status="answered", text="יש למלא את הטופס באתר. טלפון 3455*.", sources=None, claims=None, notes=None, missing="", **debug):
        r = BotReply(status, text, sources=[SRC_] if sources is None else sources, missing=missing, debug=debug,
                     claims=list(claims or []), notes=list(notes or []))
        return {"role": "bot", "text": v.history_text(text), "status": status, "view": v.to_view(r, dev=True), "question": ""}

    def user(self, text):
        return {"role": "user", "text": text, "status": None, "view": None}

    def convo(self, **debug):
        return [self.user("איך מגישים תביעה?"), self.bot(claims=["יש למלא את הטופס באתר.", "אפשר לפנות בטלפון 3455*."], **debug),
                self.user("ומה לצרף?"), self.bot("not_found", config.NOT_FOUND_TEXT, sources=[])]

    def test_the_layout_title_numbered_messages_question_hebrew_status_list_numbered_sources(self):
        t = v.transcript(self.convo())
        lines = t.splitlines()
        self.assertEqual(lines[0], "שיחה עם עוזר המידע לאתר הפניקס")
        self.assertIn(f"{self.RULE} הודעה 1 {self.RULE}", lines)
        self.assertIn(f"{self.RULE} הודעה 2 {self.RULE}", lines)
        i = lines.index("שאלה: איך מגישים תביעה?")
        self.assertEqual(lines[i + 1:i + 5], ["סטטוס: נענתה", "תשובה:", "\u2022 יש למלא את הטופס באתר.", "\u2022 אפשר לפנות בטלפון 3455*."])
        self.assertEqual(lines[i + 5:i + 8], ["מקורות:", "1. הגשת תביעה \u2014 נאסף: 04.11.2026",
                                              f"   {H}/claims-customer-service/claims-health-insurance/x/#how"])
        j = lines.index("שאלה: ומה לצרף?")
        self.assertEqual(lines[j + 1], "סטטוס: לא נמצא")
        self.assertEqual(t.count("מקורות:"), 1)

    def test_one_claim_or_a_fixed_text_is_plain_two_or_more_are_bullets(self):
        one = v.transcript([self.user("ש"), self.bot(claims=["טענה אחת בלבד."])])
        self.assertIn("תשובה:\nטענה אחת בלבד.\n", one)
        self.assertNotIn("\u2022", one)
        fixed = v.transcript([self.user("ש"), self.bot("advice", config.ADVICE_TEXT, sources=[])])
        self.assertNotIn("\u2022", fixed)
        self.assertIn(config.ADVICE_TEXT, fixed)

    def test_hebrew_status_labels(self):
        expected = {"answered": "נענתה", "partial": "חלקית", "not_found": "לא נמצא", "out_of_scope": "מחוץ להיקף"}
        for status, label in expected.items():
            self.assertEqual(v.STATUS_LABELS[status], label)
            t = v.transcript([self.user("ש"), self.bot(status, "טקסט", sources=[], claims=["טענה."] if status in ("answered", "partial") else [])])
            self.assertIn(f"סטטוס: {label}\n", t)
        for status in v.KIND:
            self.assertIn(status, v.STATUS_LABELS)

    def test_each_note_and_caveat_is_on_its_own_line_labelled(self):
        msgs = [self.user("ש"), self.bot("partial", "טקסט", claims=["יש להגיש.", "אפשר לשלוח."], missing="משהו חסר",
                                          notes=[config.PARTIAL_REMOVED_NOTE, config.COVERAGE_CAVEAT])]
        lines = v.transcript(msgs).splitlines()
        self.assertIn(f"הערה: {config.PARTIAL_REMOVED_NOTE}", lines)
        self.assertIn(f"הערה: {config.MISSING_NOTE}", lines)
        self.assertIn(f"הסתייגות: {config.COVERAGE_CAVEAT}", lines)
        self.assertLess(lines.index(f"הערה: {config.PARTIAL_REMOVED_NOTE}"), lines.index(f"הערה: {config.MISSING_NOTE}"))
        self.assertLess(lines.index(f"הערה: {config.MISSING_NOTE}"), lines.index(f"הסתייגות: {config.COVERAGE_CAVEAT}"))
        self.assertLess(lines.index(f"הסתייגות: {config.COVERAGE_CAVEAT}"), lines.index("מקורות:"))

    def test_a_partial_answer_has_the_note_in_the_copy_as_on_the_screen(self):
        r = BotReply("partial", "טענה אחת.", sources=[SRC_], missing="הנוסח של המודל", claims=["טענה אחת."])
        vm = v.to_view(r)
        self.assertEqual(vm.notes, [config.MISSING_NOTE])                        # what the screen shows ...
        t = v.transcript([self.user("ש"), {"role": "bot", "text": "x", "status": "partial", "view": vm, "question": ""}])
        self.assertIn(f"הערה: {config.MISSING_NOTE}", t)                          # ... is in the copy too
        self.assertNotIn("הנוסח של המודל", t)

    def test_notes_of_a_fixed_text_not_found_with_a_personal_caveat(self):
        r = BotReply("not_found", config.NOT_FOUND_TEXT + "\n\n" + config.SUSPECTED_PERSONAL_CAVEAT, notes=[config.SUSPECTED_PERSONAL_CAVEAT])
        t = v.transcript([self.user("ש"), {"role": "bot", "text": "x", "status": "not_found", "view": v.to_view(r), "question": ""}])
        lines = t.splitlines()
        self.assertEqual(lines.count(config.NOT_FOUND_TEXT), 1)
        self.assertIn(f"הסתייגות: {config.SUSPECTED_PERSONAL_CAVEAT}", lines)

    def test_no_english_words_in_the_generated_text_urls_aside(self):
        groups = [{"url": H + "/a/", "page_title": "עמוד א", "section": "מקטע ב", "distance": 0.2, "topic": "t", "size": 1}]
        removed = [{"text": "טענה", "quote": "q", "reason": r, "note": ""} for r in v.REASON_LABELS]
        msgs = self.convo(rewrite={"question": "אחרי שכתוב"}, retrieval={"groups": groups}, removed_claims=removed)
        for dev in (False, True):
            t = v.transcript(msgs, dev=dev)
            without_urls = re.sub(r"https?://\S+", "", t)
            self.assertNotRegex(without_urls, r"[A-Za-z]", f"dev={dev}: {re.findall(r'[A-Za-z_]+', without_urls)}")

    def test_an_empty_conversation_is_an_empty_string(self):
        self.assertEqual(v.transcript([]), "")
        self.assertEqual(v.transcript([], dev=True), "")

    def test_no_raw_personal_value_even_if_the_caller_forgot_to_mask(self):
        msgs = [self.user("ת״ז 123456789 נייד 0541234567 מייל a.b@c.com איך מגישים?"),
                {"role": "bot", "text": "x", "status": "notice", "view": v.notice_view("השאלה ארוכה מדי"), "question": ""}]
        t = v.transcript(msgs)
        for raw in ("123456789", "0541234567", "a.b@c.com"):
            self.assertNotIn(raw, t, raw)
        for ph in (self.ID, self.PHONE, self.MAIL):
            self.assertIn(ph, t)

    def test_the_rewritten_question_in_the_developer_part_is_masked_too(self):
        msgs = self.convo(rewrite={"question": "שאלה עם 0541234567"}, retrieval={"groups": []})
        self.assertNotIn("0541234567", v.transcript(msgs, dev=True))

    def test_answers_are_not_masked_the_sites_own_numbers_stay(self):
        msgs = [self.user("מה מצב התביעה שלי?"), self.bot("personal", "בטלפון: 3455* או בוואטסאפ 054-9893455", sources=[])]
        self.assertIn("054-9893455", v.transcript(msgs))

    def test_no_debug_at_all_when_developer_mode_is_off(self):
        groups = [{"url": H + "/a/", "page_title": "עמוד א", "section": "מקטע ב", "distance": 0.2, "topic": "t", "size": 1}]
        msgs = self.convo(rewrite={"question": "אחרי שכתוב"}, retrieval={"groups": groups},
                          removed_claims=[{"text": "טענה שהוסרה", "quote": "q", "reason": "low_overlap", "note": ""}])
        t = v.transcript(msgs, dev=False)
        for dev_only in ("אחרי שכתוב", "מקטע ב", "0.2", "חפיפה נמוכה", "טענה שהוסרה", "מצב מפתחת", "שכתוב:", "קבוצות שנשלפו"):
            self.assertNotIn(dev_only, t, dev_only)

    def test_the_developer_part_rewrite_groups_distance_title_address_next_line_and_removed_claims(self):
        groups = [{"url": H + "/a/", "page_title": "עמוד א", "section": "מקטע ב", "distance": 0.2, "topic": "t", "size": 1},
                  {"url": H + "/b/", "page_title": "עמוד ג", "section": "", "distance": 0.31, "topic": "t", "size": 2}]
        msgs = self.convo(rewrite={"question": "אחרי שכתוב"}, retrieval={"groups": groups},
                          removed_claims=[{"text": "טענה שהוסרה", "quote": "q", "reason": "low_overlap", "note": ""}])
        lines = v.transcript(msgs, dev=True).splitlines()
        i = lines.index("[מצב מפתחת]")
        self.assertEqual(lines[i + 1], "שכתוב: אחרי שכתוב")
        self.assertEqual(lines[i + 2:i + 7], ["קבוצות שנשלפו:", "1. 0.2 | מקטע ב", f"   {H}/a/", "2. 0.31 | עמוד ג", f"   {H}/b/"])
        self.assertEqual(lines[i + 7:i + 9], ["טענות שהוסרו:", "- חפיפה נמוכה בין הטענה לציטוט: טענה שהוסרה"])
        self.assertLess(lines.index("מקורות:"), i)                               # developer lines come after the sources

    def test_no_developer_header_when_there_is_nothing_to_show(self):
        msgs = [self.user("ש"), self.bot("not_found", config.NOT_FOUND_TEXT, sources=[])]
        self.assertNotIn("מצב מפתחת", v.transcript(msgs, dev=True))

    def test_every_removal_reason_has_a_hebrew_label(self):
        from verify import verify_claims  # noqa: F401  (the reasons below are the ones verify_claims can produce)
        for reason in ("not_in_source", "ellipsis", "too_short", "empty_quote", "empty_text", "bad_source_number", "low_overlap",
                       "unsupported_value", "strength_word"):
            self.assertIn(reason, v.REASON_LABELS)


class TestLastAnswer(unittest.TestCase):
    def bot(self, status="answered", text="x", sources=None, claims=None, notes=None, missing=""):
        r = BotReply(status, text, sources=[SRC_] if sources is None else sources, missing=missing, claims=list(claims or []),
                     notes=list(notes or []))
        return {"role": "bot", "text": v.history_text(text), "status": status, "view": v.to_view(r, dev=True), "question": ""}

    def test_it_is_the_last_answer_with_notes_and_numbered_sources_only(self):
        msgs = [{"role": "user", "text": "ש1", "status": None, "view": None}, self.bot(claims=["תשובה ראשונה."]),
                {"role": "user", "text": "ש2", "status": None, "view": None},
                self.bot(claims=["התשובה הסופית.", "טלפון 3455*."], notes=[config.COVERAGE_CAVEAT])]
        out = v.last_answer(msgs)
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["\u2022 התשובה הסופית.", "\u2022 טלפון 3455*."])
        self.assertIn(f"הסתייגות: {config.COVERAGE_CAVEAT}", lines)
        self.assertEqual(lines[-3:], ["מקורות:", "1. הגשת תביעה \u2014 נאסף: 04.11.2026",
                                      f"   {H}/claims-customer-service/claims-health-insurance/x/#how"][-3:])
        for absent in ("תשובה ראשונה", "ש2", "סטטוס", "שאלה:"):
            self.assertNotIn(absent, out)
        self.assertTrue(out.endswith("\n"))

    def test_the_partial_note_is_in_the_final_answer_copy_too(self):
        out = v.last_answer([self.bot("partial", "t", claims=["טענה."], missing="משהו")])
        self.assertIn(f"הערה: {config.MISSING_NOTE}", out)

    def test_no_sources_block_when_the_answer_has_none(self):
        out = v.last_answer([self.bot("not_found", config.NOT_FOUND_TEXT, sources=[])])
        self.assertEqual(out, config.NOT_FOUND_TEXT + "\n")

    def test_empty_when_there_is_no_answer_yet_and_a_system_notice_is_not_an_answer(self):
        self.assertEqual(v.last_answer([]), "")
        self.assertEqual(v.last_answer([{"role": "user", "text": "ש", "status": None, "view": None}]), "")
        notice = {"role": "bot", "text": "x", "status": "notice", "view": v.notice_view("השאלה ארוכה מדי"), "question": ""}
        self.assertEqual(v.last_answer([self.bot(claims=["תשובה אמיתית."]), notice]).splitlines()[0], "תשובה אמיתית.")

    def test_the_answer_is_not_masked_the_sites_own_numbers_stay(self):
        self.assertIn("054-9893455", v.last_answer([self.bot("personal", "בוואטסאפ 054-9893455", sources=[])]))


class TestClaimsAsAList(unittest.TestCase):
    def test_two_or_more_claims_are_items_one_claim_is_a_paragraph(self):
        two = v.to_view(BotReply("answered", "א. ב.", claims=["טענה א.", "טענה ב*."], sources=[]))
        self.assertEqual(two.items, ["טענה א.", "טענה ב\\*."])
        self.assertEqual(two.body, "")
        one = v.to_view(BotReply("answered", "א.", claims=["טענה אחת."], sources=[]))
        self.assertEqual((one.items, one.body), ([], "טענה אחת."))

    def test_without_claims_the_text_is_the_body(self):
        vm = v.to_view(BotReply("advice", config.ADVICE_TEXT))
        self.assertEqual((vm.items, vm.body), ([], config.ADVICE_TEXT))

    def test_notes_are_separate_lines_and_the_missing_note_follows_the_removed_note(self):
        r = BotReply("partial", "x", claims=["א.", "ב."], missing="m", notes=[config.PARTIAL_REMOVED_NOTE, config.COVERAGE_CAVEAT])
        self.assertEqual(v.to_view(r).notes, [config.PARTIAL_REMOVED_NOTE, config.MISSING_NOTE, config.COVERAGE_CAVEAT])
        r2 = BotReply("partial", "x", claims=["א."], missing="m", notes=[config.COVERAGE_CAVEAT])
        self.assertEqual(v.to_view(r2).notes, [config.MISSING_NOTE, config.COVERAGE_CAVEAT])

    def test_not_found_with_a_personal_caveat_shows_the_fixed_text_and_the_caveat_as_a_note(self):
        r = BotReply("not_found", config.NOT_FOUND_TEXT + "\n\n" + config.SUSPECTED_PERSONAL_CAVEAT,
                     notes=[config.SUSPECTED_PERSONAL_CAVEAT])
        vm = v.to_view(r)
        self.assertEqual((vm.body, vm.notes), (config.NOT_FOUND_TEXT, [config.SUSPECTED_PERSONAL_CAVEAT]))


class TestFixedTextsOfTheUi(unittest.TestCase):
    def test_no_digits_or_links_in_the_ui_texts(self):
        for name in ("MISSING_NOTE", "FOOTER_NOTE", "SOURCES_NOTE", "BUSY_TEXT", "EMPTY_QUESTION_TEXT", "TOO_LONG_TEXT", "OPENING_TEXT"):
            self.assertNotRegex(getattr(config, name), r"\d|https?://|www\.", name)


if __name__ == "__main__":
    unittest.main()
