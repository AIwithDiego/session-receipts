"""Scrub secrets from any text before it is written to the dashboard."""

from __future__ import annotations

import re
from functools import lru_cache

R = "[redacted]"

# Secret shapes that can't be mistaken for anything else. The rules engine uses these too.
SHAPES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----.*?(-----END [A-Z ]*PRIVATE KEY( BLOCK)?-----|$)", re.S),
     "[redacted private key]"),
    (re.compile(r"\bsk-ant-[\w-]{10,}"), R),
    (re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}"), R),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), R),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), R),
    (re.compile(r"\bxox[abprs]-[\w-]{10,}"), R),
    (re.compile(r"\bAIza[\w-]{30,}"), R),
    (re.compile(r"\b(sb_secret|sbp|re|rk_live|sk_live|pk_live|rk_test|sk_test|whsec)_[A-Za-z0-9_]{16,}"), R),
    (re.compile(r"\b(npm|hf|dop_v1|shpat|shpss|gsk|lin_api)_[A-Za-z0-9_-]{20,}"), R),
    (re.compile(r"\b(pplx|glpat)-[\w-]{20,}"), R),
    (re.compile(r"\bxapp-\d-[\w-]{20,}"), R),
    (re.compile(r"\bpypi-[\w-]{50,}"), R),
    (re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{50,}"), R),
    (re.compile(r"\bSG\.[\w-]{16,}\.[\w-]{16,}"), R),  # SendGrid
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), R),
    (re.compile(r"\b\d{8,10}:AA[\w-]{30,}"), R),  # Telegram bot
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), R),  # JWT
    (re.compile(r"(hooks\.slack\.com/(services|workflows|triggers)/)[\w/-]+"), r"\1" + R),
    (re.compile(r"(discord(app)?\.com/api/webhooks/\d+/)[\w-]+"), r"\1" + R),
]

# Secret-sounding names. Bounded on both sides so a long blob can't make matching slow.
_NAME = (r"(?<![\w.-])[\w.-]{0,40}?(secret|token|passw(or)?d|api[_-]?key|apikey|private[_-]?key|access[_-]?key"
         r"|account[_-]?key|shared[_-]?access[_-]?key|auth(?!or)|credential)[\w.-]{0,40}")
# A value: a quoted string (spaces allowed) or a bare word.
_VALUE = r"(?P<v>\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|[^\s\"'&,;}]{6,})"

def _keep_quotes(m: re.Match) -> str:
    v = m.group("v")
    q = v[0] if v[0] in "\"'" else ""
    return m.group(1) + q + R + q


# Secrets given away by what surrounds them: headers, flags, URLs, NAME=value.
CONTEXT: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\b((proxy-)?authorization\s*[:=]\s*[\"']?(basic|bearer|token|digest|apikey|key)?\s*)[^\s\"']{4,}"),
     r"\1" + R),
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 " + R),
    (re.compile(r"(?i)\b((set-)?cookie:\s*)[^\r\n\"']+"), r"\1" + R),
    (re.compile(r"(?i)([\"'](set-)?cookie[\"']\s*:\s*[\"'])[^\"'\n]+"), r"\1" + R),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1" + R + "@"),  # user:password@host
    (re.compile(r"(://)[A-Za-z0-9_-]{20,}@"), r"\1" + R + "@"),  # token@host (DSNs)
    (re.compile(r"(?i)([?&;][\w.-]{0,32}?(key|token|auth|sig|signature|secret|password|credential|code)=)[^&\s\"'#]+"),
     r"\1" + R),
    (re.compile(r"(?i)(--(password|passwd|token|secret|api-?key|auth-?token|access-?token)[= ]\s*)" + _VALUE.replace("{6,}", "{4,}")),
     _keep_quotes),
    # curl -u user:password, curl --user user:password
    (re.compile(r"(\b(curl|wget|http)\b[^\n|;&]{0,200}?\s(-u|--user)[= ]\s*[\"']?[^\s:\"']+:)(?!//)[^\s\"']+"), r"\1" + R),
    (re.compile(r"(\b(mysql|mysqldump|mariadb|mysqladmin)\b[^\n|;&]{0,200}?\s-p)[^\s\"']{3,}"), r"\1" + R),
    # ENV_NAME=value where the name ends like a secret (RESEND_KEY, MY_PAT, SENTRY_DSN, DB_PW)
    (re.compile(r"\b([A-Z][A-Z0-9_]{0,60}_(KEY|PAT|DSN|SECRET|TOKEN|PASSWORD|PASSWD|PASS|PWD|PW|AUTH|CREDENTIALS?)"
                r"\s*=\s*[\"']?)[^\s\"']{4,}"), r"\1" + R),
    # YAML block value under a secret name: "password: |" then indented lines
    (re.compile(r"(?im)^([ \t]*)(" + _NAME + r"[\"']?\s*:\s*[|>][-+]?[ \t]*\n)((?:\1[ \t]+\S[^\n]*(?:\n|$))+)"),
     lambda m: m.group(1) + m.group(2) + m.group(1) + "  " + R + "\n"),
    # name=value / "name": "value" where the name says secret
    (re.compile(r"(?i)(" + _NAME + r"[\"']?\s*[:=]\s*)" + _VALUE), _keep_quotes),
]

PATTERNS = SHAPES + CONTEXT


# Every pattern needs one of these somewhere in the text; most text has none and is skipped.
_MAYBE = re.compile(
    r"[A-Za-z0-9_-]{16}|-----BEGIN|://|\s(-u|--user)[= ]|[?&;][\w.-]*=|--\w|\s-p\S|eyJ|xox|webhooks|hooks\.slack|\d:AA"
    r"|(?i:bearer|token|cookie|auth|secret|passw|key|credential|_pw|_pass|_pat|_dsn)"
)


def redact(text: str) -> str:
    if not text or len(text) < 8:
        return text
    # The same text is redacted several times per build (cut, receipt, archive check): cache short ones.
    return _cached(text) if len(text) <= 4000 else _redact(text)


def _redact(text: str) -> str:
    if not _MAYBE.search(text):
        return text
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


_cached = lru_cache(maxsize=4096)(_redact)


def forget_cache() -> None:
    """Drop the cached raw text. Called after each build or refresh, so nothing unredacted stays in memory."""
    _cached.cache_clear()


def clip(text: str, n: int) -> str:
    """Redact, then cut. Cutting first can split a secret so no pattern sees it."""
    return redact(text or "")[:n]


def tail(text: str, n: int) -> str:
    """Redact, then keep the last n characters."""
    return redact(text or "")[-n:]


def redact_tree(obj):
    """Redact every string in a dict/list structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_tree(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_tree(v) for k, v in obj.items()}
    return obj
