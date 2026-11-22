"""Remove sensitive values from defect text before it is sent to an external provider.

Only placeholder counts are ever logged or stored; the scrubbed text itself is sent to
the LLM and never logged.
"""

import html
import re
from dataclasses import dataclass, field

_TAG = re.compile(r"<[^>]{0,2000}>")
_BLOCK_TAG = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\s*/?\s*>")
_HORIZONTAL_SPACE = re.compile(r"[ \t\f\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")

# Order matters: URLs before emails/hosts, secrets before generic numbers.
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("url", re.compile(r"(?i)\b(?:https?|ftp|file|smb)://[^\s<>\"'`]+"), "[URL]"),
    ("url", re.compile(r"\\\\[A-Za-z0-9._$-]+(?:\\[^\s\\]+)+"), "[URL]"),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[EMAIL]",
    ),
    (
        "secret",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        "[SECRET]",
    ),
    (
        "secret",
        re.compile(
            r"\b(?:gsk_|sk-|sk_live_|pk_live_|ghp_|gho_|ghs_|github_pat_|glpat-|xox[abprs]-|AKIA|ASIA)"
            r"[A-Za-z0-9_-]{10,}"
        ),
        "[SECRET]",
    ),
    (
        "secret",
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
        r"\1 [SECRET]",
    ),
    (
        "secret",
        re.compile(
            r"(?i)\b(password|passwd|pwd|passcode|secret|client[_-]?secret|token|"
            r"access[_-]?token|refresh[_-]?token|api[_-]?key|access[_-]?key|"
            r"connection[_-]?string|pat)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1\2[SECRET]",
    ),
    (
        "secret",
        re.compile(
            r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])"
            r"[A-Za-z0-9]{32,}={0,2}"
        ),
        "[SECRET]",
    ),
    (
        "id",
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "[ID]",
    ),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"), "[IP]"),
    (
        "host",
        re.compile(
            r"(?i)\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
            r"\.(?:local|lan|corp|internal|intranet|localdomain|ad|int|private)\b"
        ),
        "[HOST]",
    ),
    ("number", re.compile(r"(?<![\w.])\d(?:[ -]?\d){12,18}(?![\w.])"), "[NUMBER]"),
    ("number", re.compile(r"(?<![\w.])\d{7,}(?![\w.])"), "[NUMBER]"),
)


@dataclass
class ScrubResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)


def to_plain_text(value: str) -> str:
    """Convert ADO-style HTML fragments into readable plain text."""
    text = _BLOCK_TAG.sub("\n", value)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def scrub_text(value: str) -> ScrubResult:
    text = to_plain_text(value)
    counts: dict[str, int] = {}
    for category, pattern, replacement in _RULES:
        text, replaced = pattern.subn(replacement, text)
        if replaced:
            counts[category] = counts.get(category, 0) + replaced
    return ScrubResult(text=text, counts=counts)


def merge_counts(*all_counts: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for counts in all_counts:
        for key, value in counts.items():
            merged[key] = merged.get(key, 0) + value
    return merged
