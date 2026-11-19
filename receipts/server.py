"""The live dashboard: python3 -m receipts serve -> http://localhost:4747

Reads the logs on request and keeps what it read in memory, so a refresh only
re-reads the logs that changed. Listens on this Mac only. The session data is
served as JSON to the page's own origin; requests from other sites, or for a
host name other than localhost, are refused (a web page you visit could
otherwise try to read your receipts from localhost).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import archive, claude_code, codex, dashboard, rules as rulebook, verdicts
from .redact import forget_cache
from .window import Window

PORT = 4747
MIN_GAP = 5  # seconds between two re-scans of the logs
LIVE_MARK = '<script>window.LIVE=true</script>'
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Store:
    """Every session, kept current. Thread-safe; one re-scan at a time."""

    def __init__(self, claude_root: Path | None = None, codex_root: Path | None = None, out: Path = dashboard.OUT):
        self.roots = {"claude-code": claude_root or claude_code.DEFAULT_ROOT, "codex": codex_root or codex.DEFAULT_ROOT}
        self.archive_root = out / "archive"
        self.rules_path = out / "rules.md"
        self.rules_sig = None
        self.rules: list = []
        self.rules_src = self.rules_path
        self.verdicts_path = out / "verdicts.json"
        self.verdicts: dict = {}
        self.lock = threading.Lock()
        self.logs: dict[Path, tuple[tuple, dict | None]] = {}  # log -> (file signature, entry)
        self.stored: dict | None = None  # archive, loaded once
        self.ready: dict[str, dict] = {}  # archive-only entries, re-redacted and marked
        self.data: dict | None = None
        self.details: dict[str, dict] = {}
        self.scanned = 0.0
        self.reads = 0  # logs read since start (tests use it)
        self.unreadable = 0

    def refresh(self, force: bool = False) -> dict:
        with self.lock:
            try:
                return self._refresh(force)
            finally:
                forget_cache()

    def _refresh(self, force: bool) -> dict:
        if self.data is not None and not force and time.time() - self.scanned < MIN_GAP:
            return self.data
        if self.stored is None:
            home = self.archive_root.parent  # create the parent private too, not only the leaf
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            home.chmod(0o700)
            self.archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.archive_root.chmod(0o700)
            self.stored, self.unreadable = archive.load(self.archive_root)

        rsig = (_rules_signature(self.rules_path), _rules_signature(self.verdicts_path))
        if rsig != self.rules_sig:  # rules or verdicts changed: every receipt is checked again
            self.rules, self.rules_src = rulebook.load(self.rules_path)
            self.verdicts = verdicts.load(self.verdicts_path)
            self.rules_sig, self.logs = rsig, {}
        window = Window.everything()
        found = [(p, claude_code) for p in claude_code.find_logs(window, self.roots["claude-code"])]
        found += [(p, codex) for p in codex.find_logs(window, self.roots["codex"])]
        logs = {}
        changed = self.data is None or set(self.logs) != {p for p, _ in found}
        for path, reader in found:
            sig = _signature(path)
            old = self.logs.get(path)
            if old and old[0] == sig:
                logs[path] = old
                continue
            s = reader.read_session(path, window)
            self.reads += 1
            changed = True
            keep = not s.empty() or (reader is codex and any("older" in n for n in s.notes))
            e = archive.keep(self.archive_root, self.stored, dashboard.entry(s, self.rules, self.verdicts)) if keep else None
            logs[path] = (sig, e)
        self.logs = logs
        self.scanned = time.time()
        if not changed:
            return self.data  # same data, same "updated" time: the page skips a redraw

        entries = {e["summary"]["id"]: e for _, e in logs.values() if e}
        for sid, (_, e) in self.stored.items():
            if sid in entries:
                continue
            if sid not in self.ready:
                self.ready[sid] = dashboard.from_archive(e)
            entries[sid] = self.ready[sid]
        self.data, self.details = dashboard.assemble(entries, dashboard.rules_info(self.rules, self.rules_src))
        self.data["live"] = True
        return self.data

    def detail(self, sid: str) -> dict | None:
        if self.data is None:
            self.refresh()
        return self.details.get(sid)


def _rules_signature(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return ("example",)


def _signature(path: Path) -> tuple:
    st = path.stat()
    sig = [st.st_mtime_ns, st.st_size]
    sub = path.with_suffix("") / "subagents"  # Claude Code subagents live beside the session
    if sub.is_dir():
        for p in sorted(sub.glob("*.jsonl")):
            s = p.stat()
            sig += [p.name, s.st_mtime_ns, s.st_size]
    return tuple(sig)


def page() -> bytes:
    html = dashboard.TEMPLATE.read_text(encoding="utf-8")
    return html.replace('<script src="data.js"></script>', LIVE_MARK).encode()


def handler(store: Store, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "receipts"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet; errors still reach stderr via send_error
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8")

        def _allowed(self, path: str) -> bool:
            if self.headers.get("Host") not in hosts:
                return False  # DNS rebinding: another name pointing at 127.0.0.1
            origin = self.headers.get("Origin")
            if origin and origin not in origins:
                return False
            site = self.headers.get("Sec-Fetch-Site")
            if path == "/" and self.headers.get("Sec-Fetch-Mode") == "navigate":
                return True  # opening the page from a link is fine; it holds no data
            return site in (None, "same-origin", "none")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(path):
                return self._send(403, b"Forbidden", "text/plain")
            if path == "/":
                return self._send(200, page(), "text/html; charset=utf-8")
            if path == "/api/data":
                return self._json(store.refresh())
            m = re.fullmatch(r"/api/s/([\w-]{1,120})", path)
            if m:
                d = store.detail(m.group(1))
                return self._json(d) if d else self._send(404, b"No such session", "text/plain")
            return self._send(404, b"Not found", "text/plain")

        do_HEAD = do_GET

        def _refuse(self):
            self._send(405, b"Read only", "text/plain")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    return Handler


def make_server(store: Store, port: int = PORT) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    srv.RequestHandlerClass = handler(store, srv.server_address[1])  # the real port when port=0
    srv.daemon_threads = True
    return srv


def serve(port: int = PORT, open_browser: bool = False, **roots) -> int:
    store = Store(**roots)
    srv = make_server(store, port)
    url = f"http://localhost:{port}"
    print(f"Session receipts at {url} (this Mac only). Ctrl-C to stop.", flush=True)
    threading.Thread(target=store.refresh, daemon=True).start()  # warm up while the browser opens
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ---- always on, via launchd ----

LABEL = "com.aiwithdiego.session-receipts"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _python() -> str:
    """The python3 on PATH (e.g. Homebrew's link), so a Python upgrade doesn't break the service."""
    import shutil
    found = shutil.which("python3")
    return found if found and Path(found).resolve() == Path(sys.executable).resolve() else sys.executable


def _macos_only() -> bool:
    if sys.platform == "darwin":
        return False
    print("serve --install and --uninstall use launchd, so they work on macOS only. "
          "Run `serve` to keep the dashboard up while the terminal is open.", file=sys.stderr)
    return True


def install(port: int = PORT) -> int:
    import os
    import plistlib
    import subprocess

    import shutil

    if _macos_only():  # before anything is written
        return 1
    log = dashboard.OUT / "serve.log"
    dashboard.OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Run from a copy, so the service doesn't change when you switch branches in a checkout.
    app = dashboard.OUT / "app"
    shutil.rmtree(app, ignore_errors=True)
    shutil.copytree(Path(__file__).resolve().parent, app / "receipts", ignore=shutil.ignore_patterns("__pycache__"))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST, "wb") as fh:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [_python(), "-m", "receipts", "serve", "--port", str(port)],
            "WorkingDirectory": str(app),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }, fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    for _ in range(50):  # bootout finishes in the background; wait until the old one is gone
        if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode:
            break
        time.sleep(0.1)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr.strip() or "launchctl failed", file=sys.stderr)
        return 1
    print(f"Always on at http://localhost:{port} (starts at login), running a copy in {app}.\n"
          "Run --install again after updating. Undo: python3 -m receipts serve --uninstall")
    return 0


def restart() -> bool:
    """Restart the always-on dashboard if it is installed, so it rereads the archive."""
    import os
    import subprocess

    if not PLIST.exists():
        return False
    return not subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode


def uninstall() -> int:
    import os
    import subprocess

    if _macos_only():
        return 1
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    import shutil
    shutil.rmtree(dashboard.OUT / "app", ignore_errors=True)
    print("Stopped and removed.")
    return 0
