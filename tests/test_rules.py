"""Rules checked with proof (SR-4), on synthetic sessions."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from receipts import dashboard, rules
from receipts.model import Action, Prompt, Ref, Session

RULES = """# My rules
- Never push to main `push-main`
- No force pushes `force-push`
- Merge only when I ask `command: \\bgh\\s+pr\\s+merge\\b unless asked: \\bmerge`
- Never print secret files `secret-files`
- No keys in commands `secrets`
- Don't touch the clipboard `command: \\bpbcopy\\b`
- Leave the lockfile alone `edit: package-lock.json`
- Ask before building when the goal is unclear
- Broken pattern `command: (`
- Typo `comand: x`
"""

KEY = "ghp" + "_" + "Z" * 36  # built at runtime so scanners don't flag the file


def cmd(text, line, ok=True, at=None):
    return Action(at or f"2026-11-05T10:{line:02d}:00Z", "command", "Bash", text, Ref("s.jsonl", line), ok=ok)


def session(*actions, prompts=()):
    return Session("claude-code", "s", "/tmp/x", "s.jsonl", actions=list(actions), prompts=list(prompts))


def by_text(results):
    return {r.rule.text: r for r in results}


class Parse(unittest.TestCase):
    def test_kinds_and_errors(self):
        rs = {r.text: r for r in rules.parse(RULES)}
        self.assertEqual(rs["Never push to main"].kind, "push-main")
        self.assertEqual(rs["Merge only when I ask"].kind, "command")
        self.assertIsNotNone(rs["Merge only when I ask"].asked)
        self.assertFalse(rs["Ask before building when the goal is unclear"].proven)
        self.assertIn("Bad pattern", rs["Broken pattern"].error)
        self.assertIn("Don't know", rs["Typo"].error)
        self.assertEqual(len({r.id for r in rs.values()}), len(rs))  # stable, distinct ids

    def test_missing_file_uses_example(self):
        rs, src = rules.load(Path("/nonexistent/rules.md"))
        self.assertEqual(src, rules.EXAMPLE)
        self.assertTrue(any(r.proven for r in rs))

    def test_bom_and_utf16_files_are_read(self):
        # Notepad can add a BOM; `>` in Windows PowerShell 5.1 writes UTF-16. Both used to give zero rules.
        want = [r.text for r in rules.parse(RULES)]
        for encoding in ("utf-8-sig", "utf-16"):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "rules.md"
                p.write_bytes(RULES.encode(encoding))
                rs, _ = rules.load(p)
                self.assertEqual([r.text for r in rs], want, encoding)


class Check(unittest.TestCase):
    rules = rules.parse(RULES)

    def results(self, s):
        return by_text(rules.check(s, self.rules))

    def test_seeded_broken_rule_has_its_log_line(self):
        r = self.results(session(cmd("git status", 1), cmd("git push origin main", 7)))["Never push to main"]
        self.assertEqual(r.status, "broken")
        self.assertEqual((r.hits[0].ref.file, r.hits[0].ref.line), ("s.jsonl", 7))
        self.assertEqual(r.checked, 2)

    def test_blocked_attempt_is_tried_not_broken(self):
        r = self.results(session(cmd("git push origin main", 3, ok=False)))["Never push to main"]
        self.assertEqual(r.status, "tried")

    def test_feature_branch_push_is_kept(self):
        rs = self.results(session(cmd("git push -u origin feature/x", 1)))
        self.assertEqual(rs["Never push to main"].status, "kept")
        self.assertEqual(rs["No force pushes"].status, "kept")
        self.assertEqual(self.results(session(cmd("git push --force-with-lease origin feature/x", 1)))["No force pushes"].status, "broken")

    def test_unless_asked_needs_an_earlier_prompt(self):
        ask = Prompt("2026-11-05T10:00:00Z", "looks good, merge it", Ref("s.jsonl", 2))
        r = self.results(session(cmd("gh pr merge 3 --merge", 5), prompts=[ask]))["Merge only when I ask"]
        self.assertEqual(r.status, "kept")
        self.assertEqual(r.hits[0].allowed_by.ref.line, 2)
        late = Prompt("2026-11-05T11:00:00Z", "merge it", Ref("s.jsonl", 9))  # after the merge: doesn't count
        self.assertEqual(self.results(session(cmd("gh pr merge 3", 5), prompts=[late]))["Merge only when I ask"].status, "broken")

    def test_secret_files_printed_vs_loaded(self):
        rule = "Never print secret files"
        self.assertEqual(self.results(session(cmd("cat .env.local", 1)))[rule].status, "broken")
        self.assertEqual(self.results(session(cmd("tail -c 400 ~/.claude/.env", 1)))[rule].status, "broken")
        for fine in (
            "source ~/.claude/.env && curl -s api",
            "T=$(grep '^TOKEN=' ~/.claude/.env | cut -d= -f2-); curl -H \"Authorization: Bearer $T\" x",
            "grep -o '^[A-Z_]*' ~/.claude/.env",
            "cut -d= -f1 .env",
            "grep -l VERCEL ~/.claude/.env",
            "grep NEXT_PUBLIC_SUPABASE_URL .env.local",
            "cat .env.example",
            "cat > .env.local <<'EOF'\nX=1\nEOF",
            "grep -n KEY .env | sed -E 's/=.*/=<hidden>/'",
        ):
            with self.subTest(fine):
                self.assertEqual(self.results(session(cmd(fine, 1)))[rule].status, "kept")
        read = Action("2026-11-05T10:00:00Z", "read", "Read", "/p/.env", Ref("s.jsonl", 4), files=["/p/.env"])
        self.assertEqual(self.results(session(read))[rule].status, "broken")

    def test_secret_shapes(self):
        rule = "No keys in commands"
        self.assertEqual(self.results(session(cmd(f"curl -H 'Authorization: token {KEY}' x", 1)))[rule].status, "broken")
        demo = "eyJ" + "pc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24ifQ" + "." + "a" * 20 + "." + "b" * 20
        self.assertEqual(self.results(session(cmd(f"VITE_KEY={demo} npm run dev", 1)))[rule].status, "kept")

    def test_edit_glob_and_judged(self):
        e = Action("2026-11-05T10:00:00Z", "edit", "Edit", "/x/package-lock.json", Ref("s.jsonl", 3), files=["/x/package-lock.json"])
        rs = self.results(session(e))
        self.assertEqual(rs["Leave the lockfile alone"].status, "broken")
        self.assertEqual(rs["Ask before building when the goal is unclear"].status, "judge")
        self.assertEqual(rs["Broken pattern"].status, "error")


class TrialWeekNoise(unittest.TestCase):
    """False alarms from the trial week (SR-7): quoted text, inline code, editing a .env in place."""

    def results(self, s):
        return by_text(rules.check(s, rules.parse(RULES)))

    def test_quoted_text_is_data(self):
        for c in ['git commit -m "docs: never git push origin main"', 'grep -rn "gh pr merge" .',
                  "python3 -c 'print(\"git push --force origin main\")'", "echo 'cat .env to check'"]:
            rs = self.results(session(cmd(c, 1)))
            for rule in ("Never push to main", "No force pushes", "Merge only when I ask", "Never print secret files"):
                self.assertEqual(rs[rule].status, "kept", (c, rule))

    def test_quoted_text_a_shell_runs_still_counts(self):
        for c in ['bash -c "git push origin main"', "ssh build-box 'git push origin main'", 'eval "git push origin main"',
                  "git push origin 'main'"]:
            self.assertEqual(self.results(session(cmd(c, 1)))["Never push to main"].status, "broken", c)

    def test_secret_output_from_quoted_shell_commands(self):
        for c in ['bash -c "cat .env"', "sh -c 'tail .env'", 'ssh box "cat .env"',
                  'eval "cat .env"', 'bash -c "echo ready; cat .env"',
                  "bash -c 'echo \"$(cat .env)\"'"]:
            with self.subTest(command=c):
                self.assertEqual(self.results(session(cmd(c, 1)))["Never print secret files"].status, "broken")

    def test_secret_output_from_printed_substitutions(self):
        for c in ['echo "$(cat .env)"', 'printf "%s\\n" "$(tail .env)"',
                  'echo "prefix $(cat .env.local) suffix"', 'echo "$(bash -c \'cat .env\')"',
                  r'echo "\\$(cat .env)"']:
            with self.subTest(command=c):
                self.assertEqual(self.results(session(cmd(c, 1)))["Never print secret files"].status, "broken")

    def test_nested_secret_commands_that_do_not_print_values(self):
        for c in ['grep -rn "cat .env" docs/', "echo '$(cat .env)'", 'source .env',
                  'T=$(grep TOKEN .env | cut -d= -f2-)', 'T=$(bash -c "cat .env")',
                  'bash -c "source .env"', 'bash -c "cat .env | wc -l"',
                  'bash -c "cat .env" | wc -l',
                  'echo "$(cat .env | cut -d= -f1)"', 'echo "$(cat .env)" | wc -c',
                  'echo "$(cat .env.example)"', 'bash -c "echo \'cat .env\'"',
                  r'echo "\$(cat .env)"', r'printf "%s" "\$(tail .env)"']:
            with self.subTest(command=c):
                self.assertEqual(self.results(session(cmd(c, 1)))["Never print secret files"].status, "kept")

    def test_release_review_bypasses_are_caught(self):
        env = "." + "env"
        push = ["git commit -m 'don'\\''t' && git push origin main && echo 'done ok'",
                'out="$(git push origin main 2>&1)"', "echo 'git push origin main' | bash",
                "git -C ~/repo push origin main", "git push origin +main", "git push origin HEAD:refs/heads/main"]
        for c in push:
            self.assertEqual(self.results(session(cmd(c, 1)))["Never push to main"].status, "broken", c)
        for c in [f"git commit -m 'don'\\''t' && cat {env} && echo 'done ok'",
                  f'cat "$HOME/Library/Application Support/x/{env}"']:
            self.assertEqual(self.results(session(cmd(c, 1)))["Never print secret files"].status, "broken", c)
        self.assertEqual(self.results(session(cmd('echo "$(gh pr merge 12)"', 1)))["Merge only when I ask"].status, "broken")

    def test_branch_named_like_main_is_not_main(self):
        for c in ["git push origin main-fix", "git push origin feature:main.v2", "git push -u origin master/old"]:
            self.assertEqual(self.results(session(cmd(c, 1)))["Never push to main"].status, "kept", c)

    def test_quotes_pair_left_to_right(self):
        # An empty '' must not pair with the next quote and swallow the file name in between.
        c = "grep -c '' app/.env; grep -n 'STRIPE_KEY' app/.env | cut -c1-20"
        self.assertEqual(self.results(session(cmd(c, 1)))["Never print secret files"].status, "broken")

    def test_quoted_secret_still_counts(self):
        self.assertEqual(self.results(session(cmd(f'curl -H "Authorization: token {KEY}" x', 1)))["No keys in commands"].status, "broken")

    def test_editing_a_secret_file_is_not_printing(self):
        rs = self.results(session(cmd("sed -i '' 's/^KEY=.*/KEY=new/' .env && grep -c '^KEY=' .env", 1)))
        self.assertEqual(rs["Never print secret files"].status, "kept")
        self.assertEqual(self.results(session(cmd("sed -n 1,5p .env", 1)))["Never print secret files"].status, "broken")


class OnTheReceipt(unittest.TestCase):
    def test_build_shows_rules_and_redacts_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "rules.md").write_text(RULES)
            s = session(cmd("git push origin main", 7), cmd(f"echo {KEY} | pbcopy", 8))
            s.started = s.ended = "2026-11-05T10:00:00+00:00"
            dashboard.build([s], out)
            data = json.loads(re.match(r"window.RECEIPTS=(.*);$", (out / "data.js").read_text(encoding="utf-8"), re.S).group(1))
            ids = {r["text"]: r["id"] for r in data["rules"]["list"]}
            row = data["sessions"][0]
            self.assertIn(ids["Never push to main"], row["broken"])
            self.assertIn(ids["Don't touch the clipboard"], row["broken"])
            receipt = (out / "s" / "s.js").read_text(encoding="utf-8")
            self.assertIn('"line":7', receipt)
            self.assertNotIn(KEY[4:], receipt + (out / "data.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
