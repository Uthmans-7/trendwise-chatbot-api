"""Read the cleaned corpus files produced by the ingestion tooling.

Each page is a Markdown file with JSON-quoted YAML front matter
(``url``, ``path``, ``title``, ``description``, ``crawled_at``).
"""

from __future__ import annotations

import json
import re

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a Markdown file into (front matter dict, body)."""
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(": ")
        fields[key] = json.loads(value)
    return fields, text[match.end():]
