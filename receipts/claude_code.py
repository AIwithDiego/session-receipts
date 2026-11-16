"""Reader for Claude Code logs: ~/.claude/projects/<project>/<session>.jsonl.

Subagent logs live next to the session in <session>/subagents/agent-*.jsonl;
their actions are folded into the parent session, marked by_subagent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .model import Action, Denial, Prompt, Ref, Session, Tokens
from .redact import clip
from .window import Window, iso

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEARCH_TOOLS = {"Grep", "Glob", "LS", "ToolSearch"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
AGENT_TOOLS = {"Agent", "Task", "Workflow"}

# Text the harness puts in a tool result when something was blocked, and who blocked it.
DENIAL_PATTERNS = [
    (re.compile(r"hook (error|blocked)|PreToolUse|blocked by hook", re.I), "hook"),
    (re.compile(r"doesn't want to proceed|user (rejected|denied|declined)|rejected by user", re.I), "user"),
    (re.compile(r"sandbox", re.I), "sandbox"),
    (re.compile(r"^(<tool_use_error>)?\s*Blocked:|permission (denied|to use)|not allowed|access denied", re.I), "permission"),
]

SKIP_PROMPT_PREFIXES = ("<local-command", "<system-reminder", "<task-notification", "<bash-stdout", "<bash-stderr", "Caveat:")


def find_logs(window: Window, root: Path = DEFAULT_ROOT) -> list[Path]:
    """Session logs touched since the window started (mtime is a cheap first filter)."""
    if not root.is_dir():
        return []
    since = window.start_epoch
    return sorted(p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= since)


def read_session(path: Path, window: Window) -> Session:
    s = Session(tool="claude-code", id=path.stem, cwd="", log=str(path))
    usage: dict[str, Tokens] = {}
    _read_file(path, window, s, usage, by_subagent=False)

    sub_dir = path.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        for sub in sorted(sub_dir.glob("*.jsonl")):
            before = len(s.actions)
            _read_file(sub, window, s, usage, by_subagent=True)
            if len(s.actions) > before:
                s.subagents += 1

    for t in usage.values():
        s.tokens.add(t)
    s.actions.sort(key=lambda a: a.at)
    s.denials.sort(key=lambda d: d.at)
    return s


def _read_file(path: Path, window: Window, s: Session, usage: dict[str, Tokens], by_subagent: bool) -> None:
    pending: dict[str, Action] = {}
    bad_lines = 0
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

            if not by_subagent:
                if kind == "ai-title" and d.get("aiTitle"):
                    s.title = d["aiTitle"]
                elif kind == "permission-mode" and d.get("permissionMode"):
                    s.permission_mode = d["permissionMode"]

            ts = d.get("timestamp")
            if kind not in ("user", "assistant") or not window.has(ts):
                continue
            at = iso(ts)
            ref = Ref(str(path), n)
            if not by_subagent:
                s.cwd = s.cwd or d.get("cwd", "")
                s.branch = d.get("gitBranch") or s.branch
                s.started = min(s.started or at, at)
                s.ended = max(s.ended, at)

            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            if kind == "assistant":
                _assistant(msg, at, ref, s, usage, pending, by_subagent)
            else:
                _user(d, msg, at, ref, s, pending, by_subagent)
    if bad_lines:
        s.notes.append(f"{bad_lines} unreadable line(s) in {path.name}")


def _assistant(msg, at, ref, s, usage, pending, by_subagent):
    model = msg.get("model")
    if model and not model.startswith("<") and model not in s.models:
        s.models.append(model)
    u = msg.get("usage")
    if isinstance(u, dict) and msg.get("id"):
        # One API message is logged once per content block, each with the same usage; keep one.
        usage[msg["id"]] = Tokens(
            input=u.get("input_tokens") or 0,
            cache_read=u.get("cache_read_input_tokens") or 0,
            cache_write=u.get("cache_creation_input_tokens") or 0,
            output=u.get("output_tokens") or 0,
        )
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            a = _action(block.get("name") or "?", block.get("input") or {}, at, ref)
            a.by_subagent = by_subagent
            s.actions.append(a)
            if block.get("id"):
                pending[block["id"]] = a


def _user(d, msg, at, ref, s, pending, by_subagent):
    content = msg.get("content")
    blocks = content if isinstance(content, list) else []
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]

    if results:
        decision = d.get("permissionDecision") if isinstance(d.get("permissionDecision"), dict) else {}
        for r in results:
            a = pending.pop(r.get("tool_use_id"), None)
            text = _text(r.get("content"))
            if r.get("is_error") and a:
                a.ok = False
                a.error = clip(text, 400)
            source = _denial_source(text) if r.get("is_error") else None
            if decision.get("decision") == "reject":
                source = decision.get("source") or source or "permission"
            if source:
                what = (a.detail if a.kind in ("command", "edit", "read") else f"{a.name} {a.detail}") if a else "(unknown action)"
                s.denials.append(Denial(at=at, what=what, reason=clip(text, 400), source=source, ref=ref))
        return

    if by_subagent or d.get("isMeta"):
        return
    text = content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    text = text.strip()
    if not text and any(isinstance(b, dict) and b.get("type") == "image" for b in blocks):
        text = "(image)"
    if not text or text.startswith(SKIP_PROMPT_PREFIXES):
        return
    if text.startswith("<command-name>"):
        name = _tag(text, "command-name")
        args = _tag(text, "command-args")
        s.prompts.append(Prompt(at=at, text=f"{name} {args}".strip(), ref=ref, kind="slash"))
    elif text.startswith("<bash-input>"):
        s.prompts.append(Prompt(at=at, text="! " + _tag(text, "bash-input"), ref=ref, kind="shell"))
    elif not text.startswith("<"):
        s.prompts.append(Prompt(at=at, text=text, ref=ref))


def _action(name: str, inp: dict, at: str, ref: Ref) -> Action:
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if name == "Bash":
        return Action(at, "command", name, str(inp.get("command", "")), ref)
    if name in EDIT_TOOLS:
        return Action(at, "edit", name, path, ref, files=[path] if path else [])
    if name == "Read":
        return Action(at, "read", name, path, ref, files=[path] if path else [])
    if name in SEARCH_TOOLS:
        detail = " ".join(str(inp[k]) for k in ("pattern", "query", "path") if inp.get(k))
        return Action(at, "search", name, detail, ref)
    if name in WEB_TOOLS:
        return Action(at, "web", name, str(inp.get("url") or inp.get("query") or ""), ref)
    if name in AGENT_TOOLS:
        return Action(at, "agent", name, str(inp.get("description") or inp.get("subagent_type") or ""), ref)
    short = {k: v for k, v in inp.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 120}
    return Action(at, "tool", name, clip(json.dumps(short, ensure_ascii=False), 200), ref)


def _denial_source(text: str) -> str | None:
    for pattern, source in DENIAL_PATTERNS:
        if pattern.search(text[:600]):
            return source
    return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def read_all(window: Window, root: Path = DEFAULT_ROOT) -> list[Session]:
    sessions = (read_session(p, window) for p in find_logs(window, root))
    return [s for s in sessions if not s.empty()]
