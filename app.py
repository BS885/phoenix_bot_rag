"""Streamlit chat for the Phoenix site bot (stage 6.3). Run:  streamlit run app.py

Thin on purpose: every decision (status, caveats, verification, fixed texts) is made in answer.py / config.py, and everything that can
be tested without a browser lives in view.py / privacy.py. This file only keeps the conversation in st.session_state and draws.

Rules kept here:
  * the question is masked (privacy.mask) before it enters st.session_state, the history, the log or any API call:
    the user bubble shows the MASKED text and no raw value is ever stored;
  * unsafe_allow_html is used ONLY for the static CSS constant below (tests/test_app_static.py checks this with the AST);
    every dynamic text goes through st.markdown(escaped text) / st.caption / st.warning, never as HTML;
  * the model's `missing` text is never shown (view.to_view shows the fixed MISSING_NOTE instead).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: F401,E402  (truststore, for NetFree)
import streamlit as st  # noqa: E402

import answer  # noqa: E402
import chat_log  # noqa: E402
import config  # noqa: E402
import privacy  # noqa: E402
import view  # noqa: E402
from bot import retrieve  # noqa: E402
from index import build_index, embed  # noqa: E402

_CSS = """
<style>
.stApp, .stChatMessage, [data-testid="stChatInput"], [data-testid="stSidebar"], [data-testid="stSidebarContent"] {
    direction: rtl; text-align: right;
}
.stMarkdown p, .stMarkdown li, [data-testid="stCaptionContainer"], [data-testid="stAlert"] {
    direction: rtl; text-align: right; unicode-bidi: plaintext;
}
[data-testid="stChatInput"] textarea { direction: rtl; text-align: right; }
.stButton > button { direction: rtl; }
pre, code, [data-testid="stJson"] { direction: ltr; text-align: left; }
@media (max-width: 640px) { .block-container { padding-left: 0.8rem; padding-right: 0.8rem; } }
</style>
"""

DEV = os.environ.get("PHOENIX_DEV", "") == "1"


# ---------- start-up ----------

@st.cache_resource(show_spinner=False)
def get_retriever():
    """Chroma + embedder + cache, built once per process."""
    return retrieve.Retriever()


def startup_error() -> str | None:
    """A fixed, friendly message when the app cannot start (no index / wrong index / no key); never a stack trace."""
    try:
        get_retriever()
    except build_index.IndexMismatchError:
        return config.STARTUP_INDEX_ERROR
    except embed.EmbeddingError:
        return config.STARTUP_KEY_ERROR
    except Exception as exc:  # noqa: BLE001
        print(f"startup failed: {type(exc).__name__}", file=sys.stderr)      # the type only, never a message with content
        return config.STARTUP_GENERIC_ERROR
    return None


# ---------- conversation state ----------

def init_state() -> None:
    st.session_state.setdefault("messages", [])      # [{"role", "text" (masked), "status", "view", "question"}]
    st.session_state.setdefault("inflight", None)    # {"question": masked, "products": [slug] | None}
    st.session_state.setdefault("pending", None)     # set by buttons: {"question", "display", "products"}


def new_conversation() -> None:
    st.session_state["messages"] = []
    st.session_state["inflight"] = None
    st.session_state["pending"] = None


def enqueue(question: str, *, products=None, display: str | None = None) -> None:
    """Validate, mask, store the user bubble (masked) and mark the question as in flight."""
    problem = view.validate_question(question)
    if problem:
        st.session_state["messages"].append({"role": "bot", "text": problem, "status": "notice",
                                              "view": view.notice_view(problem), "question": ""})
        return
    masked = privacy.mask(question).text
    st.session_state["messages"].append({"role": "user", "text": privacy.mask_text(display) if display else masked,
                                         "status": None, "view": None, "question": ""})
    st.session_state["inflight"] = {"question": masked, "products": products}


def history_for_answer() -> list[dict]:
    """Masked texts only; the in-flight user message is not part of it (answer() gets it as the question)."""
    msgs = [m for m in st.session_state["messages"] if m["status"] != "notice"]
    msgs = msgs[:-1] if msgs and msgs[-1]["role"] == "user" else msgs
    keep = msgs[-config.REWRITE_HISTORY_TURNS:]
    return [{"role": m["role"], "text": m["text"], "status": m["status"]} for m in keep]


def run_inflight() -> None:
    job = st.session_state["inflight"]
    if not job:
        return
    history = history_for_answer()
    with st.chat_message("assistant"):
        with st.spinner(config.APP_SPINNER):
            try:
                reply = answer.answer(job["question"], history, retriever=get_retriever(), products=job["products"])
            except Exception as exc:  # noqa: BLE001  (a bug must not show a stack trace to users)
                print(f"answer failed: {type(exc).__name__}", file=sys.stderr)
                reply = answer.BotReply("error", config.ERROR_TEXT, debug={"error": type(exc).__name__})
    chat_log.log_turn(job["question"], reply)
    st.session_state["messages"].append({"role": "bot", "text": view.history_text(reply.text), "status": reply.status,
                                         "view": view.to_view(reply, dev=DEV), "question": job["question"]})
    st.session_state["inflight"] = None
    st.rerun()


# ---------- drawing ----------

def draw_message(index: int, m: dict, is_last: bool) -> None:
    if m["role"] == "user":
        with st.chat_message("user"):
            st.markdown(view.escape_markdown(m["text"]))
        return
    vm = m["view"]
    with st.chat_message("assistant"):
        if vm.kind == "warning":
            st.warning(vm.body)
        else:
            st.markdown(vm.body)
        for note in vm.notes:
            st.caption(note)
        if vm.sources:
            st.markdown("**המקורות:**")
            for s in vm.sources:
                st.markdown("- " + s.markdown())
            st.caption(vm.sources_note)
        if vm.options and is_last:
            for j, opt in enumerate(vm.options):
                if st.button(opt.label, key=f"clarify-{index}-{j}"):
                    st.session_state["pending"] = {"question": m["question"], "display": opt.label, "products": [opt.topic]}
                    st.rerun()
        if vm.show_examples and is_last:
            draw_examples(f"greeting-{index}")
        if vm.footer:
            st.caption(vm.footer)
        if vm.dev:
            with st.expander("מצב מפתחת"):
                st.json(vm.dev)


def draw_examples(prefix: str) -> None:
    for j, q in enumerate(config.EXAMPLE_QUESTIONS):
        if st.button(q, key=f"{prefix}-example-{j}"):
            st.session_state["pending"] = {"question": q, "display": None, "products": None}
            st.rerun()


def draw_opening() -> None:
    st.markdown(view.escape_markdown(config.OPENING_TEXT))
    if config.EXAMPLE_QUESTIONS:
        st.markdown("**אפשר להתחיל מכאן:**")
        draw_examples("opening")


def draw_sidebar() -> None:
    with st.sidebar:
        if st.button(config.NEW_CHAT_LABEL, key="new-chat"):
            new_conversation()
            st.rerun()
        if DEV:
            st.caption("מצב מפתחת פעיל")


# ---------- main ----------

def main() -> None:
    st.set_page_config(page_title=config.APP_TITLE, layout="centered")
    st.markdown(_CSS, unsafe_allow_html=True)             # the only unsafe_allow_html in the app: a static constant
    st.title(config.APP_TITLE)
    err = startup_error()
    if err:
        st.error(err)
        return
    init_state()
    draw_sidebar()

    pending = st.session_state["pending"]
    st.session_state["pending"] = None
    typed = st.chat_input(config.INPUT_PLACEHOLDER)
    if pending and not st.session_state["inflight"]:
        enqueue(pending["question"], products=pending["products"], display=pending["display"])
        st.rerun()
    if typed:
        enqueue(typed)
        st.rerun()

    messages = st.session_state["messages"]
    if not messages:
        draw_opening()
    for i, m in enumerate(messages):
        draw_message(i, m, is_last=(i == len(messages) - 1))
    run_inflight()


main()
