"""The bot's single entry point: answer(question, history) -> BotReply (stage 5.4).

Flow (1-2 model calls per question; a failed citation check may add one retry):
  1. rewrite.py        follow-up -> standalone question (a model call only when there is history and it looks like a follow-up);
                       guardrail flags of the original and the rewrite are merged
  2. guardrails        greeting / personal / advice are answered here with fixed texts: no search, no answer call
  3. retrieve          groups of chunks (a product named earlier in the conversation is inherited)
  4. answer call       prompt.py -> llm.generate -> JSON
  5. verification      verify.verify_claims: every claim (text + source + quote) is checked on its own
       no valid claim          -> ONE retry with a note ("copy the quote word for word"); still none -> not_found
       some claims failed      -> those claims are removed whole; the others stay: partial + a fixed note
  6. statuses          answered, partial, not_found, out_of_scope, personal, advice, greeting, clarify, error
Fixed texts and caveats come from config.py. A quota error or any model failure is "error", never "not_found".
Sources (title + url + anchor) are taken from the chunks in code; the model only gives source numbers.
"""
from dataclasses import dataclass, field

import config
import guardrails
import llm
import privacy
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
    claims: list[str] = field(default_factory=list)        # the verified claims as shown, one per item (the list the UI draws)
    notes: list[str] = field(default_factory=list)         # fixed notes / caveats that follow the claims, in order


# ---------- small pure helpers (tested directly) ----------

def _unique_sources(prompt_sources: list[dict], used: list[int]) -> list[dict]:
    out, seen = [], set()
    for s in prompt_sources:
        if s["n"] not in used:
            continue
        for src in s.get("sources") or [{"title": s["title"], "url": s["url"], "anchor": s["anchor"]}]:
            key = src["url"]
            if key not in seen:
                seen.add(key)
                out.append({"title": src.get("title") or s["title"], "url": src["url"], "anchor": src.get("anchor", ""),
                            "collected_at": src.get("collected_at", "")})
    return out


def _verify_debug(res) -> dict:
    """What verification did with every claim, and why a claim was removed."""
    return {"valid": len(res.valid), "dropped": len(res.dropped), "used_sources": res.used_sources,
            "claims": [{"n": c.n, "source": c.source, "ok": c.ok, "reason": c.reason, "note": c.note, "style": c.style,
                        "text": c.text, "quote": c.quote} for c in res.claims]}


def _join_options(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " או " + names[-1]


def _error(exc: Exception | None, debug: dict) -> BotReply:
    if isinstance(exc, llm.LLMBusyError):             # waiting would take too long: a fixed "busy" message, no call was made
        return BotReply("error", config.BUSY_TEXT, debug={**debug, "error": str(exc)[:300], "quota": False, "busy": True})
    quota = isinstance(exc, llm.LLMQuotaError)
    text = config.ERROR_TEXT
    if quota:
        text = config.ERROR_QUOTA_DAILY_TEXT if getattr(exc, "daily", False) else config.ERROR_QUOTA_TEXT
    return BotReply("error", text, debug={**debug, "error": str(exc)[:300] if exc else "", "quota": quota})


def _caveats(guard: guardrails.Guard, model_coverage: bool, model_personal: bool = False) -> tuple[list[str], bool]:
    """Fixed caveats. The model's is_personal flag never changes the status: it only asks for the suspected-personal caveat.
    A coverage caveat already says that nothing here is a personal confirmation, so it replaces the suspected-personal one."""
    coverage = guard.coverage or model_coverage
    if coverage:
        return [config.COVERAGE_CAVEAT], True
    if guard.suspected_personal or model_personal:
        return [config.SUSPECTED_PERSONAL_CAVEAT], False
    return [], False


# ---------- the flow ----------

def answer(question: str, history: list[dict] | None = None, *, retriever=None, generate=None,
           products: list[str] | None = None) -> BotReply:
    """history: [{"role": "user" | "bot", "text": str, "status": str (bot turns)}]. `retriever` and `generate` are injectable;
    `products` = slugs chosen by the person on a clarify button."""
    # Personal details never leave the process: the question and the history are masked here, whatever the caller did
    masked = privacy.mask(question)
    question = masked.text
    history = privacy.mask_history(history)
    real_generate = generate or llm.generate
    debug: dict = {"calls": 0, "raw_calls": [], "pii_masked": dict(masked.found)}

    def generate(prompt, schema, *, system=None, model=None):          # noqa: F811  (records every model call, retries included)
        entry = {"model": model, "prompt_chars": len(prompt)}
        debug["raw_calls"].append(entry)
        try:
            entry["reply"] = real_generate(prompt, schema, system=system, model=model)
            return entry["reply"]
        except Exception as exc:  # noqa: BLE001
            entry["error"] = repr(exc)[:300]
            raise

    def call(user_prompt, built):
        debug["calls"] += 1
        return generate(user_prompt, built.schema, system=built.system, model=config.LLM_MODEL_ANSWER)

    # 1. rewrite + guardrails
    rw = rewrite_mod.rewrite(question, history, generate=generate)
    if products:                          # the person picked a product on the clarify buttons: it wins over inheritance
        rw.products, rw.inherited_product = list(products), False
    debug["calls"] += int(rw.called_model)
    debug["rewrite"] = {"question": rw.question, "rewritten": rw.rewritten, "error": rw.error,
                        "inherited_product": rw.inherited_product, "products": rw.products}
    guard = rw.guard
    debug["guard"] = {"kind": guard.kind, "coverage": guard.coverage, "suspected_personal": guard.suspected_personal,
                      "matched": guard.matched}
    if rw.busy:
        return _error(llm.LLMBusyError(rw.error), debug)
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
    # the model's is_personal flag NEVER decides the status (only guardrails, by keywords, can say "personal"): it adds a caveat
    model_personal = bool(reply.get("is_personal"))

    def not_found() -> BotReply:
        notes = [config.SUSPECTED_PERSONAL_CAVEAT] if model_personal else []
        return BotReply("not_found", config.NOT_FOUND_TEXT + "".join("\n\n" + n for n in notes), debug=debug, notes=notes)

    if not reply.get("found"):
        return not_found()

    # 5. verification, claim by claim. ONE retry when there is no claim at all ("found" without claims; "missing" alone is
    #    not an answer) or no claim survives verification; it carries a note that says which of the two it was.
    claims = reply.get("claims") or []
    res = verify.verify_claims(claims, built.sources)
    debug["verify"] = _verify_debug(res)
    if not res.valid:
        note = config.CITATION_RETRY_NOTE if claims else config.CLAIM_REQUIRED_NOTE
        try:
            reply = call(built.user + "\n\n" + note, built)
        except llm.LLMError as exc:
            return _error(exc, debug)
        debug["retried_citation"] = True
        debug["retry_reason"] = "no_claims" if not claims else "no_valid_claim"
        model_personal = model_personal or bool(reply.get("is_personal"))
        if reply.get("out_of_scope") or not reply.get("found"):
            return not_found()
        res = verify.verify_claims(reply.get("claims") or [], built.sources)
        debug["verify_retry"] = _verify_debug(res)
        if not res.valid:
            return not_found()

    # a style issue (gendered form) never removes a claim: ONE rephrase request (unless the retry above was already used),
    # and if it does not help the claim is shown as it is and the issue stays in debug
    styled = [c for c in res.valid if c.style]
    if styled and not debug.get("retried_citation"):
        try:
            again = call(built.user + "\n\n" + config.STYLE_RETRY_NOTE, built)
            res2 = verify.verify_claims(again.get("claims") or [], built.sources) if again.get("found") else None
        except llm.LLMError as exc:
            res2 = None
            debug["style_retry_error"] = str(exc)[:200]
        debug["style_retry"] = "not used"
        if res2 and len(res2.valid) >= len(res.valid) and len([c for c in res2.valid if c.style]) < len(styled):
            reply, res = again, res2
            model_personal = model_personal or bool(again.get("is_personal"))
            debug["style_retry"] = "used"
            debug["verify_style_retry"] = _verify_debug(res2)
    elif styled:
        debug["style_retry"] = "skipped (retry already used)"
    debug["style_issues"] = [{"text": c.text, "style": c.style} for c in res.valid if c.style]

    claims = [c.final_text for c in res.valid]
    missing = str(reply.get("missing", "") or "").strip()
    status = "partial" if missing else "answered"
    notes: list[str] = []
    if res.dropped:                       # a claim that failed verification is removed whole; the rest stays
        debug["removed_claims"] = [{"text": c.text, "quote": c.quote, "reason": c.reason, "note": c.note} for c in res.dropped]
        notes.append(config.PARTIAL_REMOVED_NOTE)
        status = "partial"

    caveats, coverage = _caveats(guard, bool(reply.get("is_coverage")), model_personal)
    notes += caveats
    text = " ".join(claims) + "".join("\n\n" + n for n in notes)
    return BotReply(status, text, sources=_unique_sources(built.sources, res.used_sources),
                    is_coverage=coverage, missing=missing, debug=debug, claims=claims, notes=notes)
