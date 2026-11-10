"""BotReply -> what the screen shows (stage 6.2). Pure Python, no Streamlit: app.py only draws a ViewModel.

Nothing is written here by a model or by this module: texts are the reply text and the fixed texts of config.py. The reply text
is escaped for markdown (a phone like 3455* must not turn the rest into italics); links are built only from reply.sources and
only for http(s) addresses. The model's `missing` text is NEVER shown to users (it is not verified in code): a fixed note is.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime

import config
from bot import retrieve

# visual kind per status (app.py maps a kind to a CSS class)
KIND = {"answered": "normal", "partial": "normal", "not_found": "neutral", "out_of_scope": "neutral", "personal": "neutral",
        "advice": "neutral", "greeting": "friendly", "clarify": "neutral", "error": "warning"}
STATUSES_WITH_SOURCES = {"answered", "partial", "personal"}

_URL = re.compile(r"https?://[^\s)\]>\"'<]+")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|~])")


@dataclass
class SourceView:
    title: str
    url: str                   # with the #anchor when there is one; always http(s)
    date: str = ""             # "04.11.2026" or ""

    def markdown(self) -> str:
        label = escape_markdown(self.title or self.url)
        return f"[{label}]({self.url})" + (f" · נאסף: {self.date}" if self.date else "")


@dataclass
class ClarifyOption:
    label: str                 # shown on the button, e.g. "ביטוח בריאות"
    topic: str                 # slug passed to answer.answer(products=[topic])


@dataclass
class ViewModel:
    status: str
    kind: str
    body: str                              # markdown-safe text
    notes: list = field(default_factory=list)       # fixed notes under the body
    sources: list = field(default_factory=list)     # list[SourceView]
    sources_note: str = ""
    options: list = field(default_factory=list)     # list[ClarifyOption] (clarify only)
    show_examples: bool = False                     # greeting: offer the example questions
    footer: str = config.FOOTER_NOTE
    dev: dict = field(default_factory=dict)         # filled only in developer mode


def escape_markdown(text: str) -> str:
    """Escape markdown specials in the plain parts of `text`; URLs are left intact so that they stay clickable."""
    out, pos = [], 0
    for m in _URL.finditer(text):
        out.append(_MD_SPECIAL.sub(r"\\\1", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_MD_SPECIAL.sub(r"\\\1", text[pos:]))
    return "".join(out)


def format_date(collected_at: str) -> str:
    """'2026-11-04T10:00:00+00:00' -> '04.11.2026'; anything unreadable -> ''."""
    if not collected_at:
        return ""
    try:
        return datetime.fromisoformat(collected_at.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", collected_at)
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ""


def source_views(sources: list[dict]) -> list[SourceView]:
    out = []
    for s in sources or []:
        url = (s.get("url") or "").strip()
        if not re.match(r"https?://", url):
            continue                                                    # never a javascript: / data: / relative link
        if s.get("anchor"):
            url = f"{url}#{s['anchor']}"
        out.append(SourceView(title=s.get("title") or "", url=url, date=format_date(s.get("collected_at", ""))))
    return out


def validate_question(question: str) -> str | None:
    """A fixed message when the question must not be sent (empty / too long); None when it is fine."""
    q = (question or "").strip()
    if not q:
        return config.EMPTY_QUESTION_TEXT
    if len(q) > config.MAX_QUESTION_CHARS:
        return config.TOO_LONG_TEXT
    return None


def notice_view(text: str) -> ViewModel:
    """A fixed message that is not a bot answer (validation of the question): no sources, no footer."""
    return ViewModel(status="notice", kind="neutral", body=escape_markdown(text), footer="")


def history_text(text: str) -> str:
    """What a bot answer contributes to the conversation history sent to the rewrite: the first paragraph only (the fixed caveats
    and notes that follow it after a blank line are not part of what was said)."""
    return (text or "").split("\n\n", 1)[0].strip()


def to_view(reply, *, dev: bool = False) -> ViewModel:
    status = reply.status
    vm = ViewModel(status=status, kind=KIND.get(status, "neutral"), body=escape_markdown(reply.text))
    if status == "partial" and (reply.missing or "").strip():
        vm.notes.append(config.MISSING_NOTE)                           # fixed; the model's wording stays in debug
    if status in STATUSES_WITH_SOURCES:
        vm.sources = source_views(reply.sources)
        if vm.sources:
            vm.sources_note = config.SOURCES_NOTE
    if status == "clarify":
        topics = (reply.debug.get("conflict_topics") or [])[:config.CLARIFY_MAX_OPTIONS]
        vm.options = [ClarifyOption(retrieve.topic_name(t), t) for t in topics]
    if status == "greeting":
        vm.show_examples = bool(config.EXAMPLE_QUESTIONS)
    if dev:
        d = reply.debug
        vm.dev = {"status": status, "calls": d.get("calls"), "guard": d.get("guard"), "rewrite": d.get("rewrite"),
                  "retrieval": d.get("retrieval"), "model": d.get("model"), "missing (model, not verified)": reply.missing,
                  "removed_claims": d.get("removed_claims"), "style_issues": d.get("style_issues"),
                  "retry": d.get("retry_reason"), "pii_masked": d.get("pii_masked")}
    return vm
