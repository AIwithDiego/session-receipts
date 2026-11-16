#!/usr/bin/env python3
"""Build a demo dashboard from made-up sessions, for screenshots and for trying it out.

    python3 scripts/demo.py            # writes demo/ and prints the page to open
    python3 scripts/demo.py --pages    # also copies it, frozen, to docs/demo/ (the hosted demo)

Nothing here comes from a real log: projects, prompts and commands are invented,
and the home folder is /Users/demo.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo"
HOME = "/Users/demo"

PROJECTS = {
    "Clients/northwind-shop": ["add a discount code field to checkout", "fix the failing cart tests",
                               "the product page is slow on mobile, find out why", "add a size guide to product pages",
                               "send the order confirmation email in Spanish too", "clean up the unused components",
                               "make the search show out-of-stock items last", "add Apple Pay"],
    "Clients/harbor-dental": ["add online booking to the contact page", "update the opening hours",
                              "make the booking form accessible", "add the new hygienist to the team page",
                              "the map on the contact page is broken", "add a cookie banner"],
    "Products/notes-app": ["add tags to notes", "write tests for the sync code", "ship the dark mode toggle",
                           "notes with emoji titles crash the export", "add keyboard shortcuts", "make search fuzzy"],
    "Lab/agent-evals": ["compare the two prompts on the eval set", "plot the results", "add 20 harder cases",
                        "rerun the evals on the new model", "write up what we learned"],
}
RULES = """# My rules

- Never push to main `push-main`
- Never force push `force-push`
- Merge only when I ask `command: \\bgh\\s+pr\\s+merge\\b unless asked: \\bmerge`
- Never print secret files (.env, keys, credentials) `secret-files`
- No API keys or tokens in commands `secrets`
- Leave the lockfile alone `edit: package-lock.json`
- Ask before building when the goal is unclear
"""
WORK = [
    ("Bash", {"command": "git status"}, "On branch feature/x", False),
    ("Read", {"file_path": "{p}/src/app.ts"}, "...", False),
    ("Grep", {"pattern": "discount", "path": "{p}/src"}, "3 files", False),
    ("Edit", {"file_path": "{p}/src/checkout.ts", "old_string": "a", "new_string": "b"}, "ok", False),
    ("Edit", {"file_path": "{p}/src/components/Form.tsx", "old_string": "a", "new_string": "b"}, "ok", False),
    ("Bash", {"command": "npm test"}, "Tests: 42 passed", False),
    ("Bash", {"command": "npm run build"}, "built in 4.2s", False),
    ("Bash", {"command": "npm test -- cart"}, "Exit code 1\n2 failing", True),
    ("Write", {"file_path": "{p}/tests/cart.test.ts", "content": "..."}, "ok", False),
    ("Bash", {"command": "git add -A && git commit -m 'wip'"}, "1 file changed", False),
    ("Bash", {"command": "git push -u origin feature/x"}, "pushed", False),
]
TROUBLE = [
    ("Bash", {"command": "cat .env.local"}, "PreToolUse:Bash hook error: blocked: secret file", True),
    ("Bash", {"command": "git push origin main"}, "pushed to main", False),
    ("Edit", {"file_path": "{p}/package-lock.json", "old_string": "a", "new_string": "b"}, "ok", False),
    ("Bash", {"command": "rm -rf node_modules dist"}, "", False),
    ("Bash", {"command": "gh pr merge 12 --merge"}, "merged", False),
]


def claude_session(rng, sid, project, start):
    cwd = f"{HOME}/{project}"
    t = start
    lines = []

    def at():
        nonlocal t
        t += timedelta(seconds=rng.randint(20, 240))
        return t.isoformat().replace("+00:00", "Z")

    def user(content, **extra):
        lines.append({"type": "user", "timestamp": at(), "cwd": cwd, "gitBranch": "feature/x", "sessionId": sid,
                      "message": {"role": "user", "content": content}, "origin": {"kind": "human"}, **extra})

    n = [0]

    def tool(name, inp, out, err):
        n[0] += 1
        tid = f"{sid}-t{n[0]}"
        inp = json.loads(json.dumps(inp).replace("{p}", cwd))
        lines.append({"type": "assistant", "timestamp": at(), "cwd": cwd, "sessionId": sid, "message": {
            "id": f"{sid}-m{n[0]}", "role": "assistant", "model": rng.choice(["claude-opus-4-5", "claude-sonnet-4-5"]),
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
            "usage": {"input_tokens": rng.randint(5, 40), "cache_read_input_tokens": rng.randint(20_000, 90_000),
                      "cache_creation_input_tokens": rng.randint(500, 6_000), "output_tokens": rng.randint(80, 1_500)}}})
        user([{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err}])

    lines.append({"type": "permission-mode", "permissionMode": rng.choice(["default", "acceptEdits", "bypassPermissions"]),
                  "sessionId": sid})
    ask = rng.choice(PROJECTS[project])
    user(ask)
    for _ in range(rng.randint(6, 22)):
        tool(*rng.choices(WORK, weights=[3, 4, 2, 3, 3, 3, 2, 0.4, 1, 1, 1])[0])
    if rng.random() < 0.35:
        tool(*rng.choice(TROUBLE))
    if rng.random() < 0.5:
        user("looks good, merge it")
        tool("Bash", {"command": "gh pr merge 7 --squash"}, "merged", False)
    lines.append({"type": "ai-title", "aiTitle": ask[0].upper() + ask[1:], "sessionId": sid})
    return lines


def codex_session(rng, sid, project, start):
    cwd = f"{HOME}/{project}"
    t = start
    out = [{"timestamp": t.isoformat(), "type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
           {"timestamp": t.isoformat(), "type": "turn_context", "payload": {
               "model": "gpt-5-codex", "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}, "cwd": cwd}}]

    def ev(item):
        nonlocal t
        t += timedelta(seconds=rng.randint(20, 200))
        out.append({"timestamp": t.isoformat(), "type": "event_msg", "payload": {"type": "item_completed", "item": item}})

    ev({"type": "UserMessage", "content": [{"type": "text", "text": rng.choice(PROJECTS[project])}]})
    for cmd, status, code in [("rg -n TODO src", "completed", 0), ("npm test", rng.choice(["completed", "failed"]), 1),
                              ("npm run lint", "completed", 0)]:
        ev({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", cmd], "status": status,
            "exit_code": 0 if status == "completed" else code, "aggregated_output": "" if status == "completed" else "1 failing"})
    ev({"type": "FileChange", "changes": {f"{cwd}/src/notes.ts": {"type": "update"}}, "status": "completed"})
    out.append({"timestamp": t.isoformat(), "type": "token_usage_record", "payload": {"response_id": sid, "usage": {
        "input_tokens": rng.randint(40_000, 200_000), "cached_input_tokens": rng.randint(20_000, 150_000),
        "output_tokens": rng.randint(2_000, 15_000), "reasoning_output_tokens": rng.randint(500, 4_000)}}})
    return out


def _verdicts(rng, claude: Path, home: Path) -> None:
    """What the agent would record for the judged rule, so the demo shows judged verdicts too."""
    sys.path.insert(0, str(ROOT))
    from receipts.rules import parse

    rid = next(r.id for r in parse(RULES) if not r.check)
    out = {}
    for log in sorted(claude.rglob("*.jsonl")):
        lines = log.read_text().splitlines()
        edit = next((i for i, x in enumerate(lines, 1) if '"name": "Edit"' in x), None)
        if edit and rng.random() < 0.2:
            out[log.stem] = {rid: {"status": "broken", "ref": {"file": str(log), "line": edit},
                                   "why": "Started editing before asking what done looks like; the ask had two readings.",
                                   "at": "demo"}}
        else:
            out[log.stem] = {rid: {"status": "kept", "ref": None, "at": "demo",
                                   "why": "The ask was specific (one field, one page); building straight away was right."}}
    (home / "verdicts.json").write_text(json.dumps(out, indent=1))


def main() -> int:
    rng = random.Random(7)
    shutil.rmtree(OUT, ignore_errors=True)
    claude, codex, home = OUT / "logs/claude", OUT / "logs/codex", OUT / "home"
    home.mkdir(parents=True)
    (home / "rules.md").write_text(RULES)
    today = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    k = 0
    for day in range(6, -1, -1):
        for _ in range(rng.randint(2, 6)):
            k += 1
            project = rng.choice(list(PROJECTS))
            start = today - timedelta(days=day) + timedelta(minutes=rng.randint(0, 600))
            sid = f"demo{k:03d}"
            if project == "Products/notes-app" and rng.random() < 0.5:
                d = codex / start.strftime("%Y/%m/%d") / f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
                lines = codex_session(rng, sid, project, start)
            else:
                d = claude / ("-Users-demo-" + project.replace("/", "-")) / f"{sid}.jsonl"
                lines = claude_session(rng, sid, project, start)
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    _verdicts(rng, claude, home)
    env = {**os.environ, "HOME": HOME}  # project names show relative to the fake home
    r = subprocess.run([sys.executable, str(ROOT / "run.py"), "dashboard", "--out", str(home),
                        "--claude-dir", str(claude), "--codex-dir", str(codex)], env=env, text=True, capture_output=True)
    # Show the rules file where a real install keeps it, not this checkout's path.
    # Log paths as a real install has them, too, so no path from this machine shows in a screenshot.
    swap = {str(home / "rules.md"): "~/.session-receipts/rules.md",
            str(claude): f"{HOME}/.claude/projects", str(codex): f"{HOME}/.codex/sessions"}
    for f in [home / "data.js", *(home / "s").glob("*.js")]:
        text = f.read_text()
        for real, shown in swap.items():
            text = text.replace(json.dumps(real)[1:-1], shown)
        f.write_text(text)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode == 0 and "--pages" in sys.argv:
        pages = ROOT / "docs" / "demo"
        shutil.rmtree(pages, ignore_errors=True)
        (pages / "s").mkdir(parents=True)
        shutil.copy(home / "index.html", pages / "index.html")
        for f in (home / "s").glob("*.js"):
            shutil.copy(f, pages / "s" / f.name)
        data = (home / "data.js").read_text()
        # Frozen: the page counts "7 days" from the build, so the demo never goes empty.
        (pages / "data.js").write_text(data.replace("window.RECEIPTS={", 'window.RECEIPTS={"frozen":true,', 1))
        print(f"hosted demo -> {pages}")
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
