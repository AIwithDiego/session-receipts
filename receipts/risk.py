"""Risky moments: things worth a second look, each with the log line behind it."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Ref, Session

CRITICAL, SERIOUS, WARNING = "critical", "serious", "warning"


@dataclass
class Risk:
    at: str
    level: str
    label: str
    detail: str
    ref: Ref | None


GIT_PUSH = r"\bgit\s+(-[Cc]\s+\S+\s+|--\S+\s+)*push\b[^|;&]*"
TO_MAIN = r"(\s[\x27\x22+]?(main|master)(?![\w./-])|:[\x27\x22]?(refs/heads/)?(main|master)(?![\w./-]))"
FORCE = r"(\s--force\b|\s-f\b|--force-with-lease|\s\+\S)"
PUSH_PROTECTED = re.compile(GIT_PUSH + "(" + TO_MAIN + "|" + FORCE + ")")
PUSH = re.compile(GIT_PUSH)
DESTRUCTIVE = re.compile(
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r|\bgit\s+reset\s+--hard|\bgit\s+clean\s+-[a-zA-Z]*f"
    r"|\bdrop\s+(table|database|schema)\b|\btruncate\s+table\b|\bgit\s+branch\s+-D\b|\bsupabase\s+db\s+reset\b",
    re.I,
)
PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b")
SECRET_PATH = re.compile(
    r"(^|[/\s'\"=])\.env(\.[\w.-]+)?($|[\s'\";|&)])|id_(rsa|ed25519)\b|\.pem\b|\.p12\b|credentials(\.json)?\b|\.npmrc\b|\.netrc\b"
    r"|security\s+find-(generic|internet)-password|\.claude/\.env",
    re.I,
)
PERMISSIONS_OFF = ("bypassPermissions", "danger-full-access", "approval never")


def checks_off(s: Session) -> bool:
    """Ran without permission prompts. A session fact, not a moment: shown as a count, not a warning."""
    return any(p in s.permission_mode for p in PERMISSIONS_OFF)


# A heredoc fed to a shell is code that runs, so its body stays in.
RUNS_HEREDOC = re.compile(r"(^|[|;&(]|&&|\|\|)\s*(sudo\s+)?(env\s+\S+=\S*\s+)*((ba|z|da|k)?sh|ssh|eval)\b[^|;&\n]*$")


def _heredoc_end(cmd: str, pos: int, marker: str, strip_tabs: bool) -> int:
    while pos < len(cmd):
        newline = cmd.find("\n", pos)
        end = len(cmd) if newline < 0 else newline
        line = cmd[pos:end].removesuffix("\r")
        if strip_tabs:
            line = line.lstrip("\t")
        pos = min(end + 1, len(cmd))
        if line == marker:
            break
    return pos


def command_text(cmd: str) -> str:
    """The command without heredoc bodies fed in as data (a commit message, a file).
    A heredoc fed to bash, sh, zsh or ssh runs, so it stays."""
    if "<<" not in cmd or "\n" not in cmd:
        return cmd
    parts = []
    cursor = 0
    pos = 0
    while token := HEREDOC_TOKEN.search(cmd, pos):
        pos = token.end()
        if token.lastgroup is None:
            continue
        line_end = cmd.find("\n", pos)
        if line_end < 0:
            break
        line_start = cmd.rfind("\n", 0, token.start()) + 1
        pending = []
        # Bodies follow the entire header, in the order of its redirects.
        for redirect in HEREDOC_TOKEN.finditer(cmd, token.start(), line_end):
            if redirect.lastgroup is None:
                continue
            runs = bool(RUNS_HEREDOC.search(cmd[line_start:redirect.start()]))
            pending.append((redirect[redirect.lastgroup], bool(redirect["tabs"]), runs))
            if not runs:
                parts.extend((cmd[cursor:redirect.start()], "<<heredoc"))
                cursor = redirect.end()
        parts.append(cmd[cursor:line_end + 1])
        cursor = line_end + 1
        kept_body = False
        for marker, strip_tabs, runs in pending:
            end = _heredoc_end(cmd, cursor, marker, strip_tabs)
            if runs and end > cursor:
                parts.append(cmd[cursor:end])
                kept_body = True
            cursor = end
        if cursor == len(cmd) and not kept_body and not cmd.endswith("\n"):
            parts[-1] = parts[-1].removesuffix("\n").removesuffix("\r")
        pos = cursor
    parts.append(cmd[cursor:])
    return "".join(parts)


# Quoted text with a space inside is data (a commit message, a search pattern, inline code),
# unless a shell runs it. 'main' alone stays: it can be a real argument.
# \x is one escaped character, not a quote: 'don'\''t' must not throw off the pairing.
QUOTED = re.compile(r"\\.|'[^']*'|\"(?:[^\"\\]|\\.)*\"")
HEREDOC_TOKEN = re.compile(
    QUOTED.pattern + r"|(?<![^\s;|&])#[^\n]*|(?<!<)<<(?P<tabs>-)?(?!<)[ \t]*"
    r"(?:'(?P<single>[^'\r\n]+)'|\"(?P<double>[^\"\r\n]+)\"|\\?(?P<word>[\w.-]+))"
    r"(?![^\s;|&<>()])"
)
RUNS_QUOTED = re.compile(r"(\b((ba|z|da|k|fi)?sh|su)\s+(-\w+\s+)*-\w*c|\beval|\bwatch\b(\s+-\S+(\s+\S+)?)*|\bssh\b(\s+-\S+(\s+\S+)?)*\s+\S+(\s+--)?)\s*$")
PIPED_TO_SHELL = re.compile(r"\|\s*(sudo\s+)?(\S*/)?((ba|z|da|k)?sh|eval)\b")
SECRET_TAIL = re.compile(r"/(\.env(\.[\w.-]+)?|id_(rsa|ed25519)|[\w.-]+\.(pem|p12)|credentials(\.json)?|\.npmrc|\.netrc)[\"']$")


def plain_command(cmd: str) -> str:
    """What the shell runs: heredoc data and quoted text set aside (see command_text, QUOTED).
    Patterns, not a parser: code inside python -c or node -e is not read."""
    text = command_text(cmd)
    if PIPED_TO_SHELL.search(text):
        return text  # quoted text fed to a shell runs
    def keep_or_drop(m: re.Match) -> str:
        q = m.group(0)
        if q.startswith("\\") or not re.search(r"\s", q):
            return q
        if q.startswith('"') and ("$(" in q or "`" in q):
            return q  # command substitution runs inside double quotes
        if SECRET_TAIL.search(q) or RUNS_QUOTED.search(text[max(0, m.start() - 120):m.start()]):
            return q
        return '"…"'
    return QUOTED.sub(keep_or_drop, text)


def risks(s: Session) -> list[Risk]:
    out: list[Risk] = []

    for d in s.denials:
        out.append(Risk(d.at, SERIOUS, f"Blocked ({d.source})", d.what, d.ref))

    blocked = {(d.at, d.what) for d in s.denials}
    for a in s.actions:
        if (a.at, a.detail) in blocked:
            continue
        text = command_text(a.detail) if a.kind == "command" else a.detail
        if a.kind == "command":
            if PUSH_PROTECTED.search(plain_command(a.detail)):
                out.append(Risk(a.at, CRITICAL, "Push to main or force push", a.detail, a.ref))
            elif PUSH.search(plain_command(a.detail)):
                out.append(Risk(a.at, WARNING, "git push", a.detail, a.ref))
            if DESTRUCTIVE.search(text):
                out.append(Risk(a.at, CRITICAL, "Destructive command", a.detail, a.ref))
            if PIPE_TO_SHELL.search(text):
                out.append(Risk(a.at, SERIOUS, "Downloaded script piped to shell", a.detail, a.ref))
        if a.kind in ("command", "read", "edit") and SECRET_PATH.search(text):
            out.append(Risk(a.at, SERIOUS, "Secret file touched", a.detail, a.ref))

    order = {CRITICAL: 0, SERIOUS: 1, WARNING: 2}
    return sorted(out, key=lambda r: (r.at, order[r.level]))
