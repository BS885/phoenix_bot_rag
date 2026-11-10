"""Question classification in code, before any search or model call (stage 5.3, plan section 12).

classify(question) -> Guard:
  kind "greeting" : the whole question is a greeting / thanks                  -> fixed reply, no search
  kind "personal" : asks about the person's own case (status, balance ...)     -> refer to the personal area, no search
  kind "advice"   : asks what to buy / join / choose                           -> polite refusal + general information
  kind "none"     : search normally; flags add caveats to the answer:
      coverage           : asks about coverage / eligibility / a refund amount (the site's text only, no promise)
      suspected_personal : "mine / ours" wording without a clear case lookup
Priority: coverage > pure personal > advice > greeting. A how-to word turns a pure-personal phrase and a weak advice
phrase into a procedure question ("כדאי לי לצרף מסמך לתביעה?" is about the process, not advice).
"""
import re
from dataclasses import dataclass, field

import config

_NIQQUD = re.compile(r"[֑-ׇ]")
_QUOTES = re.compile("[\"'׳״`´’‘“”]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_RUNS = re.compile(r"(.)\1{2,}")


def normalize(text: str) -> str:
    text = _NIQQUD.sub("", text.lower())
    text = _QUOTES.sub("", text)
    text = _PUNCT.sub(" ", text)
    text = _RUNS.sub(r"\1\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _hits(norm: str, phrases) -> list[str]:
    """Substring match (Hebrew prefixes: 'ולכיסוי', 'מהתביעה')."""
    return [p for p in phrases if normalize(p) in norm]


@dataclass
class Guard:
    kind: str = "none"                       # greeting | personal | advice | none
    coverage: bool = False
    suspected_personal: bool = False
    matched: list[str] = field(default_factory=list)


def _is_greeting(norm: str) -> bool:
    if not norm or len(norm.split()) > config.GUARD_GREETING_MAX_WORDS:
        return False
    rest = f" {norm} "
    for g in sorted({normalize(x) for x in config.GUARD_GREETING}, key=len, reverse=True):
        rest = rest.replace(f" {g} ", " ")
    return not rest.strip()


def personal_reply() -> tuple[str, list[dict]]:
    """The fixed answer to a pure-personal question (kind "personal"): text from config, plus its source page."""
    text = config.PERSONAL_REPLY.format(**config.PERSONAL_FACTS)
    src = config.PERSONAL_FACTS_SOURCE
    return text, [{"title": src["page_title_section"], "url": src["page_url"], "anchor": ""}]


def classify(question: str) -> Guard:
    norm = normalize(question)
    out = Guard()
    how_to = _hits(norm, config.GUARD_PROCEDURE)
    suspect = _hits(norm, config.GUARD_SUSPECT_WORDS)

    cov = _hits(norm, config.GUARD_COVERAGE) + [m.group(0) for p in config.GUARD_COVERAGE_PATTERNS
                                                for m in [re.search(p, norm)] if m]
    if cov:                                   # coverage wins over personal: the search runs, the caveat is added
        out.coverage, out.matched = True, cov
        return out

    pure = _hits(norm, config.GUARD_PERSONAL_PURE) + re.findall(config.GUARD_PERSONAL_ID_PATTERN, norm)
    pure += [p for k in ("id", "phone") for p in [config.PII_PLACEHOLDERS[k]] if p in question]   # masked ID / phone
    if config.PII_PLACEHOLDERS["email"] in question:
        suspect = suspect + [config.PII_PLACEHOLDERS["email"]]
    if pure and not how_to:
        out.kind, out.matched = "personal", pure
        return out

    adv = _hits(norm, config.GUARD_ADVICE_STRONG) + _hits(norm, config.GUARD_CHOICE_VERBS)
    if not adv and not _hits(norm, config.GUARD_PROCEDURE_VERBS):
        adv = _hits(norm, config.GUARD_BEST_PHRASES)
    if not adv and not how_to:
        adv = _hits(norm, config.GUARD_ADVICE_WEAK)
    if adv:
        out.kind, out.matched = "advice", adv
        return out

    if _is_greeting(norm):
        out.kind = "greeting"
        return out

    if pure or suspect:
        out.suspected_personal, out.matched = True, pure or suspect
    return out
