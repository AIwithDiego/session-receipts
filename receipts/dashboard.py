"""Build the local dashboard: ~/.session-receipts/index.html + data files.

The page is static and opens offline (file://). The overview data ships as
data.js; each session's full receipt is its own small script, loaded on click,
so the page stays fast with thousands of sessions. Everything is redacted
before it is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import archive, rules as rulebook, verdicts
from .model import Session
from .redact import clip, forget_cache, redact, redact_tree
from .risk import checks_off, command_text, risks
from .window import parse_ts

OUT = Path.home() / ".session-receipts"
TEMPLATE = Path(__file__).parent / "assets" / "dashboard.html"
REPEAT_MIN = 5  # same project + same opening prompt this often = a scheduled run
PROMPT_CHARS = 4000
# Relative weight of each kind of token, so shares reflect effort, not raw counts
# (re-reading cached context is far cheaper than writing output). Ratios, not prices.
WEIGHTS = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}
DETAIL_CHARS = 1500


def entry(s: Session, rules: list | None = None, verdicts: dict | None = None) -> dict:
    """One session, ready to show and archive: redacted summary + receipt + scheduled-run key.
    verdicts: every recorded verdict on judged rules, by session id."""
    rs = risks(s)
    results = rulebook.check(s, rules or [], (verdicts or {}).get(_safe(s.id)))
    summary, detail = _summary(s, rs, ""), _detail(s, rs)
    summary["broken"] = [r.rule.id for r in results if r.status == "broken"]
    summary["tried"] = [r.rule.id for r in results if r.status == "tried"]
    summary["judged"] = [r.rule.id for r in results if r.judged_at]
    detail["rules"] = [r.to_dict() for r in results]
    return {"key": _repeat_key(s), "summary": redact_tree(summary), "detail": redact_tree(detail)}


def rules_info(rules: list, src: Path) -> dict:
    return {"file": str(src), "list": [
        {"id": r.id, "text": r.text, "proven": r.proven, "check": r.check, "error": r.error} for r in rules]}


ARCHIVE_NOTE = "Raw log deleted; this receipt comes from the archive"


def from_archive(e: dict) -> dict:
    """An archived entry whose session isn't in the live logs. Its text is redacted
    again so newer patterns reach old receipts, and it is marked if the log is gone."""
    e = {**e, "summary": redact_tree(e["summary"]), "detail": redact_tree(e["detail"])}
    if not Path(e["detail"].get("log") or "").exists():
        e["summary"]["archived"] = True
        for part in (e["summary"], e["detail"]):
            part["notes"] = [*part.get("notes", []), ARCHIVE_NOTE]
    return e


def assemble(entries: dict[str, dict], rules: dict | None = None) -> tuple[dict, dict[str, dict]]:
    """The overview data (what data.js holds) and each receipt by id."""
    repeats = Counter(e["key"] for e in entries.values() if e["key"])
    rows = []
    for e in entries.values():
        key = e["key"]
        rows.append({**e["summary"], "repeat": key if key and repeats[key] >= REPEAT_MIN else ""})
    rows.sort(key=lambda r: r.get("start") or "")
    data = {
        "built": datetime.now().astimezone().isoformat(timespec="seconds"),
        "home": str(Path.home()),
        "sessions": rows,
        "rules": rules or {"file": "", "list": []},
    }
    return data, {sid: e["detail"] for sid, e in entries.items()}


def build(sessions: list[Session], out: Path = OUT, since: str = "", info: dict | None = None) -> Path:
    """Write the static dashboard. Sessions whose logs are gone come from out/archive.

    since: first local day (YYYY-MM-DD) to show from the archive; "" shows it all.
    info, when given, is filled with counts for the command line.
    """
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    sdir = out / "s"
    if sdir.exists():
        shutil.rmtree(sdir)
    sdir.mkdir(mode=0o700)

    rules, src = rulebook.load(out / "rules.md")
    judged = verdicts.load(out / "verdicts.json")
    live = {}
    for s in sessions:
        e = entry(s, rules, judged)
        live[e["summary"]["id"]] = e

    entries, only, bad = archive.sync(out / "archive", live)
    kept = 0
    for sid in only:
        if entries[sid]["summary"].get("day", "") < since:
            del entries[sid]
            continue
        entries[sid] = from_archive(entries[sid])
        kept += 1

    data, details = assemble(entries, rules_info(rules, src))
    for sid, detail in details.items():
        _write_js(sdir / f"{sid}.js", f"window.__receipt({json.dumps(sid)},", detail, ");")
    _write_js(out / "data.js", "window.RECEIPTS=", data, ";")
    shutil.copyfile(TEMPLATE, out / "index.html")
    os.chmod(out / "index.html", 0o600)
    if info is not None:
        info.update(live=len(live), archived=kept, unreadable=bad)
    forget_cache()
    return out / "index.html"


def _summary(s: Session, rs, repeat: str) -> dict:
    start = parse_ts(s.started)
    local = start.astimezone() if start else None
    end = parse_ts(s.ended)
    first = s.prompts[0].text if s.prompts else ""
    return {
        "id": _safe(s.id),
        "tool": s.tool,
        "project": _project(s.cwd),
        "title": s.title,
        "ask": clip(first, 240),
        "day": local.strftime("%Y-%m-%d") if local else "",
        "hour": local.hour if local else 0,
        "start": s.started,
        "end": s.ended,
        "mins": round((end - start).total_seconds() / 60) if start and end else 0,
        "prompts": len(s.prompts),
        "actions": len(s.actions),
        "commands": len(s.commands),
        "edits": len(s.files_changed),
        "failed": sum(1 for a in s.actions if not a.ok),
        "blocked": len(s.denials),
        "subagents": s.subagents,
        "tokens": s.tokens.total,
        "w": round(weighted(s)),
        "fails": [_sig(a.detail) for a in s.commands if not a.ok][:8],
        "blockedWhat": [_sig(d.what) for d in s.denials][:8],
        "risk": {lvl: n for lvl, n in Counter(r.level for r in rs).items()},
        "riskLabels": sorted({r.label for r in rs}),
        "repeat": repeat,
        "models": s.models,
        "mode": s.permission_mode,
        "checksOff": checks_off(s),
        "notes": s.notes,
    }


def _detail(s: Session, rs) -> dict:
    ref = lambda r: {"file": r.file, "line": r.line} if r else None
    return {
        "id": _safe(s.id),
        "tool": s.tool,
        "project": _project(s.cwd),
        "cwd": s.cwd,
        "branch": s.branch,
        "title": s.title,
        "log": s.log,
        "start": s.started,
        "end": s.ended,
        "models": s.models,
        "mode": s.permission_mode,
        "subagents": s.subagents,
        "notes": s.notes,
        "tokens": {**s.tokens.__dict__, "total": s.tokens.total},
        "prompts": [{"at": p.at, "kind": p.kind, "text": clip(p.text, PROMPT_CHARS), "cut": len(p.text) > PROMPT_CHARS, "ref": ref(p.ref)} for p in s.prompts],
        "actions": [{
            "at": a.at, "kind": a.kind, "name": a.name, "detail": clip(a.detail, DETAIL_CHARS),
            "ok": a.ok, "error": clip(a.error, DETAIL_CHARS), "sub": a.by_subagent, "ref": ref(a.ref),
        } for a in s.actions],
        "files": s.files_changed,
        "risks": [{"at": r.at, "level": r.level, "label": r.label, "detail": clip(r.detail, DETAIL_CHARS), "ref": ref(r.ref)} for r in rs],
        "denials": [{"at": d.at, "source": d.source, "what": clip(d.what, DETAIL_CHARS), "reason": clip(d.reason, DETAIL_CHARS), "ref": ref(d.ref)} for d in s.denials],
    }


def weighted(s: Session) -> float:
    t = s.tokens
    return (t.input * WEIGHTS["input"] + t.cache_write * WEIGHTS["cache_write"]
            + t.cache_read * WEIGHTS["cache_read"] + t.output * WEIGHTS["output"])  # Codex output already includes reasoning


def _sig(cmd: str) -> str:
    """Short, stable form of a command so repeats can be counted: first line, no heredoc, no digits."""
    first = command_text(cmd).strip().splitlines()[0] if cmd.strip() else ""
    return re.sub(r"\d+", "N", redact(first))[:80]


def _repeat_key(s: Session) -> str:
    if not s.prompts:
        return ""
    # Scheduled prompts differ only in dates and times, so digits are ignored.
    norm = re.sub(r"\d+", "#", s.prompts[0].text[:200])
    h = hashlib.sha1(norm.encode()).hexdigest()[:10]
    return f"{_project(s.cwd)}#{h}"


def _project(cwd: str) -> str:
    if not cwd:
        return "?"
    home = str(Path.home())
    if cwd == home:
        return "~"
    return cwd[len(home) + 1:] if cwd.startswith(home + "/") else cwd


def _safe(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:120]


def _write_js(path: Path, head: str, data, tail: str) -> None:
    # "</" is escaped so nothing in a log can close the script tag.
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(head + body + tail)
