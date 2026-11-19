"""Your rules, checked against what the agent did, with the log line as proof.

Rules live in a plain Markdown file (~/.session-receipts/rules.md). Each rule is
a "- " line in plain language. A check in backticks makes it proven from the
logs; without one the rule is "judged" (by the agent running the skill, later).

Checks:
  push-main                 git push to main/master
  force-push                git push --force / -f / --force-with-lease
  secret-files              a secret file (.env, keys, credentials) read or printed
  secrets                   secret-shaped text (API keys, tokens, private keys) in a command
  command: <regex>          any command matching the regex
  file: <glob>              any file read or edited matching the glob
  edit: <glob>              any file edited matching the glob
Add "unless asked: <regex>" to allow it when an earlier prompt in the session
matched, e.g. `command: gh pr merge unless asked: merge`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .model import Prompt, Ref, Session
from .redact import SHAPES, clip
from .risk import FORCE, GIT_PUSH, TO_MAIN, command_text, plain_command
from . import verdicts as verdicts_mod

EXAMPLE = Path(__file__).parent / "assets" / "rules.example.md"

PUSH_MAIN = re.compile(GIT_PUSH + TO_MAIN)
FORCE_PUSH = re.compile(GIT_PUSH + FORCE)
# A print command aimed at the file itself. `source .env` loads secrets without showing them; that's allowed.
PRINTS = re.compile(r"^\s*(sudo\s+)?(cat|less|more|head|tail|bat|grep|rg|sed|awk|strings|xxd|cut|sort|uniq)\b")
SECRET_FILE = re.compile(
    r"(^|[/\s'\"=])\.env(?!\.(example|sample|template)\b)(\.[\w.-]+)?($|[\s'\";|&)])"
    r"|id_(rsa|ed25519)\b|\.pem\b|\.p12\b|credentials\.json\b|\.npmrc\b|\.netrc\b",
    re.I,
)
# Shows names, counts or file names, or only public values: no secret on screen.
HARMLESS = re.compile(
    r"\bgrep\b[^|]*\s-[a-zA-Z]*[lLcq]|\bcut\s+-d\s*['\"]?=['\"]?\s+-f\s*1\b|\^\[A-Z_\]"
    r"|\b(NEXT_PUBLIC|VITE|EXPO_PUBLIC|PUBLIC)_[A-Z]|\]\*=?['\"]|\bsed\b[^|]*s/=\.|\|\s*(wc|cut\s+-d\s*['\"]?=['\"]?\s+-f\s*1)\b"
)
# `sed -i` edits the file in place and prints nothing.
EDITS_IN_PLACE = re.compile(r"^\s*(sudo\s+)?sed\s+(-\S+\s+)*-i")
# $(...) captures output into a variable: loaded, not shown.
CAPTURED = re.compile(r"\$\((?:[^()]|\([^()]*\))*\)")
STATEMENTS = re.compile(r"\|\||&&|;|\n")
WRITES = re.compile(r"^\s*cat\s*>")
# Only the secret shapes that can't be mistaken for something else (not NAME=value guesses).
SECRET_SHAPES = [p for p, _ in SHAPES]
# Published on purpose: Supabase's local demo keys, AWS's documentation example keys.
NOT_SECRET = re.compile(r"eyJpc3MiOiJzdXBhYmFzZS1kZW1vI|EXAMPLE")

NAMED = {"push-main", "force-push", "secret-files", "secrets"}


@dataclass
class Rule:
    id: str
    text: str
    check: str = ""          # "" = judged
    kind: str = ""           # push-main | secret-files | secrets | command | file | edit
    pattern: str = ""
    asked: re.Pattern | None = None
    error: str = ""
    _rx: re.Pattern | None = field(default=None, repr=False)

    @property
    def proven(self) -> bool:
        return bool(self.kind) and not self.error


@dataclass
class Hit:
    at: str
    what: str
    ref: object
    went_through: bool
    allowed_by: Prompt | None = None


@dataclass
class Result:
    rule: Rule
    status: str  # kept | broken | tried | judge | error
    hits: list[Hit]
    checked: int
    why: str = ""        # a judged rule: the agent's reason
    judged_at: str = ""

    def to_dict(self) -> dict:
        ref = lambda r: {"file": r.file, "line": r.line} if r else None
        return {
            "id": self.rule.id,
            "status": self.status,
            "checked": self.checked,
            "hits": [{
                "at": h.at, "what": clip(h.what, 600), "ok": h.went_through, "ref": ref(h.ref),
                "asked": {"text": clip(h.allowed_by.text, 200), "ref": ref(h.allowed_by.ref)} if h.allowed_by else None,
            } for h in self.hits[:20]],
            "more": max(0, len(self.hits) - 20),
            **({"why": self.why, "judged": self.judged_at} if self.judged_at else {"why": self.why} if self.why else {}),
        }


def parse(text: str) -> list[Rule]:
    rules = []
    for line in text.splitlines():
        m = re.match(r"\s*[-*]\s+(.+)", line)
        if not m:
            continue
        body = m.group(1).strip()
        checks = re.findall(r"`([^`]+)`", body)
        plain = re.sub(r"\s*`[^`]+`\s*", " ", body).strip()
        rid = hashlib.sha1(plain.lower().encode()).hexdigest()[:8]
        r = Rule(id=rid, text=plain, check=checks[-1].strip() if checks else "")
        if r.check and r.check.lower() != "judged":
            _compile(r)
        rules.append(r)
    return rules


def _compile(r: Rule) -> None:
    spec = r.check
    m = re.search(r"\s+unless asked:\s*(.+)$", spec)
    try:
        if m:
            r.asked = re.compile(m.group(1).strip(), re.I)
            spec = spec[: m.start()].strip()
        if spec in NAMED:
            r.kind = spec
            return
        k, sep, arg = spec.partition(":")
        k, arg = k.strip(), arg.strip()
        if not sep or k not in ("command", "file", "edit") or not arg:
            r.error = f"Don't know the check `{r.check}`"
            return
        r.kind, r.pattern = k, arg
        if k == "command":
            r._rx = re.compile(arg, re.I)
    except re.error as e:
        r.error = f"Bad pattern in `{r.check}`: {e}"


def read_text(path: Path) -> str:
    """UTF-8, with or without a BOM, or UTF-16 with a BOM (what `>` writes in Windows PowerShell 5.1)."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load(path: Path) -> tuple[list[Rule], Path]:
    src = path if path.is_file() else EXAMPLE
    return parse(read_text(src)), src


def check(s: Session, rules: list[Rule], verdicts: dict | None = None) -> list[Result]:
    """verdicts: this session's recorded verdicts on judged rules, by rule id."""
    out = [_check(s, r) for r in rules]
    for res in out:
        v = (verdicts or {}).get(res.rule.id)
        if res.status == "judge" and v:
            if v.get("status") == "kept" and verdicts_mod.stale(v, s):
                res.why = "Kept when judged, but the session went on after that. Judge it again."
                continue
            ref = Ref(v["ref"]["file"], v["ref"]["line"]) if v.get("ref") else None
            res.status, res.why, res.judged_at = v["status"], v.get("why", ""), v.get("at", "?")
            if v["status"] == "broken":
                res.hits = [Hit("", v.get("why", ""), ref, True)]
    return out


def _check(s: Session, r: Rule) -> Result:
    if r.error:
        return Result(r, "error", [], 0)
    if not r.kind:
        return Result(r, "judge", [], 0)
    hits: list[Hit] = []
    checked = 0
    for a in s.actions:
        if a.kind == "command":
            if r.kind in ("file", "edit"):
                continue
            checked += 1
            # A secret is a secret even inside quotes; every other check looks at what the shell runs.
            text = command_text(a.detail) if r.kind == "secrets" else plain_command(a.detail)
            if _command_hit(r, text):
                hits.append(Hit(a.at, a.detail, a.ref, a.ok))
        elif a.kind in ("read", "edit"):
            if r.kind == "secret-files":
                checked += 1
                if SECRET_FILE.search(" " + a.detail) and a.kind == "read":
                    hits.append(Hit(a.at, a.detail, a.ref, a.ok))
            elif r.kind in ("file", "edit") and (r.kind == "file" or a.kind == "edit"):
                checked += 1
                if any(fnmatch(f, r.pattern) or fnmatch(Path(f).name, r.pattern) for f in a.files or [a.detail]):
                    hits.append(Hit(a.at, a.detail, a.ref, a.ok))

    if r.asked:
        for h in hits:
            # The latest prompt that mentions it decides: "merge it" allows, "don't merge yet" takes it back.
            last = next((p for p in reversed(s.prompts) if p.at <= h.at and r.asked.search(p.text)), None)
            h.allowed_by = last if last and _asks(r.asked, last.text) else None

    live = [h for h in hits if not h.allowed_by]
    if any(h.went_through for h in live):
        status = "broken"
    elif live:
        status = "tried"  # attempted, but blocked or failed
    else:
        status = "kept"
    return Result(r, status, hits, checked)


NEGATION = re.compile(r"(?i)\b(don'?t|do\s+not|never|not|no|without|stop|wait\s+(to|before))\b(\W+\w+){0,3}\W*$")


def _asks(rx: re.Pattern, text: str) -> bool:
    """True when the prompt asks for it: at least one match not negated just before ("don't merge", "do not merge yet")."""
    return any(not NEGATION.search(text[max(0, m.start() - 40):m.start()]) for m in rx.finditer(text))


def _command_hit(r: Rule, text: str) -> bool:
    if r.kind == "push-main":
        return bool(PUSH_MAIN.search(text))
    if r.kind == "force-push":
        return bool(FORCE_PUSH.search(text))
    if r.kind == "secret-files":
        return any(_prints_secret(st) for st in STATEMENTS.split(CAPTURED.sub("$X", text)))
    if r.kind == "secrets":
        return any(not NOT_SECRET.search(m.group(0)) for p in SECRET_SHAPES for m in p.finditer(text))
    if r.kind == "command":
        return bool(r._rx.search(text))
    return False


def _prints_secret(statement: str) -> bool:
    """The first stage prints a secret file and nothing later in the pipeline hides the values."""
    first = statement.split("|", 1)[0]
    return bool(PRINTS.search(first) and not WRITES.search(first) and not EDITS_IN_PLACE.search(first)
                and SECRET_FILE.search(first)
                and not HARMLESS.search(statement))
