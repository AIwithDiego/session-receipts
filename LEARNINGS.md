# Learnings: one week of receipts on my own sessions

Trial week: 9 to 15 Nov 2026 (first runs on 8 Nov). 177 Claude Code sessions across 19 projects, about 1.2 billion tokens. Eleven rules: seven checked from the logs, four judged by the agent.

## What it caught

**A push straight to main.** On 9 Nov an agent finished a release with `git push origin feature/v1:main`, after a "let's finish it, go ahead". My setup blocks commits on main with a pre-commit hook. A push from another branch never makes a commit on main, so the hook never ran. The receipt found what my own guard missed. Fixed since with a global pre-push hook that refuses any push to an existing main or master.

**Part of a test key printed.** On 11 Nov an agent checked a Stripe test key by grepping the .env file and masking all but the first six characters. Masked and test-only, but still a secret file printed to the terminal. My rule says load secrets through a script.

## What was noise

- **Editing is not printing.** An `sed -i` on a .env value plus `grep -c` (a count, no value shown) was flagged as printing a secret file. The check sees ".env" in a command and stops there.
- **Words inside quotes and code.** On 8 Nov all three proven breaks were false: `git push origin main` inside a test script, fake test secrets, a search for `gh pr merge`. Checks should skip quoted strings and inline code.
- **Judging is the weak half.** Judged rules see the first 300 characters of each action and none of the agent's own replies, so "no em dashes" and "ask before building" mostly stay "not judged".
- **Scheduled runs crowd the list.** 91 of the 177 sessions were my hourly home-page refresh. They need a filter or their own lane.

## Fixed before the public release

Replaying 8 to 16 Nov through the fixed checks: the three false alarms above are gone (a search for `gh pr merge`, a heredoc that mentioned main, the .env edit), every real catch is still there, and the wider push check found three more real pushes to main that the old pattern missed (`git push origin $SHA:refs/heads/main`).

- Quoted text with a space inside and heredocs fed to a program count as data, unless `sh -c`, `eval` or `ssh` runs them.
- `sed -i` on a secret file is an edit, not a print.
- Pushes to main written as `'main'`, `+main`, `HEAD:refs/heads/main` or `git -C dir push` are now caught; they slipped through before.
- A security review before release found ways the new quote handling could hide a real push (`$(...)` inside quotes, quotes piped into a shell, escaped apostrophes). Fixed, with a test for each.
- `read week` and `judge week`, and the skill answers "receipts for the week" with the breaks first.
- Kept on purpose: a fake key in a command still counts as a secret. From the log alone, fake and real look the same.

Still open: judged rules still see only the first 300 characters of each action and none of the agent's replies, and scheduled runs get counted, not filtered.

## What I learned about using it

I asked for receipts once, on day one, and never again. The dashboard was running all week and I didn't open it. A tool you have to remember to ask for gets forgotten. The catch that mattered came from running it after the fact over the whole week. The useful shape is a weekly digest that comes to you: breaks first, with the log line, and noise kept out so you trust it.
