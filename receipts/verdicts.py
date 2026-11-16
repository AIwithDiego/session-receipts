"""Verdicts on the judged rules: the ones with no check in backticks.

The agent running the skill reads a session's evidence (`receipts judge`),
decides kept or broken, and records it here with the log line that shows it
(`receipts verdict`). Receipts then show that verdict instead of "not judged yet".

Stored in ~/.session-receipts/verdicts.json:
  {session id: {rule id: {"status", "why", "ref": {"file", "line"} | null, "at"}}}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .redact import redact
from .window import parse_ts

STATUSES = ("kept", "broken")
WHY_CHARS = 400
# A session judged while still running (often the one doing the judging) only counts as
# having moved on once it was active this long after the verdict.
GRACE = timedelta(minutes=10)


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def record(path: Path, items: list[dict]) -> int:
    """Add or replace verdicts. Each item: session, rule, status, why, optional ref "file:line".
    Raises ValueError on a bad item, before anything is written."""
    clean = [_clean(i) for i in items]
    data = load(path)
    for sid, rid, v in clean:
        data.setdefault(sid, {})[rid] = v
    save(path, data)
    return len(clean)


def _clean(i: dict) -> tuple[str, str, dict]:
    sid, rid, status = str(i.get("session", "")).strip(), str(i.get("rule", "")).strip(), i.get("status")
    why = str(i.get("why", "")).strip()
    if not sid or not rid:
        raise ValueError("each verdict needs a session and a rule id")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}, not {status!r}")
    if not why:
        raise ValueError("say why: one plain sentence a person can check against the log line")
    ref = _ref(i.get("ref"))
    if status == "broken" and not ref:
        raise ValueError("a broken rule needs the ref of the log line that shows it")
    return sid, rid, {"status": status, "why": redact(why)[:WHY_CHARS], "ref": ref,
                      "at": datetime.now().astimezone().isoformat(timespec="seconds")}


def _ref(ref) -> dict | None:
    if not ref:
        return None
    if isinstance(ref, dict):
        file, line = str(ref.get("file") or ""), str(ref.get("line") or "")
    else:
        file, _, line = str(ref).rpartition(":")
    if not file or not line.isdigit() or int(line) < 1:
        raise ValueError(f"ref must look like path/to/log.jsonl:123, not {ref!r}")
    return {"file": file, "line": int(line)}


def stale(v: dict, session) -> bool:
    """The session went on after this verdict, so it may have broken the rule since."""
    at = parse_ts(v.get("at"))
    times = [parse_ts(session.ended)] + [parse_ts(x.at) for x in (*session.prompts, *session.actions)]
    last = max((x for x in times if x), default=None)
    return bool(at and last and last > at + GRACE)
