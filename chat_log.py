"""Optional chat log for tuning (stage 6.2). OFF unless PHOENIX_CHAT_LOG=1 is set in the ENVIRONMENT (never from the UI), and
off in the demo. A record holds only masked text, the status and retrieval numbers: never the model's raw JSON, no user id,
no address. The file is created only when the first record is written.
"""
import json
import os
import threading
import time
from pathlib import Path

import config
import privacy

_LOCK = threading.Lock()


def enabled() -> bool:
    return bool(config.CHAT_LOG_ENABLED) or os.environ.get("PHOENIX_CHAT_LOG", "") == "1"


def build_record(question: str, reply) -> dict:
    """One log line for a turn. `question` is masked again here, whatever the caller did."""
    d = reply.debug or {}
    groups = (d.get("retrieval") or {}).get("groups") or []
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": privacy.mask_text(question),
        "status": reply.status,
        "groups": [{"url": g.get("url"), "section": g.get("section"), "distance": g.get("distance")} for g in groups],
        "llm_calls": d.get("calls"),
        "retry": d.get("retry_reason"),
        "style_retry": d.get("style_retry"),
        "removed_claims": len(d.get("removed_claims") or []),
        "pii_masked": d.get("pii_masked"),
        "error": bool(d.get("error")),
    }


def log_turn(question: str, reply, path: Path | str | None = None) -> bool:
    """Append a record when logging is enabled. Returns True when something was written."""
    if not enabled():
        return False
    target = Path(path or config.CHAT_LOG_PATH)
    line = json.dumps(build_record(question, reply), ensure_ascii=False)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return True
