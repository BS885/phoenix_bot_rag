"""Masking of personal details (stage 6.1): ID number, phone, e-mail. Runs BEFORE anything leaves the process:
the rewrite call, the question embedding, the answer call, the conversation history and the chat log.

mask(text) -> Masked(text, found)      found = {"id": n, "phone": n, "email": n}
The placeholders ([ת״ז], [טלפון], [מייל]) live in config.PII_PLACEHOLDERS; guardrails treat the first two as a sign of a
personal question. Not masked on purpose: short service numbers (3455*, 5432*, *8888 ...) and the site's own numbers inside
an ANSWER (answers are verified against the site, they are not user input). Policy / claim numbers have no uniform shape and
are NOT masked (a documented limit; the opening screen asks users not to type personal details).
"""
import re
from dataclasses import dataclass, field

import config

_B = r"(?<![\d*A-Za-z_])"       # not glued to a digit, a star or a latin letter on the left (a Hebrew prefix like 'ל-' is fine)
_E = r"(?![\d*A-Za-z_])"          # nor on the right

_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_MOBILE = re.compile(_B + r"(?:\+?972[-\s]?|0)5\d[-\s]?\d{3}[-\s]?\d{4}" + _E)
_LANDLINE = re.compile(_B + r"(?:\+?972[-\s]?|0)[23489][-\s]?\d{3}[-\s]?\d{4}" + _E)
_ID_CUE = re.compile(r"((?:ת\.?\s?ז\.?|ת[\"״']ז|תעודת\s+זהות|מספר\s+זהות|זהות)\D{0,12}?)(\d{7,9})(?!\d)")
_ID_NINE = re.compile(_B + r"\d{9}" + _E)


@dataclass
class Masked:
    text: str
    found: dict = field(default_factory=lambda: {"id": 0, "phone": 0, "email": 0})

    @property
    def any(self) -> bool:
        return any(self.found.values())


def mask(text: str) -> Masked:
    ph = config.PII_PLACEHOLDERS
    out = Masked(text=text or "")
    s = out.text

    s, n = _EMAIL.subn(ph["email"], s)
    out.found["email"] += n
    for pat in (_MOBILE, _LANDLINE):
        s, n = pat.subn(ph["phone"], s)
        out.found["phone"] += n
    s, n = _ID_CUE.subn(lambda m: m.group(1) + ph["id"], s)
    out.found["id"] += n
    s, n = _ID_NINE.subn(ph["id"], s)
    out.found["id"] += n
    out.text = s
    return out


def mask_text(text: str) -> str:
    return mask(text).text


def mask_history(history: list[dict] | None) -> list[dict]:
    """A copy of the history with every text masked (other keys untouched)."""
    return [{**t, "text": mask_text(t.get("text", ""))} for t in (history or [])]


def has_placeholder(text: str, kinds=("id", "phone")) -> list[str]:
    """Placeholders of the given kinds that appear in `text` (what guardrails look for)."""
    return [config.PII_PLACEHOLDERS[k] for k in kinds if config.PII_PLACEHOLDERS[k] in (text or "")]
