"""Leak, fake-data and raw-dump patterns used by the output guard.

Every pattern is a case-insensitive Python regex; a match in a bot answer
blocks it. Exemption: a pattern that also matches the visitor's messages is
ignored, so the bot may echo a name the visitor used (e.g. "I have no
information about Velmora") without being blocked.
"""

import re

from app.settings import LeakSettings

# Planted in the system prompt; seeing it in an answer proves a prompt leak
# regardless of paraphrasing elsewhere.
SYSTEM_PROMPT_CANARY = "TW-CANARY-7F3A"

# Project-specific values (internal ports, storage / file names) come from
# .env (LEAK_INTERNAL_PORTS, LEAK_INTERNAL_NAMES) so they never sit in git.
_LEAK = LeakSettings()
_PORTS = _LEAK.ports()
_NAMES = [re.escape(n) + r"[\w-]*" for n in _LEAK.names()]

INFRA_LEAK_PATTERNS: dict[str, str] = {
    "private_ip": (
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
        r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    ),
    "localhost": r"\blocalhost\b|\b127\.0\.0\.1\b",
    **({"internal_port": rf":(?:{'|'.join(_PORTS)})\b"} if _PORTS else {}),
    "bucket_or_file": "|".join([*_NAMES, r"\.mp4\b"]),
    "api_endpoint": r"/api/[\w-]+",
    "dev_path": r"(?<![\w.])/dev/",
    "build_state": r"\bturbopack\b|\bdev[- ]mode\b|\bnext\.?js\b",
    "canary": re.escape(SYSTEM_PROMPT_CANARY),
}

# Fictional data from the homepage preview widgets; must never be in the
# corpus or be presented as real.
FAKE_DATA_PATTERNS: dict[str, str] = {
    "fake_brand": r"\bvelmora\b",
    "example_domain": r"[\w@-]\.example\b",
}

# Extraction artefacts that only appear if raw chunks are dumped verbatim.
RAW_DUMP_PATTERNS: dict[str, str] = {
    "alt_view_marker": r"Alternate view:",
    "front_matter": r"^\s*(?:crawled_at|url):\s*\"",
}

GLOBAL_MUST_NOT: dict[str, str] = {
    **INFRA_LEAK_PATTERNS,
    **FAKE_DATA_PATTERNS,
    **RAW_DUMP_PATTERNS,
}


def find_violations(
    text: str, patterns: dict[str, str], question: str = ""
) -> list[str]:
    """Return names of patterns matching ``text`` but not ``question``."""
    flags = re.IGNORECASE | re.MULTILINE
    return [
        name
        for name, pattern in patterns.items()
        if re.search(pattern, text, flags)
        and not (question and re.search(pattern, question, flags))
    ]
