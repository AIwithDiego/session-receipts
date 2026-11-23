"""Reader for Codex logs: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

Reads the `item_completed` events (Codex's own record of each finished step),
not the raw model calls: those wrap shell commands inside JavaScript and change
shape between versions. The format is undocumented, so anything unknown is
noted on the session instead of guessed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Action, Denial, Prompt, Ref, Session, Tokens
from .redact import clip, tail
from .window import Window, iso

DEFAULT_ROOT = Path.home() / ".codex" / "sessions"

BLOCKED = {"declined", "rejected", "denied", "aborted_by_user"}


def find_logs(window: Window, root: Path = DEFAULT_ROOT) -> list[Path]:
    if not root.is_dir():
        return []
    since = window.start_epoch
    return sorted(p for p in root.glob("*/*/*/*.jsonl") if p.stat().st_mtime >= since)


def read_session(path: Path, window: Window) -> Session:
    s = Session(tool="codex", id=path.stem, cwd="", log=str(path))
    seen_responses: set[str] = set()
    items = 0
    bad_lines = 0
    unknown: set[str] = set()
    meta_seen = False

    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(d, dict):
                continue
            kind = d.get("type")
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            ts = d.get("timestamp")

            if kind == "session_meta":
                if not meta_seen:  # a forked session also carries its parent's session_meta further down
                    s.id = p.get("id") or s.id
                    meta_seen = True
                s.cwd = p.get("cwd") or s.cwd
                continue
            if kind == "turn_context":
                model = p.get("model")
                if model and model not in s.models:
                    s.models.append(model)
                sandbox = p.get("sandbox_policy") if isinstance(p.get("sandbox_policy"), dict) else {}
                s.permission_mode = f"approval {p.get('approval_policy', '?')} · sandbox {sandbox.get('type', '?')}"
                s.cwd = p.get("cwd") or s.cwd
                continue

            if not window.has(ts):
                continue
            at = iso(ts)
            ref = Ref(str(path), n)

            if kind == "token_usage_record":
                rid = p.get("response_id") or f"line{n}"
                u = p.get("usage") if isinstance(p.get("usage"), dict) else None
                if u and rid not in seen_responses:
                    seen_responses.add(rid)
                    cached = u.get("cached_input_tokens") or 0
                    s.tokens.add(Tokens(
                        input=max((u.get("input_tokens") or 0) - cached, 0),  # Codex counts cached inside input
                        cache_read=cached,
                        cache_write=u.get("cache_write_input_tokens") or 0,
                        output=u.get("output_tokens") or 0,
                        reasoning=u.get("reasoning_output_tokens") or 0,
                    ))
                continue

            if kind == "event_msg" and p.get("type") == "item_completed" and isinstance(p.get("item"), dict):
                items += 1
                s.started = min(s.started or at, at)
                s.ended = max(s.ended, at)
                _item(p["item"], at, ref, s, unknown)

    if bad_lines:
        s.notes.append(f"{bad_lines} unreadable line(s)")
    if items == 0 and path.stat().st_size > 0 and window.has(_first_ts(path)):
        s.notes.append("No item_completed events: older Codex log format, not read")
    if unknown:
        s.notes.append("Unknown Codex step types skipped: " + ", ".join(sorted(unknown)))
    s.actions.sort(key=lambda a: a.at)
    return s


def _item(item: dict, at: str, ref: Ref, s: Session, unknown: set[str]) -> None:
    t = item.get("type")
    status = str(item.get("status") or "completed")

    if t == "UserMessage":
        text = " ".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)).strip()
        if text:
            s.prompts.append(Prompt(at=at, text=text, ref=ref))
        return

    if t == "CommandExecution":
        a = Action(at, "command", "exec", _command(item.get("command")), ref)
        if status != "completed":
            a.ok = False
            out = str(item.get("aggregated_output") or "").strip()
            a.error = f"exit {item.get('exit_code')}: {tail(out, 300)}" if status == "failed" else status
    elif t == "FileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
        files = sorted(changes)
        a = Action(at, "edit", "apply_patch", clip(", ".join(files), 200), ref, files=files)
        if status != "completed":
            a.ok = False
            a.error = clip(str(item.get("stderr") or status), 300)
    elif t == "McpToolCall":
        name = f"{item.get('server', '?')}.{item.get('tool', '?')}"
        args = item.get("arguments")
        detail = clip(json.dumps(args, ensure_ascii=False), 200) if args is not None else ""
        a = Action(at, "tool", name, detail, ref)
        if status != "completed":
            a.ok = False
            a.error = clip(str(item.get("error") or status), 300)
    elif t == "Extension":
        kind = str(item.get("kind") or "")
        a = Action(at, "web" if kind.startswith("web") else "tool", kind or "extension", clip(str(item.get("query") or ""), 200), ref)
    elif t in ("Reasoning", "AgentMessage", "Plan", "ContextCompaction"):
        return
    else:
        unknown.add(str(t))
        return

    s.actions.append(a)
    if status in BLOCKED:
        s.denials.append(Denial(at=at, what=a.detail, reason=status, source="user" if "user" in status else "permission", ref=ref))


def _command(cmd) -> str:
    """['/bin/zsh', '-lc', 'git push'] -> 'git push'."""
    if isinstance(cmd, list):
        if len(cmd) >= 3 and cmd[1] in ("-c", "-lc", "-l"):
            return str(cmd[-1])
        return " ".join(str(c) for c in cmd)
    return str(cmd or "")


def _first_ts(path: Path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                return json.loads(line).get("timestamp")
            except (json.JSONDecodeError, AttributeError):
                return None
    return None


def read_all(window: Window, root: Path = DEFAULT_ROOT) -> list[Session]:
    sessions = (read_session(p, window) for p in find_logs(window, root))
    return [s for s in sessions if not s.empty() or any("older" in n for n in s.notes)]
