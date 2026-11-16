"""Which events count: a day (local time) or any time window."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


class Window:
    def __init__(self, start: datetime, end: datetime):
        self.start = start
        self.end = end

    @classmethod
    def day(cls, d: date) -> "Window":
        # Local midnight with that day's own offset (summer or winter), so DST days run 23 or 25 hours.
        start = datetime.combine(d, time.min).astimezone()
        end = datetime.combine(d + timedelta(days=1), time.min).astimezone()
        return cls(start, end)

    @classmethod
    def named(cls, word: str) -> "Window":
        """today, yesterday, week (the last 7 days, today included), all, or YYYY-MM-DD."""
        today = date.today()
        if word == "all":
            return cls.everything()
        if word == "week":
            return cls(cls.day(today - timedelta(days=6)).start, cls.day(today).end)
        d = {"today": today, "yesterday": today - timedelta(days=1)}.get(word) or date.fromisoformat(word)
        return cls.day(d)

    @classmethod
    def everything(cls) -> "Window":
        return cls(datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc))

    def has(self, ts: str | float | int | None) -> bool:
        t = parse_ts(ts)
        return t is not None and self.start <= t < self.end

    @property
    def start_epoch(self) -> float:
        try:
            return self.start.timestamp()
        except (OverflowError, ValueError):
            return 0.0


def parse_ts(ts: str | float | int | None) -> datetime | None:
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        secs = ts / 1000 if ts > 1e11 else ts  # milliseconds or seconds
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(ts: str | float | int | None) -> str:
    t = parse_ts(ts)
    return t.isoformat() if t else ""
