# Changelog

## 1.1.1 · 19 Nov 2026

Follow-ups from the review of the Windows work.

- `verdict --batch FILE --delete` removes the file once it is read, also when a verdict is refused. It only works on `verdict-batch.json` in the receipts folder. On PowerShell the skill now writes the verdicts there and always uses `--delete`, so text that is not redacted yet never stays on disk (#17).
- `verdict --batch -` accepts piped input that starts with a BOM, which Windows PowerShell 5.1 sends (#19).
- README and SECURITY.md say where file privacy comes from on Windows: your profile folder, not file modes (#18).

## 1.1.0 · 19 Nov 2026

Runs on Windows. All three changes came from an outside contributor, @deyenonevik (#11, #13, #14).

- Windows: `dashboard --open` and `serve --open` open the default browser on any system, output is UTF-8 everywhere, and `rules.md` and verdict files are read with a BOM or as UTF-16 (what Windows PowerShell 5.1 writes). `serve --install` stays macOS only and now says so before it writes anything.
- CI runs the tests on Windows as well as Linux and macOS.
- Dates show the year: `2026-11-18 Wed`. The archive outlives a year end, so "14 Nov" alone was ambiguous.
- The chart tooltip shows anywhere on a bar, not only in the space above it.
- Not on Windows yet: the command and path checks for PowerShell and `cmd` (see #10).

## 1.0.0 · 16 Nov 2026

First public release, after a week of daily use on real sessions (see LEARNINGS.md).

- Reads Claude Code (`~/.claude/projects`) and Codex (`~/.codex/sessions`) logs into one session shape, every fact with its log file and line.
- A receipt per session: prompts, files changed, commands run, what was blocked, risky moments, tokens.
- Your rules in plain language, proven from the logs (`push-main`, `force-push`, `secret-files`, `secrets`, `command:`, `file:`, `edit:`, `unless asked:`) or judged by the agent with the log line as proof.
- Live dashboard on localhost, a static copy, and a history archive that outlives Claude Code's 30-day log cleanup.
- Secrets redacted before anything is written, shown or cut short, with a leak test across every output.
- One SKILL.md for Claude Code and Codex: "receipts for today" and "receipts for the week".
- Checks read what the shell runs: quoted text and heredocs are data unless a shell runs them.
