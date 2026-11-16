"""Keep receipts after the raw logs are gone.

Claude Code deletes session logs it hasn't touched in 30 days. Every dashboard
build saves each session's redacted summary and receipt here, one gzipped file
per session under archive/<YYYY-MM>/, so history builds up without keeping the
raw logs (which can hold secrets) any longer.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import date
from pathlib import Path

from .redact import redact_tree

VERSION = 1


def _size(entry: dict) -> int:
    s = entry["summary"]
    return s.get("prompts", 0) + s.get("actions", 0)


def load(root: Path) -> tuple[dict[str, tuple[Path, dict]], int]:
    """Every archived entry by session id, plus how many files could not be read."""
    found: dict[str, tuple[Path, dict]] = {}
    bad = 0
    if not root.is_dir():
        return found, bad
    for p in sorted(root.glob("*/*.json.gz")):
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                e = json.load(fh)
            sid = e["summary"]["id"]
        except (OSError, ValueError, KeyError, TypeError):
            bad += 1
            continue
        clean = redact_tree(e)
        if clean != e:  # newer patterns catch more: scrub the copy on disk too, once
            _write(p, clean)
            e = clean
        if sid not in found or _size(e) > _size(found[sid][1]):
            found[sid] = (p, e)
    return found, bad


def save(root: Path, entry: dict) -> Path:
    month = (entry["summary"].get("day") or "unknown")[:7]
    folder = root / month
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{entry['summary']['id']}.json.gz"
    _write(path, entry)
    return path


def _write(path: Path, entry: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as fh:
        json.dump({**entry, "v": VERSION}, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def forget(root: Path, ids: set[str] | None = None, before: date | None = None) -> list[str]:
    """Delete archived receipts by session id, or every one from days before `before`.
    Returns the ids removed."""
    gone = []
    found, _ = load(root)
    for sid, (path, e) in found.items():
        day = e["summary"].get("day") or ""
        if (ids and sid in ids) or (before and day and day < before.isoformat()):
            for p in root.glob(f"*/{sid}.json.gz"):
                p.unlink(missing_ok=True)
            gone.append(sid)
    return gone


def keep(root: Path, stored: dict[str, tuple[Path, dict]], e: dict) -> dict:
    """Archive one live entry unless the archive holds more of that session; return
    whichever copy is fuller. Updates stored in place."""
    sid = e["summary"]["id"]
    old = stored.get(sid)
    if old and _size(old[1]) > _size(e):
        return old[1]
    path = save(root, e)
    if old and old[0] != path:
        old[0].unlink(missing_ok=True)
    stored[sid] = (path, e)
    return e


def sync(root: Path, live: dict[str, dict]) -> tuple[dict[str, dict], set[str], int]:
    """Save the live entries and merge them with the archive.

    A live session replaces its archived copy unless the archive holds more of it
    (a build limited with --days can read only part of a long session).
    Returns every entry by id, the ids that came only from the archive, and the
    number of unreadable archive files.
    """
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    stored, bad = load(root)
    merged = {sid: keep(root, stored, e) for sid, e in live.items()}
    only = {sid for sid in stored if sid not in live}
    for sid in only:
        merged[sid] = stored[sid][1]
    return merged, only, bad
