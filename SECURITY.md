# Security

Session receipts reads logs that can hold secrets and other people's data, so security reports matter.

## Report privately

Use GitHub's **Report a vulnerability** button on the Security tab. Please don't open a public issue.

## In scope

- A secret that survives redaction into any output: the dashboard, the archive, `read --json` or the live server.
- The live server reachable from anything but this machine's loopback, or readable by another website.
- Anything that sends data off the machine.
- A way for log content to make the skill, or the agent running it, run commands or change files.

## Out of scope

A rule check that misses or over-flags a command. The checks are patterns, not a shell parser, by design: open a normal issue with the command.

## File privacy on Windows

On macOS and Linux, receipts, the archive and verdicts are written with modes 0700 and 0600. Windows ignores those modes. There, privacy comes from your profile folder: the default `%USERPROFILE%\.session-receipts` can be read only by you and administrators. A folder outside your profile (for example `--out C:\receipts`) can be readable by other accounts on the machine, so keep `--out` and `--dir` inside your profile.

## Supported versions

The latest release only. Reports get a reply within 7 days, best effort.
