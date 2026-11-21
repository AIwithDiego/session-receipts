"""SR-5: the agent judges the fuzzy rules and the receipt shows its verdict."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from receipts import __main__ as cli
from receipts import dashboard, read_sessions, server
from receipts.window import Window

FIX = Path(__file__).parent / "fixtures"
RULES = "- Never push to main `push-main`\n- Ask before building when the goal is unclear\n"
FAKE_KEY = "sk-ant-" + "api03-" + "Q" * 40


class Judge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.logs = root / "home", root / "logs"
        shutil.copytree(FIX / "claude", self.logs / "claude")
        shutil.copytree(FIX / "codex", self.logs / "codex")
        self.home.mkdir()
        (self.home / "rules.md").write_text(RULES)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), _stdin(stdin):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def judge(self, *extra):
        code, out, _ = self.run_cli("judge", "2026-11-05", "--dir", str(self.home), "--claude-dir",
                                    str(self.logs / "claude"), "--codex-dir", str(self.logs / "codex"), *extra)
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_judge_lists_only_judged_rules_with_evidence(self):
        rep = self.judge()
        self.assertEqual([r["text"] for r in rep["rules"]], ["Ask before building when the goal is unclear"])
        rid = rep["rules"][0]["id"]
        s1 = next(s for s in rep["sessions"] if s["session"] == "s1")
        self.assertEqual(s1["judge"], [rid])
        self.assertIn("fix the login bug", [p["text"] for p in s1["prompts"]])
        self.assertTrue(all(p["ref"].endswith(tuple(f":{n}" for n in range(1, 99))) for p in s1["prompts"]))

    def test_verdict_shows_on_the_receipt_and_is_not_asked_again(self):
        rep = self.judge()
        rid = rep["rules"][0]["id"]
        s1 = next(s for s in rep["sessions"] if s["session"] == "s1")
        ref = s1["actions"][0]["ref"]
        batch = json.dumps([{"session": "s1", "rule": rid, "status": "broken",
                             "why": f"Edited app.py without asking; key {FAKE_KEY}", "ref": ref}])
        code, out, err = self.run_cli("verdict", "--batch", "-", "--dir", str(self.home), stdin=batch)
        self.assertEqual(code, 0, err)

        stored = (self.home / "verdicts.json").read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, stored)

        self.assertNotIn("s1", [s["session"] for s in self.judge()["sessions"]])
        self.assertIn("s1", [s["session"] for s in self.judge("--again")["sessions"]])

        sessions = read_sessions(Window.everything(), ("claude-code", "codex"), self.logs / "claude", self.logs / "codex")
        dashboard.build(sessions, self.home)
        detail = (self.home / "s" / "s1.js").read_text(encoding="utf-8")
        res = json.loads(detail[detail.index(",") + 1: detail.rindex(")")])
        rule = next(r for r in res["rules"] if r["id"] == rid)
        self.assertEqual(rule["status"], "broken")
        self.assertTrue(rule["why"].startswith("Edited app.py without asking"))
        self.assertEqual(f"{rule['hits'][0]['ref']['file']}:{rule['hits'][0]['ref']['line']}", ref)
        data = (self.home / "data.js").read_text(encoding="utf-8")
        self.assertIn(rid, json.loads(data[len("window.RECEIPTS="):-1])["sessions"][-2]["broken"]
                      + json.loads(data[len("window.RECEIPTS="):-1])["sessions"][-1]["broken"])

    def test_live_server_picks_up_new_verdicts(self):
        store = server.Store(self.logs / "claude", self.logs / "codex", self.home)
        store.refresh(force=True)
        rid = self.judge()["rules"][0]["id"]
        self.assertEqual(next(r for r in store.detail("s1")["rules"] if r["id"] == rid)["status"], "judge")
        self.run_cli("verdict", "s1", rid, "kept", "--why", "Asked what done looks like first.", "--dir", str(self.home))
        store.refresh(force=True)
        self.assertEqual(next(r for r in store.detail("s1")["rules"] if r["id"] == rid)["status"], "kept")

    def test_piped_batch_may_start_with_a_bom(self):
        # Windows PowerShell 5.1 puts a BOM first when it pipes text.
        code, out, err = self.run_cli("verdict", "--batch", "-", "--dir", str(self.home), stdin="\ufeff[]")
        self.assertEqual(code, 0, err)

    def test_delete_removes_the_batch_file_whatever_happens(self):
        rid = self.judge()["rules"][0]["id"]
        good = json.dumps([{"session": "s1", "rule": rid, "status": "kept", "why": "Asked first."}])
        bad = json.dumps([{"session": "s1", "rule": rid, "status": "maybe", "why": f"key {FAKE_KEY}"}])
        f = self.home / "verdict-batch.json"
        for text, want in ((good, 0), (bad, 2), ("not json " + FAKE_KEY, 2)):
            f.write_text(text, encoding="utf-8")
            code, _, err = self.run_cli("verdict", "--batch", str(f), "--delete", "--dir", str(self.home))
            self.assertEqual(code, want, err)
            self.assertFalse(f.exists(), text[:20])
        f.write_text(good, encoding="utf-8")  # without --delete the file is the user's and stays
        self.assertEqual(self.run_cli("verdict", "--batch", str(f), "--dir", str(self.home))[0], 0)
        self.assertTrue(f.exists())
        self.assertEqual(self.run_cli("verdict", "--batch", "-", "--delete", "--dir", str(self.home), stdin="[]")[0], 0)
        other = self.home / "other.json"  # --delete never removes any other file
        other.write_text("[]", encoding="utf-8")
        self.assertEqual(self.run_cli("verdict", "--batch", str(other), "--delete", "--dir", str(self.home))[0], 2)
        self.assertTrue(other.exists())

    def test_bad_verdicts_are_refused_and_nothing_is_written(self):
        for batch in ('[{"session": "s1", "rule": "x", "status": "maybe", "why": "?"}]',
                      '[{"session": "s1", "rule": "x", "status": "kept"}]',
                      '[{"session": "s1", "rule": "x", "status": "broken", "why": "y", "ref": "nope"}]',
                      '[{"session": "s1", "rule": "x", "status": "broken", "why": "no ref"}]',
                      '[{"session": "s1", "rule": "x", "status": "broken", "why": "y", "ref": {"file": "a"}}]',
                      '[{"session": "s1", "rule": "x", "status": "broken", "why": "y", "ref": "a.jsonl:-3"}]',
                      '{"not": "a list"}'):
            code, _, err = self.run_cli("verdict", "--batch", "-", "--dir", str(self.home), stdin=batch)
            self.assertEqual(code, 2, batch)
            self.assertIn("error", err)
        self.assertFalse((self.home / "verdicts.json").exists())


    def test_report_has_a_size_budget(self):
        from receipts import judge
        old = judge.BUDGET
        judge.BUDGET = 10
        try:
            rep = self.judge()
        finally:
            judge.BUDGET = old
        self.assertEqual(len(rep["sessions"]), 1)  # always at least one
        self.assertTrue(rep["truncated"])


@contextlib.contextmanager
def _stdin(text):
    import sys
    old = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = old


if __name__ == "__main__":
    unittest.main()
