"""SR-11: fixes from the pre-release review (Codex, 8 Nov 2026)."""

import contextlib
import io
import os
import re
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from receipts import __main__ as cli
from receipts import rules, verdicts
from receipts.model import Action, Prompt, Ref, Session
from receipts.redact import redact
from receipts.risk import command_text
from receipts.window import Window

OPAQUE = "Zq8vKp2mWx7rTnB4"  # a password with no shape: only its context gives it away


class ContextSecrets(unittest.TestCase):
    """No fragment of the secret survives, not only the whole string."""

    def assertGone(self, text, *parts):
        out = redact(text)
        for p in parts:
            self.assertNotIn(p, out, f"{p!r} leaked from {text!r}: {out!r}")

    def test_curl_user_password(self):
        u = "-" + "u"  # built at runtime so secret scanners don't flag the file
        self.assertGone(f"curl {u} alice:{OPAQUE} https://api.example.test", OPAQUE)
        self.assertGone(f"curl -s --us" + f"er admin:{OPAQUE} localhost:8080", OPAQUE)
        self.assertEqual(redact("curl -u https://example.test"), "curl -u https://example.test")

    def test_cookie_in_json(self):
        self.assertGone(f'{{"Cookie":"session={OPAQUE}"}}', OPAQUE)
        self.assertGone(f"{{'set-cookie': 'sid={OPAQUE}; Path=/'}}", OPAQUE)

    def test_quoted_flag_value_with_spaces(self):
        self.assertGone("mytool --password 'alpha bravo charlie'", "alpha", "bravo", "charlie")
        self.assertGone('mytool --token="two words" next', "two", "words")

    def test_yaml_block_value(self):
        out = redact(f"db:\n  password: |\n    {OPAQUE}\n    second-line\n  host: localhost\n")
        self.assertNotIn(OPAQUE, out)
        self.assertNotIn("second-line", out)
        self.assertIn("host: localhost", out)


class Heredocs(unittest.TestCase):
    def test_heredoc_run_by_a_shell_is_checked(self):
        for cmd in ["bash <<'EOF'\ngit push origin main\nEOF", "ssh prod <<EOF\ngit push origin main\nEOF",
                    "cd app && sh -s <<EOF\ngit push origin main\nEOF"]:
            self.assertIn("git push origin main", command_text(cmd), cmd)

    def test_heredoc_as_data_is_not(self):
        for cmd in ["git commit -F - <<'EOF'\nnever git push origin main\nEOF", "cat > notes.md <<'EOF'\nbash\nEOF"]:
            self.assertNotIn("\n", command_text(cmd), cmd)

    def test_push_inside_bash_heredoc_breaks_the_rule(self):
        rs = rules.parse("- Never push to main `push-main`\n")
        a = Action("2026-11-05T10:00:00Z", "command", "Bash", "bash <<'EOF'\ngit push origin main\nEOF", Ref("s.jsonl", 3))
        s = Session("claude-code", "s", "/tmp/x", "s.jsonl", actions=[a])
        self.assertEqual(rules.check(s, rs)[0].status, "broken")


class UnlessAsked(unittest.TestCase):
    rule = rules.parse("- Merge only when I ask `command: \\bgh\\s+pr\\s+merge\\b unless asked: \\bmerge`\n")

    def status(self, *prompts):
        ps = [Prompt(f"2026-11-05T10:0{i}:00Z", t, Ref("s.jsonl", i + 1)) for i, t in enumerate(prompts)]
        a = Action("2026-11-05T11:00:00Z", "command", "Bash", "gh pr merge 3", Ref("s.jsonl", 20))
        return rules.check(Session("claude-code", "s", "/tmp/x", "s.jsonl", actions=[a], prompts=ps), self.rule)[0].status

    def test_a_no_is_not_permission(self):
        for no in ["do not merge this yet", "don't merge", "never merge without me", "no, don't merge it yet"]:
            self.assertEqual(self.status(no), "broken", no)
        self.assertEqual(self.status("looks good, merge it"), "kept")

    def test_permission_taken_back(self):
        self.assertEqual(self.status("merge it when green", "actually, don't merge yet"), "broken")
        self.assertEqual(self.status("don't merge yet", "ok now merge it"), "kept")


class StaleVerdicts(unittest.TestCase):
    rule = rules.parse("- Ask before building when the goal is unclear\n")

    def session(self, last):
        a = Action(last.isoformat(), "edit", "Edit", "app.py", Ref("s.jsonl", 9), files=["app.py"])
        return Session("claude-code", "s", "/tmp/x", "s.jsonl", actions=[a], ended=last.isoformat())

    def test_kept_goes_back_to_not_judged_when_the_session_went_on(self):
        at = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
        v = {self.rule[0].id: {"status": "kept", "why": "clear ask", "ref": None, "at": at.isoformat()}}
        self.assertEqual(rules.check(self.session(at + timedelta(minutes=5)), self.rule, v)[0].status, "kept")
        late = rules.check(self.session(at + timedelta(hours=2)), self.rule, v)[0]
        self.assertEqual(late.status, "judge")
        self.assertIn("went on", late.why)

    def test_broken_stays_broken(self):
        at = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
        v = {self.rule[0].id: {"status": "broken", "why": "no", "ref": {"file": "s.jsonl", "line": 9}, "at": at.isoformat()}}
        self.assertEqual(rules.check(self.session(at + timedelta(hours=2)), self.rule, v)[0].status, "broken")


class Forget(unittest.TestCase):
    def test_forget_removes_the_static_copy(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "archive").mkdir()
            (home / "s").mkdir()
            for f in ["index.html", "data.js", "s/x1.js"]:
                (home / f).write_text("receipt")
            from receipts import archive
            orig = archive.forget
            archive.forget = lambda root, ids, before: ["x1"]
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    cli.main(["forget", "x1", "--dir", str(home)])
            finally:
                archive.forget = orig
            self.assertFalse((home / "data.js").exists())
            self.assertFalse((home / "s").exists())
            self.assertIn("Static copy removed", out.getvalue())


class DaylightSaving(unittest.TestCase):
    def test_each_day_uses_its_own_offset(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Dublin"
        time.tzset()
        try:
            summer = Window.day(date(2026, 7, 1))
            self.assertEqual(summer.start.utcoffset(), timedelta(hours=1))
            self.assertEqual(Window.day(date(2026, 10, 25)).end - Window.day(date(2026, 10, 25)).start, timedelta(hours=25))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


if __name__ == "__main__":
    unittest.main()
