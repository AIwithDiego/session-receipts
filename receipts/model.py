"""One session shape, whichever tool wrote the log."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Ref:
    """Where a fact came from: the log file and line number. This is the proof."""

    file: str
    line: int


@dataclass
class Prompt:
    at: str
    text: str
    ref: Ref
    kind: str = "typed"  # typed | slash | shell


@dataclass
class Action:
    at: str
    kind: str  # command | edit | read | search | web | agent | tool
    name: str  # tool name as the agent called it (Bash, Edit, exec, mcp server.tool)
    detail: str  # the command, path or short summary
    ref: Ref
    files: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    by_subagent: bool = False


@dataclass
class Denial:
    """An action that was blocked: a hook, a permission check or the user said no."""

    at: str
    what: str
    reason: str
    source: str  # hook | permission | user | sandbox
    ref: Ref


@dataclass
class Tokens:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0  # Codex reports it apart; for Claude it sits inside output

    def add(self, other: "Tokens") -> None:
        for k in ("input", "cache_read", "cache_write", "output", "reasoning"):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output


@dataclass
class Session:
    tool: str  # claude-code | codex
    id: str
    cwd: str
    log: str
    started: str = ""
    ended: str = ""
    title: str = ""
    branch: str = ""
    models: list[str] = field(default_factory=list)
    permission_mode: str = ""  # e.g. bypassPermissions, or Codex "never / danger-full-access"
    prompts: list[Prompt] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    tokens: Tokens = field(default_factory=Tokens)
    subagents: int = 0
    notes: list[str] = field(default_factory=list)  # what the reader could not read

    @property
    def files_changed(self) -> list[str]:
        return sorted({f for a in self.actions if a.kind == "edit" and a.ok for f in a.files})

    @property
    def commands(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "command"]

    def empty(self) -> bool:
        return not (self.prompts or self.actions)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["files_changed"] = self.files_changed
        d["tokens"]["total"] = self.tokens.total
        return d
