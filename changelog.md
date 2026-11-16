# Changelog

## 1.0.0 · 16 Nov 2026

First public release, after a week of daily use on real sessions (see LEARNINGS.md).

- Reads Claude Code (`~/.claude/projects`) and Codex (`~/.codex/sessions`) logs into one session shape, every fact with its log file and line.
- A receipt per session: prompts, files changed, commands run, what was blocked, risky moments, tokens.
- Your rules in plain language, proven from the logs (`push-main`, `force-push`, `secret-files`, `secrets`, `command:`, `file:`, `edit:`, `unless asked:`) or judged by the agent with the log line as proof.
- Live dashboard on localhost, a static copy, and a history archive that outlives Claude Code's 30-day log cleanup.
- Secrets redacted before anything is written, shown or cut short, with a leak test across every output.
- One SKILL.md for Claude Code and Codex: "receipts for today" and "receipts for the week".
- Checks read what the shell runs: quoted text and heredocs are data unless a shell runs them.
