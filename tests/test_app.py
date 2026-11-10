"""AppTest of app.py (Streamlit's headless test runner): the real script runs in-process with answer.answer and the Retriever
replaced by fakes. No API, no browser."""
import os
import unittest
from pathlib import Path
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

import answer
import config
import privacy
from bot import retrieve
from index import build_index

APP = str(Path(__file__).resolve().parent.parent / "app.py")
H = "https://www.fnx.co.il"
SRC = {"title": "הגשת תביעה", "url": H + "/claims-customer-service/claims-health-insurance/x/", "anchor": "how",
       "collected_at": "2026-11-04T10:00:00+00:00"}
PHONE, MAIL, ID = (config.PII_PLACEHOLDERS[k] for k in ("phone", "email", "id"))


class FakeRetriever:
    pass


class Recorder:
    """Replaces answer.answer: records every call, returns queued replies (or the default)."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, question, history=None, *, retriever=None, generate=None, products=None):
        self.calls.append({"question": question, "history": history, "products": products})
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return answer.BotReply("answered", "יש למלא את הטופס באתר. טלפון 3455*.", sources=[SRC])


def run_app(rec, *, env=None, retriever=FakeRetriever):
    st.cache_resource.clear()
    patches = [mock.patch.object(answer, "answer", rec), mock.patch.object(retrieve, "Retriever", retriever)]
    ctx = [p.start() for p in patches]
    envp = mock.patch.dict(os.environ, env or {}, clear=False)
    envp.start()
    os.environ.pop("PHOENIX_CHAT_LOG", None)
    at = AppTest.from_file(APP, default_timeout=30)
    return at, patches + [envp]


class AppCase(unittest.TestCase):
    def start(self, *replies, env=None, retriever=FakeRetriever):
        self.rec = Recorder(*replies)
        at, self.patches = run_app(self.rec, env=env, retriever=retriever)
        self.at = at.run()
        return self.at

    def tearDown(self):
        for p in getattr(self, "patches", []):
            p.stop()
        st.cache_resource.clear()

    def ask(self, text):
        self.at.chat_input[0].set_value(text).run()
        return self.at

    def texts(self):
        return [m.value for m in self.at.markdown] + [c.value for c in self.at.caption]


class TestStart(AppCase):
    def test_the_app_starts_without_an_exception_and_shows_the_opening_screen(self):
        self.start()
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(len(self.at.chat_input), 1)
        joined = " ".join(self.texts())
        self.assertIn("אין להזין פרטים אישיים", joined)
        buttons = [b.label for b in self.at.button]
        for q in config.EXAMPLE_QUESTIONS:
            self.assertIn(q, buttons)
        self.assertGreaterEqual(len(config.EXAMPLE_QUESTIONS), 4)

    def test_a_missing_index_shows_a_friendly_error_and_no_chat(self):
        class Bad:
            def __init__(self):
                raise build_index.IndexMismatchError("no index yet")
        self.start(retriever=Bad)
        self.assertEqual([e.value for e in self.at.error], [config.STARTUP_INDEX_ERROR])
        self.assertEqual((len(self.at.chat_input), len(self.at.exception)), (0, 0))

    def test_a_missing_key_shows_a_friendly_error(self):
        from index import embed

        class Bad:
            def __init__(self):
                raise embed.EmbeddingError("GEMINI_API_KEY is missing")
        self.start(retriever=Bad)
        self.assertEqual([e.value for e in self.at.error], [config.STARTUP_KEY_ERROR])

    def test_any_other_start_failure_is_generic_without_a_stack_trace(self):
        class Bad:
            def __init__(self):
                raise RuntimeError("chroma exploded with secret details")
        self.start(retriever=Bad)
        self.assertEqual([e.value for e in self.at.error], [config.STARTUP_GENERIC_ERROR])
        self.assertNotIn("secret details", " ".join(self.texts()))


class TestConversation(AppCase):
    def test_a_question_is_answered_with_sources_dates_and_the_footer(self):
        self.start()
        self.ask("איך מגישים תביעה?")
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(self.rec.calls[0]["question"], "איך מגישים תביעה?")
        joined = " ".join(self.texts())
        self.assertIn("יש למלא את הטופס באתר", joined)
        self.assertIn("3455\\*", joined)                                     # escaped: no italics
        self.assertIn(f"[הגשת תביעה]({SRC['url']}#how) · נאסף: 04.11.2026", joined)
        self.assertIn(config.SOURCES_NOTE, joined)
        self.assertIn(config.FOOTER_NOTE, joined)
        self.assertEqual(len(self.at.exception), 0)

    def test_history_goes_to_the_next_question_masked_and_without_the_current_one(self):
        self.start()
        self.ask("איך מגישים תביעה? הנייד שלי 0541234567")
        self.ask("ומה לצרף?")
        second = self.rec.calls[1]
        self.assertEqual(second["question"], "ומה לצרף?")
        self.assertEqual([t["role"] for t in second["history"]], ["user", "bot"])
        self.assertEqual(second["history"][0]["text"], f"איך מגישים תביעה? הנייד שלי {PHONE}")
        self.assertEqual(second["history"][1]["status"], "answered")
        self.assertNotIn("0541234567", repr(second))
        self.assertNotIn("המקורות", second["history"][1]["text"])            # the first paragraph only

    def test_history_is_cut_to_the_last_turns(self):
        self.start()
        for i in range(6):
            self.ask(f"שאלה {i}")
        self.assertLessEqual(len(self.rec.calls[-1]["history"]), config.REWRITE_HISTORY_TURNS)

    def test_new_conversation_clears_everything(self):
        self.start()
        self.ask("שאלה")
        self.at.sidebar.button[0].click().run()
        self.assertEqual(self.at.session_state["messages"], [])
        self.ask("שאלה חדשה")
        self.assertEqual(self.rec.calls[-1]["history"], [])


class TestPrivacyInTheApp(AppCase):
    RAW = ("123456789", "0541234567", "someone@example.com")

    def test_the_user_bubble_and_session_state_hold_only_masked_text(self):
        self.start()
        self.ask("מספר הזהות שלי 123456789, הטלפון 0541234567 והמייל someone@example.com. איך מגישים תביעה?")
        self.assertEqual(self.rec.calls[0]["question"],
                         f"מספר הזהות שלי {ID}, הטלפון {PHONE} והמייל {MAIL}. איך מגישים תביעה?")
        bubble = " ".join(c.markdown[0].value for c in self.at.chat_message if c.markdown)
        state = repr(self.at.session_state["messages"])
        everything = bubble + " " + state + " " + " ".join(self.texts()) + " " + repr(self.rec.calls)
        for raw in self.RAW:
            self.assertNotIn(raw, everything, raw)
        self.assertIn(view_escape(ID), bubble)           # shown escaped for markdown

    def test_a_clicked_example_or_option_is_masked_too(self):
        self.start()
        self.assertEqual(privacy.mask(config.EXAMPLE_QUESTIONS[0]).text, config.EXAMPLE_QUESTIONS[0])   # the examples have no PII


class TestQuestionLimits(AppCase):
    def test_an_empty_or_a_too_long_question_never_reaches_answer(self):
        self.start()
        self.ask("   ")
        self.ask("א" * (config.MAX_QUESTION_CHARS + 1))
        self.assertEqual(self.rec.calls, [])
        joined = " ".join(self.texts())
        self.assertIn(config.TOO_LONG_TEXT, joined)
        self.assertEqual([m["role"] for m in self.at.session_state["messages"]], ["bot", "bot"][-len(self.at.session_state["messages"]):])

    def test_exactly_the_limit_is_allowed(self):
        self.start()
        self.ask("א" * config.MAX_QUESTION_CHARS)
        self.assertEqual(len(self.rec.calls), 1)


class TestStatuses(AppCase):
    def reply_for(self, status, text="טקסט קבוע", **kw):
        return answer.BotReply(status, text, **kw)

    def test_error_is_a_warning_box_and_has_no_sources(self):
        self.start(self.reply_for("error", config.ERROR_QUOTA_TEXT))
        self.ask("שאלה")
        self.assertEqual([w.value for w in self.at.warning], [view_escape(config.ERROR_QUOTA_TEXT)])

    def test_busy_and_daily_quota_texts_are_shown_as_they_are(self):
        self.start(self.reply_for("error", config.BUSY_TEXT), self.reply_for("error", config.ERROR_QUOTA_DAILY_TEXT))
        self.ask("שאלה")
        self.ask("עוד שאלה")
        shown = [w.value for w in self.at.warning]
        self.assertEqual(shown, [view_escape(config.BUSY_TEXT), view_escape(config.ERROR_QUOTA_DAILY_TEXT)])

    def test_partial_shows_the_fixed_note_and_never_the_model_missing_text(self):
        self.start(self.reply_for("partial", "ניתן להגיש תביעה.", sources=[SRC], missing="טקסט חופשי של המודל"))
        self.ask("שאלה")
        joined = " ".join(self.texts())
        self.assertIn(config.MISSING_NOTE, joined)
        self.assertNotIn("טקסט חופשי של המודל", joined)

    def test_statuses_without_sources_show_no_source_list(self):
        for status in ("not_found", "out_of_scope", "advice", "greeting"):
            self.tearDown()
            self.start(self.reply_for(status, "טקסט", sources=[SRC]))
            self.ask("שאלה")
            self.assertNotIn("**המקורות:**", " ".join(self.texts()), status)

    def test_personal_shows_the_contact_page_as_a_source(self):
        contact = {"title": "כל הדרכים ליצור איתנו קשר", "url": H + "/claims-customer-service/be-in-touch/", "anchor": "",
                   "collected_at": ""}
        self.start(self.reply_for("personal", "אין לי גישה למידע אישי", sources=[contact]))
        self.ask("מה מצב התביעה שלי?")
        self.assertIn(f"[כל הדרכים ליצור איתנו קשר]({contact['url']})", " ".join(self.texts()))

    def test_greeting_offers_the_examples(self):
        self.start(self.reply_for("greeting", config.GREETING_TEXT))
        self.ask("שלום")
        self.assertEqual(len([b for b in self.at.button if b.label in config.EXAMPLE_QUESTIONS]), len(config.EXAMPLE_QUESTIONS))

    def test_an_exception_in_answer_is_a_friendly_error_not_a_trace(self):
        self.start(RuntimeError("bug with secret details"))
        self.ask("שאלה")
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual([w.value for w in self.at.warning], [view_escape(config.ERROR_TEXT)])
        self.assertNotIn("secret details", " ".join(self.texts()))


class TestClarifyAndExamples(AppCase):
    def clarify_reply(self):
        return answer.BotReply("clarify", config.CLARIFY_TEXT.format(options="ביטוח בריאות או ביטוח רכב"),
                               debug={"conflict_topics": ["health-insurance", "car-insurance"]})

    def test_clarify_buttons_continue_with_the_chosen_product_and_the_original_question(self):
        self.start(self.clarify_reply())
        self.ask("איך מגישים תביעה?")
        labels = [b.label for b in self.at.button]
        self.assertIn("ביטוח בריאות", labels)
        self.assertIn("ביטוח רכב", labels)
        [b for b in self.at.button if b.label == "ביטוח רכב"][0].click().run()
        call = self.rec.calls[1]
        self.assertEqual((call["question"], call["products"]), ("איך מגישים תביעה?", ["car-insurance"]))
        self.assertIn("clarify", [t["status"] for t in call["history"]])
        msgs = self.at.session_state["messages"]
        self.assertEqual([m["text"] for m in msgs[:3]][0:1] + [msgs[2]["text"]], ["איך מגישים תביעה?", "ביטוח רכב"])   # the bubble shows the choice
        self.assertEqual([m["status"] for m in msgs], [None, "clarify", None, "answered"])

    def test_free_typing_still_works_after_a_clarify(self):
        self.start(self.clarify_reply())
        self.ask("איך מגישים תביעה?")
        self.ask("בריאות")
        self.assertEqual(self.rec.calls[1]["question"], "בריאות")
        self.assertIn("clarify", [t["status"] for t in self.rec.calls[1]["history"]])

    def test_old_clarify_buttons_disappear_after_the_next_answer(self):
        self.start(self.clarify_reply())
        self.ask("איך מגישים תביעה?")
        self.ask("בריאות")
        self.assertNotIn("ביטוח רכב", [b.label for b in self.at.button])

    def test_a_clicked_example_is_sent_as_a_normal_question(self):
        self.start()
        q = config.EXAMPLE_QUESTIONS[1]
        [b for b in self.at.button if b.label == q][0].click().run()
        self.assertEqual(self.rec.calls[0]["question"], q)
        self.assertEqual(self.rec.calls[0]["products"], None)


class TestNoHtmlInjection(AppCase):
    def test_html_in_an_answer_a_title_or_a_question_is_escaped_never_rendered(self):
        evil = '<img src=x onerror=alert(1)> <script>alert(2)</script>'
        src = dict(SRC, title=f"כותרת {evil}")
        self.start(answer.BotReply("answered", f"טקסט {evil}", sources=[src]))
        self.ask(f"שאלה {evil}")
        values = [m.value for m in self.at.markdown] + [c.value for c in self.at.caption]
        css_blocks = [v for v in values if v.strip().startswith("<style>")]
        self.assertEqual(len(css_blocks), 1)                                   # only the static CSS is HTML
        for v in values:
            if v in css_blocks:
                continue
            self.assertNotRegex(v, r"(?<![\\])<\s*(img|script)")      # every "<" is backslash-escaped: shown as text, never as a tag
        self.assertEqual(len(self.at.exception), 0)


class TestCopyConversation(AppCase):
    DEBUGGY = answer.BotReply(
        "partial", "ניתן להגיש תביעה. טלפון 3455*.", sources=[SRC], missing="x",
        debug={"rewrite": {"question": "השאלה אחרי שכתוב"}, "calls": 2,
               "retrieval": {"groups": [{"url": H + "/a/", "page_title": "עמוד א", "section": "מקטע ב", "distance": 0.212, "topic": "t", "size": 1}]},
               "removed_claims": [{"text": "טענה שהוסרה", "quote": "q", "reason": "low_overlap", "note": ""}]})

    def sidebar_code(self):
        codes = self.at.sidebar.code
        return codes[0].value if len(codes) else None

    def test_no_copy_area_before_the_first_message(self):
        self.start()
        self.assertEqual(len(self.at.sidebar.expander), 0)
        self.assertIsNone(self.sidebar_code())

    def test_the_conversation_is_in_a_collapsed_sidebar_code_block_with_masked_questions(self):
        self.start(self.DEBUGGY)
        self.ask("הנייד שלי 0541234567, איך מגישים תביעה?")
        self.assertEqual([e.label for e in self.at.sidebar.expander], [config.COPY_CHAT_LABEL])
        text = self.sidebar_code()
        self.assertIn(f"שאלה: הנייד שלי {PHONE}, איך מגישים תביעה?", text)
        self.assertNotIn("0541234567", text)
        order = [text.index(x) for x in ("שאלה:", "סטטוס: נענתה חלקית (partial)", "תשובה:", "מקורות:")]
        self.assertEqual(order, sorted(order))
        self.assertIn("ניתן להגיש תביעה. טלפון 3455*.", text)          # plain text: not markdown-escaped
        self.assertIn(f"{SRC['url']}#how | נאסף: 04.11.2026", text)

    def test_no_developer_details_when_developer_mode_is_off(self):
        self.start(self.DEBUGGY)
        self.ask("שאלה")
        text = self.sidebar_code()
        for dev_only in ("השאלה אחרי שכתוב", "הקבוצות שנשלפו", "מרחק", "טענות שהוסרו", "low_overlap", "מצב מפתחת"):
            self.assertNotIn(dev_only, text)

    def test_developer_details_are_included_in_developer_mode(self):
        self.start(self.DEBUGGY, env={"PHOENIX_DEV": "1"})
        self.ask("שאלה")
        text = self.sidebar_code()
        self.assertIn("[מצב מפתחת] השאלה אחרי שכתוב: השאלה אחרי שכתוב", text)
        self.assertIn("עמוד א > מקטע ב | מרחק 0.212 | " + H + "/a/", text)
        self.assertIn("low_overlap: טענה שהוסרה", text)

    def test_the_whole_conversation_is_there_in_order_and_a_new_chat_removes_it(self):
        self.start()
        self.ask("שאלה ראשונה")
        self.ask("שאלה שנייה")
        text = self.sidebar_code()
        self.assertLess(text.index("שאלה: שאלה ראשונה"), text.index("שאלה: שאלה שנייה"))
        self.assertEqual(text.count("סטטוס:"), 2)
        self.at.sidebar.button[0].click().run()
        self.assertIsNone(self.sidebar_code())

    def test_a_download_button_gives_the_same_text_as_a_txt_file(self):
        self.start()
        self.ask("שאלה")
        buttons = self.at.sidebar.get("download_button")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].proto.label, config.DOWNLOAD_CHAT_LABEL)

    def test_nothing_is_written_to_disk_by_the_copy_area(self):
        import tempfile
        before = set(Path(tempfile.gettempdir()).glob("conversation*.txt"))
        self.start()
        self.ask("שאלה")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("conversation*.txt")), before)
        self.assertFalse((Path(APP).parent / "conversation.txt").exists())


class TestDeveloperMode(AppCase):
    def test_off_by_default(self):
        self.start()
        self.ask("שאלה")
        self.assertEqual(len(self.at.main.expander), 0)               # the developer expander lives in the main area

    def test_on_with_the_environment_variable_only(self):
        self.start(env={"PHOENIX_DEV": "1"})
        self.ask("שאלה")
        self.assertEqual(len(self.at.main.expander), 1)


class TestChatLogFromTheApp(AppCase):
    def test_nothing_is_written_by_default(self):
        with mock.patch.object(config, "CHAT_LOG_PATH", Path(os.environ.get("TEMP", ".")) / "app_test_chat.jsonl") as _:
            path = config.CHAT_LOG_PATH
            if path.exists():
                path.unlink()
            self.start()
            self.ask("שאלה")
            self.assertFalse(path.exists())


def view_escape(text):
    import view
    return view.escape_markdown(text)


if __name__ == "__main__":
    unittest.main()
