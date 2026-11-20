"""python3 -m receipts serve [--open] [--install|--uninstall]
python3 -m receipts dashboard [--days N] [--open]
python3 -m receipts read [today|yesterday|week|YYYY-MM-DD] [--json [--raw]]
python3 -m receipts judge [today|yesterday|week|YYYY-MM-DD|all] [--session ID] [--again]
python3 -m receipts verdict SESSION RULE kept|broken --why "..." [--ref log.jsonl:123]
python3 -m receipts verdict --batch verdicts.json [--delete]   (or - for stdin)
python3 -m receipts forget [SESSION ...] [--before YYYY-MM-DD]
python3 -m receipts status"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import read_sessions
from .redact import redact_tree
from .window import Window, parse_ts


def main(argv=None) -> int:
    # Windows uses the locale code page (often cp1252) for piped stdin and stdout, which can't hold
    # every character in a log. Use UTF-8 everywhere.
    for stream, errors in ((sys.stdout, "replace"), (sys.stdin, "strict")):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)
    ap = argparse.ArgumentParser(prog="receipts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="read one day of sessions into the common shape")
    r.add_argument("day", nargs="?", default="today")
    r.add_argument("--tool", choices=["claude-code", "codex"], action="append")
    r.add_argument("--json", action="store_true", help="full sessions as JSON, secrets redacted")
    r.add_argument("--raw", action="store_true", help="with --json: skip redaction (secrets included; keep it local)")
    r.add_argument("--claude-dir", type=Path)
    r.add_argument("--codex-dir", type=Path)
    v = sub.add_parser("serve", help="live dashboard at http://localhost:4747 (this machine only)")
    v.add_argument("--port", type=int, default=4747)
    v.add_argument("--open", action="store_true", help="open it in the browser")
    v.add_argument("--install", action="store_true", help="keep it running, starting at login (launchd, macOS only)")
    v.add_argument("--uninstall", action="store_true", help="stop it and remove the login item")
    v.add_argument("--claude-dir", type=Path)
    v.add_argument("--codex-dir", type=Path)
    b = sub.add_parser("dashboard", help="build a static copy in ~/.session-receipts (no server)")
    b.add_argument("--days", type=int, help="only the last N days (default: everything)")
    b.add_argument("--open", action="store_true", help="open it in the browser")
    b.add_argument("--out", type=Path)
    b.add_argument("--claude-dir", type=Path)
    b.add_argument("--codex-dir", type=Path)
    f = sub.add_parser("forget", help="delete archived receipts (by session id, or every one before a day)")
    f.add_argument("sessions", nargs="*")
    f.add_argument("--before", type=date.fromisoformat, help="every receipt from days before YYYY-MM-DD")
    f.add_argument("--dir", type=Path, help="default ~/.session-receipts")
    j = sub.add_parser("judge", help="evidence for the judged rules, as JSON, for the agent to decide")
    j.add_argument("day", nargs="?", default="today", help="today, yesterday, week, YYYY-MM-DD or all")
    j.add_argument("--session", help="only this session id")
    j.add_argument("--again", action="store_true", help="include rules that already have a verdict")
    j.add_argument("--dir", type=Path, help="where rules.md and verdicts.json live (default ~/.session-receipts)")
    j.add_argument("--claude-dir", type=Path)
    j.add_argument("--codex-dir", type=Path)
    d = sub.add_parser("verdict", help="record kept or broken for a judged rule")
    d.add_argument("session", nargs="?")
    d.add_argument("rule", nargs="?")
    d.add_argument("status", nargs="?", choices=["kept", "broken"])
    d.add_argument("--why", default="", help="one plain sentence a person can check against the log line")
    d.add_argument("--ref", default="", help="the log line that shows it: path/to/log.jsonl:123")
    d.add_argument("--batch", help="a JSON list of {session, rule, status, why, ref}; - reads stdin")
    d.add_argument("--delete", action="store_true",
                   help="with --batch verdict-batch.json in the receipts folder: delete it once read, also when a verdict is refused")
    d.add_argument("--dir", type=Path, help="where verdicts.json lives (default ~/.session-receipts)")
    st = sub.add_parser("status", help="is the live dashboard running, and where things are")
    st.add_argument("--port", type=int, default=4747)
    a = ap.parse_args(argv)

    if a.cmd == "status":
        return status(a)

    if a.cmd == "forget":
        return forget(a, ap)

    if a.cmd == "judge":
        from .judge import judge
        return judge(a)
    if a.cmd == "verdict":
        from .judge import verdict
        return verdict(a, ap)

    if a.cmd == "dashboard":
        return dashboard(a)
    if a.cmd == "serve":
        from . import server
        if a.uninstall:
            return server.uninstall()
        if a.install:
            return server.install(a.port)
        return server.serve(a.port, a.open, claude_root=a.claude_dir, codex_root=a.codex_dir)

    window = Window.named(a.day)
    sessions = read_sessions(window, tuple(a.tool or ("claude-code", "codex")), a.claude_dir, a.codex_dir)

    if a.json:
        rows = [s.to_dict() for s in sessions]
        if a.raw:
            print("warning: --raw output is not redacted and can hold secrets", file=sys.stderr)
        else:
            rows = redact_tree(rows)
        json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0

    many = a.day in ("week", "all")
    span = {"week": f"{window.start.date()} to {(window.end - timedelta(seconds=1)).date()}", "all": "all days"}.get(a.day, f"{window.start.date()}")
    print(f"{len(sessions)} session(s) on {span}")
    for s in sessions:
        print(
            (f"{_day(s.started)} " if many else "") + f"{_hm(s.started)}-{_hm(s.ended)} {s.tool:<11} {Path(s.cwd).name or '?':<22.22} "
            f"prompts {len(s.prompts):>3} · actions {len(s.actions):>4} · edits {len(s.files_changed):>3} · "
            f"commands {len(s.commands):>3} · blocked {len(s.denials):>2} · tokens {s.tokens.total:>11,}"
            + (f" · subagents {s.subagents}" if s.subagents else "")
            + (f"  [{'; '.join(s.notes)}]" if s.notes else "")
        )
    return 0


def dashboard(a) -> int:
    import time
    import webbrowser

    from . import dashboard as dash

    t0 = time.time()
    since = ""
    if a.days:
        start = date.today() - timedelta(days=a.days - 1)
        window = Window(Window.day(start).start, Window.day(date.today()).end)
        since = start.isoformat()
    else:
        window = Window.everything()
    sessions = read_sessions(window, ("claude-code", "codex"), a.claude_dir, a.codex_dir)
    info: dict = {}
    page = dash.build(sessions, a.out or dash.OUT, since=since, info=info)
    extra = f" + {info['archived']} from the archive" if info["archived"] else ""
    warn = f"  ({info['unreadable']} archive file(s) unreadable, skipped)" if info["unreadable"] else ""
    print(f"{info['live']} sessions{extra} -> {page}  ({time.time() - t0:.1f}s){warn}")
    if a.open:
        webbrowser.open(Path(page).resolve().as_uri())
    return 0


def status(a) -> int:
    import http.client

    from . import dashboard
    try:
        c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=3)
        c.request("GET", "/api/data", headers={"Host": f"localhost:{a.port}"})
        up = c.getresponse().status == 200
    except TimeoutError:
        up = True  # it answered the connection and is busy reading logs (the first scan takes a while)
    except OSError:
        up = False
    home = dashboard.OUT
    print(f"live dashboard: {'http://localhost:%d' % a.port if up else 'not running (RUN dashboard --open builds a static copy)'}")
    print(f"rules: {home / 'rules.md' if (home / 'rules.md').exists() else 'none yet (the example rules are used)'}")
    print(f"static copy: {home / 'index.html'}")
    return 0


def _drop_static(home: Path) -> bool:
    """Delete the static dashboard (index.html, data.js, s/): it holds copies of every receipt."""
    import shutil

    files = [home / "index.html", home / "data.js"]
    found = any(f.exists() for f in files) or (home / "s").exists()
    for f in files:
        f.unlink(missing_ok=True)
    shutil.rmtree(home / "s", ignore_errors=True)
    return found


def forget(a, ap) -> int:
    from . import archive, dashboard, server, verdicts

    if not a.sessions and not a.before:
        ap.error("forget needs session ids or --before YYYY-MM-DD")
    home = a.dir or dashboard.OUT
    root = home / "archive"
    found, _ = archive.load(root)
    logs = {sid: e["detail"].get("log") or "" for sid, (_, e) in found.items()}
    gone = archive.forget(root, set(a.sessions), a.before)
    judged = verdicts.load(home / "verdicts.json")
    if any(sid in judged for sid in gone):
        verdicts.save(home / "verdicts.json", {k: v for k, v in judged.items() if k not in gone})
    print(f"{len(gone)} receipt(s) deleted from {root}")
    if gone and _drop_static(home):
        print(f"Static copy removed from {home} (it held the deleted receipts). Run dashboard to build it again.")
    live = [logs[sid] for sid in gone if logs.get(sid) and Path(logs[sid]).exists()]
    if live:
        print(f"{len(live)} still have their raw log, so the next build would bring them back. Delete those too:")
        for path in live:
            print(f"  {path}")
    if a.dir is None and server.restart():
        print("Live dashboard restarted.")
    return 0


def _day(ts: str) -> str:
    t = parse_ts(ts)
    return t.astimezone().strftime("%a %d") if t else "?     "


def _hm(ts: str) -> str:
    t = parse_ts(ts)
    return t.astimezone().strftime("%H:%M") if t else "--:--"


if __name__ == "__main__":
    raise SystemExit(main())
