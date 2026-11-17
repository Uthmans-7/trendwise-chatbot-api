"""Deterministic output guard applied to every model answer.

Checks, in order:
1. shared leak / fake-data / raw-dump patterns (``app.chat.patterns``);
2. URL whitelist: every URL must be a public page URL from the corpus;
3. numbers: every $ amount, percentage and "N day/week/month/year" in the
   answer must appear in the cited pages (or the whole corpus if nothing is
   cited) or be simple arithmetic on stated $ amounts (x12, /12, a-b, a+b,
   10% off).

If anything fails, the answer is replaced with a safe fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product

from app.chat.patterns import GLOBAL_MUST_NOT, find_violations
from app.chat.prompt import Page

URL = re.compile(r"https?://[^\s<>()\"']+|\bwww\.[^\s<>()\"']+", re.IGNORECASE)
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
DURATION = re.compile(r"\b(\d+)[- ]?(?:day|week|month|year)s?\b", re.IGNORECASE)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class GuardResult:
    """Outcome of the output guard for one answer."""

    text: str
    violations: list[str] = field(default_factory=list)
    cited_urls: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True if the raw answer was replaced."""
        return bool(self.violations)


def _num(text: str) -> float:
    return round(float(text.replace(",", "")), 2)


def _allowed_numbers(source_text: str) -> tuple[set[float], set[float]]:
    """Return (allowed money values, allowed plain numbers) for a text."""
    plain = {_num(n) for n in NUMBER.findall(source_text)}
    money = {_num(m) for m in MONEY.findall(source_text)}
    base = money | {round(m * 12, 2) for m in money} | {
        round(m / 12, 2) for m in money
    }
    derived = set(base)
    for a, b in product(base, repeat=2):
        derived.update({round(abs(a - b), 2), round(a + b, 2)})
    derived.update(round(m * 0.9, 2) for m in base)
    derived.update(round(m * 0.1, 2) for m in base)
    return derived, plain | {12.0}


def _strip_url(url: str) -> str:
    return url.rstrip(".,;:!?)]").rstrip("/")


def check_answer(
    answer: str,
    pages: list[Page],
    context_text: str = "",
    fallback: str = "",
) -> GuardResult:
    """Run all guard checks on ``answer``.

    ``context_text`` holds the visitor's messages, used for the echo
    exemption on leak patterns and to allow numbers the visitor supplied.
    """
    violations = [f"pattern:{name}" for name in
                  find_violations(answer, GLOBAL_MUST_NOT, context_text)]

    by_url = {p.url.rstrip("/"): p for p in pages}
    urls = [_strip_url(u) for u in URL.findall(answer)]
    cited = [u for u in urls if u in by_url]
    violations += [f"url:{u}" for u in urls if u not in by_url]

    cited_pages = [by_url[u] for u in dict.fromkeys(cited)] or pages
    money_ok, plain_ok = _allowed_numbers(
        "\n".join(p.body for p in cited_pages)
    )
    user_numbers = {_num(n) for n in NUMBER.findall(context_text)}
    answer_no_urls = URL.sub(" ", answer)
    for value in MONEY.findall(answer_no_urls):
        if _num(value) not in money_ok | user_numbers:
            violations.append(f"number:${value}")
    for value in PERCENT.findall(answer_no_urls):
        if _num(value) not in plain_ok | user_numbers:
            violations.append(f"number:{value}%")
    for value in DURATION.findall(answer_no_urls):
        if _num(value) not in plain_ok | user_numbers:
            violations.append(f"number:{value} (duration)")

    return GuardResult(
        text=fallback if violations else answer,
        violations=violations,
        cited_urls=list(dict.fromkeys(cited)),
    )
