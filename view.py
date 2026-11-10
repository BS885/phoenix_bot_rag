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
    plain: str = ""                                 # the answer text exactly as produced (NOT markdown-escaped)
    items: list = field(default_factory=list)       # 2+ claims: one markdown-safe line each (the UI draws a list)
    claims: list = field(default_factory=list)      # the claims as plain text (for the copy areas)
    plain_notes: list = field(default_factory=list)  # all fixed notes and caveats, plain text, in display order (for the copy areas)


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
    return ViewModel(status="notice", kind="neutral", body=escape_markdown(text), footer="", plain=text)


def history_text(text: str) -> str:
    """What a bot answer contributes to the conversation history sent to the rewrite: the first paragraph only (the fixed caveats
    and notes that follow it after a blank line are not part of what was said)."""
    return (text or "").split("\n\n", 1)[0].strip()


STATUS_LABELS = {"answered": "נענתה", "partial": "חלקית", "not_found": "לא נמצא", "out_of_scope": "מחוץ להיקף",
                 "personal": "שאלה אישית", "advice": "בקשת ייעוץ", "greeting": "ברכה", "clarify": "שאלת הבהרה", "error": "תקלה",
                 "notice": "הודעת מערכת"}
REASON_LABELS = {"not_in_source": "הציטוט לא נמצא במקור", "ellipsis": "ציטוט עם שלוש נקודות", "too_short": "ציטוט קצר מדי",
                 "empty_quote": "ציטוט ריק", "empty_text": "טענה ריקה", "bad_source_number": "מספר מקור לא תקין",
                 "low_overlap": "חפיפה נמוכה בין הטענה לציטוט", "unsupported_value": "מספר, טלפון או קישור שלא מופיע במקור",
                 "strength_word": "החלשה או הגברה של הניסוח"}
CAVEAT_NOTES = (config.COVERAGE_CAVEAT, config.SUSPECTED_PERSONAL_CAVEAT)
BULLET = "\u2022"
RULE_LINE = "\u2501\u2501"


def _answer_lines(vm: "ViewModel") -> list[str]:
    """The answer block of the copy areas: claims as a list (2+) or plain text, then each note / caveat on its own line, then the
    numbered sources (title and collection date, the address on the next line)."""
    lines = []
    if vm.claims:
        lines += [f"{BULLET} {c}" for c in vm.claims] if len(vm.claims) >= 2 else [vm.claims[0]]
    else:
        lines.append(vm.plain)
    for n in vm.plain_notes:
        lines.append(("הסתייגות: " if n in CAVEAT_NOTES else "הערה: ") + n)
    if vm.sources:
        lines.append("מקורות:")
        for i, s in enumerate(vm.sources, 1):
            lines.append(f"{i}. {s.title or s.url}" + (f" \u2014 נאסף: {s.date}" if s.date else ""))
            lines.append(f"   {s.url}")
    return lines


def transcript(messages: list[dict], *, dev: bool = False) -> str:
    """The whole conversation as plain Hebrew text. Per message: a numbered rule line, the question (masked), the status in Hebrew,
    the answer as a list, each note / caveat on its own line, the numbered sources. With dev=True ALSO: the rewritten question, the
    retrieved groups (distance | section title, the address on the next line) and the removed claims with the reason (in Hebrew).
    Pure: no Streamlit, no disk. Questions are masked again here; answers are not masked (verified site text: the site's own phone
    numbers must stay)."""
    import privacy
    if not messages:
        return ""
    lines = ["שיחה עם עוזר המידע לאתר הפניקס", "פרטים אישיים בשאלות הוחלפו בסימנים (למשל [טלפון]).", ""]
    n = 0
    for m in messages:
        vm = m.get("view")
        if m.get("role") == "user":
            n += 1
            lines += [f"{RULE_LINE} הודעה {n} {RULE_LINE}", f"שאלה: {privacy.mask_text(m.get('text', ''))}"]
            continue
        if vm is None:
            continue
        if vm.status == "notice":
            n += 1
            lines += [f"{RULE_LINE} הודעה {n} {RULE_LINE}", f"הודעת מערכת: {vm.plain}", ""]
            continue
        lines += [f"סטטוס: {STATUS_LABELS.get(vm.status, vm.status)}", "תשובה:"] + _answer_lines(vm)
        if dev and vm.dev:
            d = vm.dev
            rewritten = (d.get("rewrite") or {}).get("question")
            groups = (d.get("retrieval") or {}).get("groups") or []
            removed = d.get("removed_claims") or []
            if rewritten or groups or removed:
                lines.append("[מצב מפתחת]")
            if rewritten:
                lines.append(f"שכתוב: {privacy.mask_text(rewritten)}")
            if groups:
                lines.append("קבוצות שנשלפו:")
                for i, g in enumerate(groups, 1):
                    lines.append(f"{i}. {g.get('distance')} | {g.get('section') or g.get('page_title') or ''}")
                    lines.append(f"   {g.get('url')}")
            if removed:
                lines.append("טענות שהוסרו:")
                lines += [f"- {REASON_LABELS.get(c.get('reason'), 'סיבה לא מוכרת')}: {c.get('text')}" for c in removed]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def last_answer(messages: list[dict]) -> str:
    """The FINAL bot answer alone, same layout as one message of the transcript but without the question, the status and any
    developer details. Empty string when there is no answer yet. Pure: no Streamlit, no disk."""
    for m in reversed(messages or []):
        vm = m.get("view")
        if m.get("role") != "bot" or vm is None or vm.status == "notice":
            continue
        return "\n".join(_answer_lines(vm)).rstrip() + "\n"
    return ""


def to_view(reply, *, dev: bool = False) -> ViewModel:
    status = reply.status
    claims = list(getattr(reply, "claims", None) or [])
    vm = ViewModel(status=status, kind=KIND.get(status, "neutral"), body=escape_markdown(reply.text), plain=reply.text, claims=claims)
    if claims:                                                         # the answer is drawn from the verified claims, not from the joined text
        vm.body = escape_markdown(claims[0]) if len(claims) == 1 else ""
        vm.items = [escape_markdown(c) for c in claims] if len(claims) >= 2 else []
        notes = list(getattr(reply, "notes", None) or [])
        if status == "partial" and (reply.missing or "").strip():       # fixed; the model's wording stays in debug
            notes.insert(1 if notes and notes[0] == config.PARTIAL_REMOVED_NOTE else 0, config.MISSING_NOTE)
        vm.notes = notes
        vm.plain_notes = list(notes)
    else:
        notes = list(getattr(reply, "notes", None) or [])
        if status == "partial" and (reply.missing or "").strip():
            notes.insert(0, config.MISSING_NOTE)
        if notes and reply.text.endswith(tuple(notes)):                 # fixed texts that already carry their notes (not_found + caveat)
            vm.body = escape_markdown(reply.text.split("\n\n", 1)[0])
            vm.plain = reply.text.split("\n\n", 1)[0]
        vm.notes, vm.plain_notes = notes, list(notes)
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
