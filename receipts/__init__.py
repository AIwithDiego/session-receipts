"""Session receipts: one page of evidence per Claude Code or Codex session. Local only."""

from __future__ import annotations

from pathlib import Path

from . import claude_code, codex
from .model import Session
from .window import Window


def read_sessions(window: Window, tools=("claude-code", "codex"), claude_root: Path | None = None, codex_root: Path | None = None) -> list[Session]:
    out: list[Session] = []
    if "claude-code" in tools:
        out += claude_code.read_all(window, claude_root or claude_code.DEFAULT_ROOT)
    if "codex" in tools:
        out += codex.read_all(window, codex_root or codex.DEFAULT_ROOT)
    return sorted(out, key=lambda s: s.started or "")
