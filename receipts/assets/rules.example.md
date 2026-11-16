# My rules

One rule per "- " line, in plain language. A check in backticks makes it proven from the logs;
without one, the rule is judged (by the agent, when you ask for a receipt).
Checks: push-main · force-push · secret-files · secrets · command: <regex> · file: <glob> · edit: <glob>,
optionally followed by "unless asked: <regex>" (allowed when an earlier prompt in the session matched).

- Never push to main `push-main`
- Never force push `force-push`
- Merge only when I ask `command: \bgh\s+pr\s+merge\b unless asked: \bmerge`
- Never print secret files (.env, keys, credentials) `secret-files`
- No API keys or tokens in commands `secrets`
- Don't touch the clipboard `command: \b(pbcopy|pbpaste)\b`
- Ask before building when the goal is unclear
