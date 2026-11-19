"""Readers on synthetic logs. Real session logs never go in this repo."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from receipts import claude_code, codex, read_sessions
from receipts.window import Window

FIX = Path(__file__).parent / "fixtures"
START = datetime(2026, 11, 5, tzinfo=timezone.utc)
DAY = Window(START, START + timedelta(days=1))


class ClaudeCodeReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        [cls.s] = claude_code.read_all(DAY, FIX / "claude")

    def test_meta(self):
        s = self.s
        self.assertEqual((s.title, s.cwd, s.branch), ("Fix login", "/Users/demo/proj", "feat"))
        self.assertEqual(s.permission_mode, "bypassPermissions")
        self.assertEqual(s.models, ["claude-test-1"])

    def test_prompts_skip_meta_and_other_days(self):
        self.assertEqual(
            [(p.kind, p.text) for p in self.s.prompts],
            [("typed", "fix the login bug"), ("slash", "/review high"), ("shell", "! ls")],
        )

    def test_actions(self):
        kinds = [(a.kind, a.detail, a.by_subagent) for a in self.s.actions]
        self.assertIn(("command", "git status", False), kinds)
        self.assertIn(("agent", "look around", False), kinds)
        self.assertIn(("read", "/Users/demo/proj/README.md", True), kinds)
        self.assertEqual(self.s.files_changed, ["/Users/demo/proj/app.py", "/Users/demo/proj/notes.md"])
        self.assertEqual(self.s.subagents, 1)

    def test_failed_command_is_not_a_denial(self):
        [a] = [a for a in self.s.actions if a.detail == "false"]
        self.assertFalse(a.ok)
        self.assertNotIn("false", [d.what for d in self.s.denials])

    def test_denials_with_proof(self):
        got = {d.what: d.source for d in self.s.denials}
        self.assertEqual(got, {"cat .env": "hook", "git push origin main": "user"})
        d = next(d for d in self.s.denials if d.what == "cat .env")
        line = Path(d.ref.file).read_text(encoding="utf-8").splitlines()[d.ref.line - 1]
        self.assertIn("SECURITY GUARD", line)

    def test_tokens_counted_once_per_message(self):
        # 6 API messages (m1 logged twice), each 10 in + 100 cache read + 20 cache write + 5 out
        t = self.s.tokens
        self.assertEqual((t.input, t.cache_read, t.cache_write, t.output), (60, 600, 120, 30))

    def test_bad_line_noted(self):
        self.assertTrue(any("unreadable" in n for n in self.s.notes))


class CodexReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sessions = {s.id: s for s in codex.read_all(DAY, FIX / "codex")}
        cls.s = cls.sessions["cx1"]

    def test_meta(self):
        s = self.s
        self.assertEqual((s.cwd, s.models), ("/Users/demo/other", ["gpt-test"]))
        self.assertEqual(s.permission_mode, "approval never · sandbox danger-full-access")

    def test_prompt_and_commands(self):
        self.assertEqual([p.text for p in self.s.prompts], ["add a readme"])
        self.assertEqual([c.detail for c in self.s.commands], ["git push origin main", "npm test", "rm -rf build"])
        failed = next(c for c in self.s.commands if c.detail == "npm test")
        self.assertEqual((failed.ok, failed.error), (False, "exit 1: 1 failing"))

    def test_edits_tools_denials(self):
        self.assertEqual(self.s.files_changed, ["/Users/demo/other/README.md"])
        self.assertIn("github.create_pr", [a.name for a in self.s.actions])
        self.assertEqual([(d.what, d.reason) for d in self.s.denials], [("rm -rf build", "declined")])

    def test_tokens_dedup_and_cached_split(self):
        t = self.s.tokens
        self.assertEqual((t.input, t.cache_read, t.output, t.reasoning, t.total), (400, 1600, 100, 40, 2100))

    def test_unknown_and_old_format_are_noted_not_guessed(self):
        self.assertTrue(any("BrandNewThing" in n for n in self.s.notes))
        old = next(s for s in self.sessions.values() if s.id.endswith("old"))
        self.assertTrue(old.empty())
        self.assertTrue(any("older Codex log format" in n for n in old.notes))


class BothTools(unittest.TestCase):
    def test_same_shape_sorted_by_start(self):
        ss = read_sessions(DAY, claude_root=FIX / "claude", codex_root=FIX / "codex")
        self.assertEqual([s.tool for s in ss if not s.empty()], ["claude-code", "codex"])
        for s in ss:
            d = s.to_dict()
            self.assertTrue({"prompts", "actions", "denials", "tokens", "files_changed"} <= set(d))

    def test_nothing_outside_the_window(self):
        later = Window(START + timedelta(days=1), START + timedelta(days=2))
        ss = read_sessions(later, claude_root=FIX / "claude", codex_root=FIX / "codex")
        self.assertEqual(ss, [])


if __name__ == "__main__":
    unittest.main()
