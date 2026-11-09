"""Follow-up questions -> one standalone question (stage 5.3, plan section 3).

rewrite(question, history) -> Rewrite
  * no history, or a question that already looks standalone -> no model call (the budget is 1-2 calls per question)
  * a follow-up -> one call to the fast model; any failure (quota, broken JSON, API down) -> the ORIGINAL question
  * the rewrite is validated in code: it may add a general linking term, never numbers; it must be short
  * the product named in the history is inherited when the new question names none (a product named in the new
    question always wins)
  * guardrails run on the original and on the rewrite, and the flags are merged
"""
import re
from dataclasses import dataclass, field

import config
import guardrails
from bot import retrieve

_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}
_NUMBER = re.compile(r"\d+")


@dataclass
class Rewrite:
    question: str                        # what the search uses
    original: str
    rewritten: bool = False              # the model's rewrite was accepted
    called_model: bool = False
    products: list[str] = field(default_factory=list)   # products for the search: named in the question, else inherited
    inherited_product: bool = False
    guard: guardrails.Guard = field(default_factory=guardrails.Guard)
    error: str = ""                      # why the original was kept (empty when nothing went wrong)
    quota_error: bool = False


def looks_like_followup(question: str) -> bool:
    words = question.split()
    if len(words) <= config.REWRITE_SHORT_WORDS:
        return True
    norm = guardrails.normalize(question).split()
    return any(m in norm for m in config.REWRITE_FOLLOWUP_MARKERS)


def history_products(history: list[dict]) -> list[str]:
    """The product the conversation is about: the most recent user message that names one."""
    for turn in reversed(history):
        if turn.get("role") == "user":
            found = retrieve.detect_products(turn.get("text", ""))
            if found:
                return found
    return []


def merge_guards(a: guardrails.Guard, b: guardrails.Guard) -> guardrails.Guard:
    """Flags are united. personal beats advice beats none; a greeting counts only when the ORIGINAL is one."""
    kind = "none"
    for k in ("personal", "advice"):
        if a.kind == k or b.kind == k:
            kind = k
            break
    else:
        if a.kind == "greeting":
            kind = "greeting"
    return guardrails.Guard(kind=kind, coverage=a.coverage or b.coverage,
                            suspected_personal=a.suspected_personal or b.suspected_personal,
                            matched=list(dict.fromkeys(a.matched + b.matched)))


def _valid(rewritten: str, original: str, history: list[dict]) -> str:
    """Empty string when fine, else the reason the rewrite is rejected."""
    if not rewritten.strip():
        return "empty rewrite"
    if len(rewritten) > config.REWRITE_MAX_CHARS:
        return "rewrite too long"
    allowed = set(_NUMBER.findall(original)) | {n for t in history for n in _NUMBER.findall(t.get("text", ""))}
    new = set(_NUMBER.findall(rewritten)) - allowed
    if new:
        return f"rewrite added numbers {sorted(new)}"
    return ""


def _prompt(question: str, history: list[dict]) -> str:
    turns = history[-config.REWRITE_HISTORY_TURNS:]
    lines = [("שאלה" if t.get("role") == "user" else "תשובה") + ": " + t.get("text", "") for t in turns]
    return "השיחה עד כה:\n" + "\n".join(lines) + f"\n\nהשאלה האחרונה: {question}"


def rewrite(question: str, history: list[dict] | None = None, *, generate=None) -> Rewrite:
    import llm
    generate = generate or llm.generate
    history = history or []
    out = Rewrite(question=question, original=question)
    out.products = retrieve.detect_products(question)
    if not out.products:
        out.products = history_products(history)
        out.inherited_product = bool(out.products)

    if history and looks_like_followup(question):
        out.called_model = True
        try:
            reply = generate(_prompt(question, history), _SCHEMA, system=config.REWRITE_SYSTEM,
                             model=config.LLM_MODEL_FAST)
            candidate = str(reply.get("question", "")).strip()
            problem = _valid(candidate, question, history)
            if problem:
                out.error = problem
            else:
                out.question, out.rewritten = candidate, candidate != question
        except llm.LLMQuotaError as exc:
            out.error, out.quota_error = f"quota: {exc}", True
        except llm.LLMError as exc:
            out.error = f"model: {exc}"

    if out.rewritten and not retrieve.detect_products(question):
        named = retrieve.detect_products(out.question)       # the rewrite may name the product itself
        if named:
            out.products, out.inherited_product = named, False
    out.guard = merge_guards(guardrails.classify(question), guardrails.classify(out.question))
    return out
