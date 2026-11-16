"""SR-6 leak test: plant secrets in every place a log can carry them, then check
that none reaches anything the tool writes or serves.

Every secret is built at runtime so no scanner mistakes this file for a leak.
If this test fails, a secret would have reached disk or the browser.
"""

import contextlib
import gzip
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

from receipts import __main__ as cli
from receipts import dashboard, read_sessions, server
from receipts.window import Window


def opaque(tag: str) -> str:
    """A secret with no recognisable shape: only its context can give it away."""
    return "Zq" + tag + "w7" * 12


SHAPED = {
    "anthropic": "sk-ant-" + "api03-" + "A" * 40,
    "openai": "sk-" + "proj-" + "B" * 40,
    "github": "ghp" + "_" + "C" * 36,
    "github_app": "ghs" + "_" + "G" * 36,
    "github_fine": "github" + "_pat_" + "H" * 40,
    "aws": "AKIA" + "D" * 16,
    "slack": "xox" + "b-" + "1234567890-abcdefghij",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTYifQ" + "." + "S" * 30,
    "stripe": "sk_live" + "_" + "E" * 24,
    "google": "AIza" + "I" * 35,
    "supabase": "sb_secret" + "_" + "J" * 30,
    "npm": "npm" + "_" + "K" * 36,
    "hf": "hf" + "_" + "L" * 34,
    "gitlab": "glpat" + "-" + "M" * 20,
    "sendgrid": "SG." + "N" * 22 + "." + "O" * 43,
}
PEM_BODY = "MIIEow" + "F" * 60
PEM = "-----BEGIN RSA PRIVATE KEY-----\n" + PEM_BODY + "\n-----END RSA PRIVATE KEY-----"

# Secrets that only their surroundings reveal: NAME=value, headers, flags, URLs.
O = {k: opaque(k) for k in (
    "export", "envfile", "envkey", "bearer", "xapikey", "cookie", "query", "urlpass",
    "flagpw", "flagtoken", "json", "yaml", "dsn", "codexenv", "codexout", "denial", "title",
)}

CONTEXTUAL = [
    f"export OPENAI_API_KEY={O['export']}",
    f"curl -H 'Authorization: Bearer {O['bearer']}' https://api.example.com",
    f"curl -H \"x-api-key: {O['xapikey']}\" https://api.example.com",
    f"curl -H 'Cookie: session={O['cookie']}' https://app.example.com",
    f"curl 'https://api.example.com/v1/items?api_key={O['query']}&q=1'",
    f"git clone https://deploy:{O['urlpass']}@git.example.com/repo.git",
    f"mysql --password={O['flagpw']} -h db",
    f"vercel deploy --token {O['flagtoken']}",
    f"psql postgres://app:{O['dsn']}@db.example.com:5432/app",
]
ENV_DUMP = (
    "NEXT_PUBLIC_SITE=https://example.com\n"
    f"DATABASE_PASSWORD={O['envfile']}\n"
    f"RESEND_KEY={O['envkey']}\n"
)
EDIT_BODY = f'{{"client_secret": "{O["json"]}"}}\napi_key: {O["yaml"]}\n'

EVERY = [*SHAPED.values(), PEM_BODY, *O.values()]


def _claude_log() -> list[dict]:
    base = {"cwd": "/Users/demo/leaky", "gitBranch": "feat", "sessionId": "leak1"}
    t = iter(f"2026-11-05T09:{m:02d}:00.000Z" for m in range(60))
    lines = [{"type": "ai-title", "aiTitle": f"rotate key {SHAPED['github']}", "sessionId": "leak1"}]

    def user(content, **extra):
        lines.append({**base, "type": "user", "timestamp": next(t), "message": {"role": "user", "content": content},
                      "origin": {"kind": "human"}, **extra})

    def tool(name, inp, result, error=False, n=[0]):
        n[0] += 1
        tid = f"t{n[0]}"
        lines.append({**base, "type": "assistant", "timestamp": next(t), "message": {
            "id": f"m{n[0]}", "role": "assistant", "model": "claude-test-1",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}})
        user([{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": error}])

    user("here are all my keys: " + " ".join(SHAPED.values()) + "\n" + PEM)
    for c in CONTEXTUAL:
        tool("Bash", {"command": c}, f"error: {c}", error=True)
    tool("Bash", {"command": "cat .env.local"}, ENV_DUMP)
    tool("Edit", {"file_path": "/Users/demo/leaky/config.yml", "old_string": "x", "new_string": EDIT_BODY}, "ok")
    tool("Write", {"file_path": "/Users/demo/leaky/.env", "content": ENV_DUMP}, "ok")
    tool("Bash", {"command": f"GH_TOKEN={O['denial']} git push origin main"},
         f"PreToolUse:Bash hook error: blocked RESEND_KEY={O['denial']}", error=True)
    return lines


def _codex_log() -> list[dict]:
    t = iter(f"2026-11-05T10:{m:02d}:00.000Z" for m in range(60))

    def ev(item):
        return {"timestamp": next(t), "type": "event_msg", "payload": {"type": "item_completed", "item": item}}

    return [
        {"timestamp": next(t), "type": "session_meta", "payload": {"id": "leak2", "cwd": "/Users/demo/leaky"}},
        ev({"type": "UserMessage", "content": [{"type": "text", "text": f"use {SHAPED['openai']}, password: {O['title']}"}]}),
        ev({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", f"STRIPE_SECRET_KEY={O['codexenv']} npm test"],
            "status": "failed", "exit_code": 1, "aggregated_output": f"auth_token={O['codexout']} rejected"}),
        ev({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", CONTEXTUAL[1]], "status": "completed",
            "exit_code": 0, "aggregated_output": ""}),
    ]


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


class Leaks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.claude, cls.codex, cls.out = root / "claude", root / "codex", root / "out"
        _write(cls.claude / "-Users-demo-leaky" / "leak1.jsonl", _claude_log())
        _write(cls.codex / "2026/11/05" / "rollout-2026-11-05T10-00-00-leak2.jsonl", _codex_log())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def assertClean(self, where: str, text: str):
        found = [s[:12] + "..." for s in EVERY if s in text]
        self.assertEqual(found, [], f"secrets reached {where}")

    def test_the_logs_do_hold_the_secrets(self):
        # Guard against a test that passes because nothing was planted.
        raw = "".join(p.read_text() for p in [*self.claude.rglob("*.jsonl"), *self.codex.rglob("*.jsonl")])
        raw = raw.replace("\\n", "\n")
        self.assertEqual([s[:12] for s in EVERY if s not in raw], [])
        sessions = read_sessions(Window.everything(), ("claude-code", "codex"), self.claude, self.codex)
        self.assertEqual(len(sessions), 2)

    def test_secret_split_by_a_cut_is_still_hidden(self):
        from receipts.redact import clip, tail
        token = SHAPED["github"]
        for n in range(5, len(token)):
            self.assertNotIn("C" * 12, clip("x" * 9 + " " + token, 10 + n))
            self.assertNotIn("C" * 12, tail(token + "y" * 280, 300 - n))
        long_out = "z" * 49 + " " + token + "y" * 270  # Codex keeps the last 300 characters of failed output
        self.assertNotIn("C" * 12, tail(long_out, 300))

    def test_static_dashboard_and_archive(self):
        out = self.out / "static"
        sessions = read_sessions(Window.everything(), ("claude-code", "codex"), self.claude, self.codex)
        dashboard.build(sessions, out)
        files = [p for p in out.rglob("*") if p.is_file()]
        self.assertTrue(any(p.suffix == ".gz" for p in files), "nothing was archived")
        for p in files:
            text = gzip.decompress(p.read_bytes()).decode() if p.suffix == ".gz" else p.read_text()
            self.assertClean(str(p.relative_to(out)), text)

    def test_files_are_private(self):
        out = self.out / "perms"
        dashboard.build(read_sessions(Window.everything(), ("claude-code", "codex"), self.claude, self.codex), out)
        for p in [out, *out.rglob("*")]:
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode & 0o077, 0, f"{p.relative_to(out.parent)} is readable by others ({oct(mode)})")

    def test_live_server(self):
        store = server.Store(self.claude, self.codex, self.out / "live")
        data = store.refresh(force=True)
        self.assertClean("/api/data", json.dumps(data))
        for row in data["sessions"]:
            self.assertClean(f"/api/s/{row['id']}", json.dumps(store.detail(row["id"])))

    def test_read_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["read", "2026-11-05", "--json", "--claude-dir", str(self.claude), "--codex-dir", str(self.codex)])
        self.assertIn("leak1", buf.getvalue())
        self.assertClean("read --json", buf.getvalue())


    def test_every_shape_trips_the_secrets_rule(self):
        from receipts.rules import SECRET_SHAPES
        missed = [k for k, v in SHAPED.items() if not any(p.search(v) for p in SECRET_SHAPES)]
        self.assertEqual(missed, [])


    def test_redactor_on_every_shape_from_the_privacy_review(self):
        from receipts.redact import redact
        secret = "Zq" + "mark" + "w7" * 8
        cases = [
            f'curl -H "Authorization: Basic {secret}"', f"Authorization: Token {secret}",
            f"https://hooks.slack.com/services/T0/B0/{secret}", f"https://discord.com/api/webhooks/123456/{secret}",
            "sk" + "_test_" + secret, "123456789" + ":AA" + secret * 2,
            f"mysql -u root -p{secret} db", f"MY_PW={secret}", f'{{"password":"p@ss {secret}!"}}',
            f"secret: 'a b {secret}'", f"Endpoint=x;AccountName=y;AccountKey={secret};Suffix=z",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\n" + secret + "\n-----END PGP PRIVATE KEY BLOCK-----",
            "AGE-SECRET-KEY-1" + secret.upper() * 4, "pplx-" + secret * 2, "gsk" + "_" + secret * 2,
            "lin_api" + "_" + secret * 2, f"https://b.s3.amazonaws.com/f?X-Amz-Signature={secret}",
            f"SENTRY_DSN=https://{secret}x@o1.ingest.sentry.io/1", f"curl 'https://x.io/?access_token={secret}'",
        ]
        self.assertEqual([c for c in cases if "mark" in redact(c)], [])

    def test_everyday_text_is_left_alone(self):
        from receipts.redact import redact
        for s in ["git push origin main", "ls -p src", "grep -rn token src/", "mysql -u root -p db",
                  "see https://example.com/?page=2&keyword=x", "NODE_ENV=production npm start", "author: Diego"]:
            self.assertEqual(redact(s), s)

    def test_a_huge_paste_stays_fast(self):
        import time
        from receipts.redact import redact
        blobs = ["aB3.-_" * 9000, "secret" * 9000, "a." * 27000, "A_" * 27000 + "=x"]
        t = time.perf_counter()
        for b in blobs:
            redact(b)
        self.assertLess(time.perf_counter() - t, 1.0)


    def test_old_archive_is_scrubbed_on_disk(self):
        from receipts import archive
        root = self.out / "old-archive"
        leaked = {"summary": {"id": "old1", "day": "2026-09-01", "prompts": 1, "actions": 0},
                  "detail": {"log": "/gone.jsonl", "prompts": [{"text": "RESEND_KEY=" + O["envkey"]}]}}
        path = archive.save(root, leaked)  # as written by a version that missed this shape
        self.assertIn(O["envkey"], gzip.decompress(path.read_bytes()).decode())
        archive.load(root)
        self.assertClean("archive file after load", gzip.decompress(path.read_bytes()).decode())

    def test_forget(self):
        from receipts import archive
        home = self.out / "forget"
        for sid, day in (("a1", "2026-09-01"), ("a2", "2026-10-01"), ("a3", "2026-11-01")):
            archive.save(home / "archive", {"summary": {"id": sid, "day": day}, "detail": {"log": ""}})
        (home / "verdicts.json").write_text('{"a1": {"r": {"status": "kept"}}, "a3": {}}')
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["forget", "a3", "--dir", str(home)])
            cli.main(["forget", "--before", "2026-10-01", "--dir", str(home)])
        left, _ = archive.load(home / "archive")
        self.assertEqual(sorted(left), ["a2"])
        self.assertEqual(json.loads((home / "verdicts.json").read_text()), {})


    def test_quick_skip_never_hides_a_secret(self):
        from receipts import redact as rd
        samples = [*EVERY, *CONTEXTUAL, ENV_DUMP, EDIT_BODY, "xoxb-12345-abcd", "eyJabcdefgh.ijklmnop.qrstuvwx",
                   "discord.com/api/webhooks/1/abc", "hooks.slack.com/services/T/B/x", "12345678:AA" + "b" * 30]
        for text in samples:
            hit = any(p.search(text) for p, _ in rd.PATTERNS)
            if hit:
                self.assertTrue(rd._MAYBE.search(text), text[:30])


if __name__ == "__main__":
    unittest.main()
