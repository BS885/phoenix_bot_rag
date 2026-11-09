"""The bot's single entry point: answer(question, history) -> BotReply (stage 5.4).

Flow (1-2 model calls per question; a failed citation check may add one retry):
  1. rewrite.py        follow-up -> standalone question (a model call only when there is history and it looks like a follow-up);
                       guardrail flags of the original and the rewrite are merged
  2. guardrails        greeting / personal / advice are answered here with fixed texts: no search, no answer call
  3. retrieve          groups of chunks (a product named earlier in the conversation is inherited)
  4. answer call       prompt.py -> llm.generate -> JSON
  5. verification      verify.py: citations and numbers/links, in code
       no valid citation       -> ONE retry with a note ("copy the quote word for word"); still invalid -> not_found
       unsupported values      -> the WHOLE sentence holding the value is removed; if no remaining sentence is backed by a
                                  valid citation -> not_found, else partial + a fixed note
  6. statuses          answered, partial, not_found, out_of_scope, personal, advice, greeting, clarify, error
Fixed texts and caveats come from config.py. A quota error or any model failure is "error", never "not_found".
Sources (title + url + anchor) are taken from the chunks in code; the model only gives source numbers.
"""
import re
from dataclasses import dataclass, field

import config
import guardrails
import llm
import prompt as prompt_mod
import rewrite as rewrite_mod
import verify
from bot import retrieve


@dataclass
class BotReply:
    status: str
    text: str
    sources: list[dict] = field(default_factory=list)      # [{"title", "url", "anchor"}]
    is_coverage: bool = False
    missing: str = ""
    debug: dict = field(default_factory=dict)


# ---------- small pure helpers (tested directly) ----------

_SPLIT = re.compile(r"((?<=[.!?])\s+|\n+)")


def split_sentences(text: str) -> list[tuple[str, str]]:
    """[(sentence, separator that followed it)]"""
    parts = _SPLIT.split(text.strip())
    out = []
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        if sentence.strip():
            out.append((sentence, sep))
    return out


def remove_sentences_with(text: str, bad_numbers: list[str], bad_links: list[str]) -> tuple[str, list[str]]:
    """Drops every WHOLE sentence that holds one of the unsupported values. Returns (new text, removed sentences)."""
    bad_n, bad_l = set(bad_numbers), set(bad_links)
    kept, removed = [], []
    for sentence, sep in split_sentences(text):
        vals = verify.extract_values(sentence)
        if (set(vals["numbers"]) & bad_n) or (set(vals["links"]) & bad_l) or (set(vals["emails"]) & bad_l):
            removed.append(sentence)
        else:
            kept.append((sentence, sep))
    text_out = ""
    for i, (sentence, sep) in enumerate(kept):
        text_out += sentence + (sep if i < len(kept) - 1 else "")
    return text_out.strip(), removed


def backed_sentences(text: str, cited_texts: list[str]) -> list[str]:
    """The sentences of `text` whose words (mostly) appear in the validly cited sources."""
    pool = set()
    for t in cited_texts:
        pool.update(verify.words(t))
    out = []
    for sentence, _ in split_sentences(text):
        ws = [w for w in verify.words(sentence) if len(w) > 1]
        if ws and sum(w in pool for w in ws) / len(ws) >= config.ANSWER_SUPPORT_MIN_RATIO:
            out.append(sentence)
    return out


def _unique_sources(prompt_sources: list[dict], used: list[int]) -> list[dict]:
    out, seen = [], set()
    for s in prompt_sources:
        if s["n"] not in used:
            continue
        for src in s.get("sources") or [{"title": s["title"], "url": s["url"], "anchor": s["anchor"]}]:
            key = src["url"]
            if key not in seen:
                seen.add(key)
                out.append({"title": src.get("title") or s["title"], "url": src["url"], "anchor": src.get("anchor", "")})
    return out


def _join_options(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " או " + names[-1]


def _error(exc: Exception | None, debug: dict) -> BotReply:
    quota = isinstance(exc, llm.LLMQuotaError)
    text = config.ERROR_TEXT
    if quota:
        text = config.ERROR_QUOTA_DAILY_TEXT if getattr(exc, "daily", False) else config.ERROR_QUOTA_TEXT
    return BotReply("error", text, debug={**debug, "error": str(exc)[:300] if exc else "", "quota": quota})


def _caveats(text: str, guard: guardrails.Guard, model_coverage: bool) -> tuple[str, bool]:
    coverage = guard.coverage or model_coverage
    if coverage:
        text += "\n\n" + config.COVERAGE_CAVEAT
    if guard.suspected_personal:
        text += "\n\n" + config.SUSPECTED_PERSONAL_CAVEAT
    return text, coverage


# ---------- the flow ----------

def answer(question: str, history: list[dict] | None = None, *, retriever=None, generate=None) -> BotReply:
    """history: [{"role": "user" | "bot", "text": str, "status": str (bot turns)}]. `retriever` and `generate` are injectable."""
    history = history or []
    generate = generate or llm.generate
    debug: dict = {"calls": 0}

    def call(user_prompt, built):
        debug["calls"] += 1
        return generate(user_prompt, built.schema, system=built.system, model=config.LLM_MODEL_ANSWER)

    # 1. rewrite + guardrails
    rw = rewrite_mod.rewrite(question, history, generate=generate)
    debug["calls"] += int(rw.called_model)
    debug["rewrite"] = {"question": rw.question, "rewritten": rw.rewritten, "error": rw.error,
                        "inherited_product": rw.inherited_product, "products": rw.products}
    guard = rw.guard
    debug["guard"] = {"kind": guard.kind, "coverage": guard.coverage, "suspected_personal": guard.suspected_personal,
                      "matched": guard.matched}
    if rw.quota_error:
        return _error(llm.LLMQuotaError(rw.error, daily="daily" in rw.error), debug)

    # 2. answers that need no search
    if guard.kind == "greeting":
        return BotReply("greeting", config.GREETING_TEXT, debug=debug)
    if guard.kind == "personal":
        text, sources = guardrails.personal_reply()
        return BotReply("personal", text, sources=sources, debug=debug)
    if guard.kind == "advice":
        return BotReply("advice", config.ADVICE_TEXT, debug=debug)

    # 3. retrieval
    if retriever is None:
        retriever = retrieve.Retriever()
    ret = retriever.retrieve(rw.question, products=rw.products)
    debug["retrieval"] = ret.debug()
    if not ret.groups:
        return BotReply("not_found", config.NOT_FOUND_TEXT, debug=debug)
    already_clarified = any(t.get("status") == "clarify" for t in history)
    conflict = ret.product_conflict and not already_clarified

    # 4. answer call (also on a product conflict: an out-of-scope question must be decided before clarify)
    built = prompt_mod.build(rw.question, ret.groups)
    try:
        reply = call(built.user, built)
    except llm.LLMError as exc:
        return _error(exc, debug)
    debug["model"] = {k: reply.get(k) for k in ("found", "is_coverage", "is_personal", "out_of_scope", "missing")}

    if reply.get("out_of_scope"):
        return BotReply("out_of_scope", config.OUT_OF_SCOPE_TEXT, debug=debug)
    if conflict:
        topics = ret.conflict_topics[:config.CLARIFY_MAX_OPTIONS]      # best groups first, different products
        names = [retrieve.topic_name(t) for t in topics]
        debug["conflict_topics"] = ret.conflict_topics
        return BotReply("clarify", config.CLARIFY_TEXT.format(options=_join_options(names)), debug=debug)
    if reply.get("is_personal") and not reply.get("found"):
        text, sources = guardrails.personal_reply()
        return BotReply("personal", text, sources=sources, debug=debug)
    if not reply.get("found") or not str(reply.get("answer", "")).strip():
        return BotReply("not_found", config.NOT_FOUND_TEXT, debug=debug)

    # 5. verification (+ one retry when no citation is valid)
    check = verify.verify(reply["answer"], reply.get("citations") or [], built.sources)
    debug["verify"] = {"hint": check.hint, "used_sources": check.used_sources,
                       "citations": [(c.n, c.ok, c.reason) for c in check.citations]}
    if not check.has_valid_citation:
        try:
            reply = call(built.user + "\n\n" + config.CITATION_RETRY_NOTE, built)
        except llm.LLMError as exc:
            return _error(exc, debug)
        debug["retried_citation"] = True
        if reply.get("out_of_scope") or not reply.get("found"):
            return BotReply("not_found", config.NOT_FOUND_TEXT, debug=debug)
        check = verify.verify(reply.get("answer", ""), reply.get("citations") or [], built.sources)
        debug["verify_retry"] = {"hint": check.hint, "used_sources": check.used_sources,
                                 "citations": [(c.n, c.ok, c.reason) for c in check.citations]}
        if not check.has_valid_citation:
            return BotReply("not_found", config.NOT_FOUND_TEXT, debug=debug)

    text = str(reply["answer"]).strip()
    missing = str(reply.get("missing", "") or "").strip()
    status = "partial" if missing else "answered"
    if check.unsupported_numbers or check.unsupported_links:
        text, removed = remove_sentences_with(text, check.unsupported_numbers, check.unsupported_links)
        debug["removed_sentences"] = removed
        cited = [s["text"] for s in built.sources if s["n"] in check.used_sources]
        if not text or not backed_sentences(text, cited):
            return BotReply("not_found", config.NOT_FOUND_TEXT, debug=debug)
        text += "\n\n" + config.PARTIAL_REMOVED_NOTE
        status = "partial"

    text, coverage = _caveats(text, guard, bool(reply.get("is_coverage")))
    return BotReply(status, text, sources=_unique_sources(built.sources, check.used_sources),
                    is_coverage=coverage, missing=missing, debug=debug)
