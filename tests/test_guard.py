"""Output guard and corpus loader tests (pure functions, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.chat.guard import check_answer
from app.chat.patterns import SYSTEM_PROMPT_CANARY
from app.chat.prompt import Page, load_pages
from app.settings import AppSettings, LeakSettings
from tests.stub_client import FAKE_PRIVATE_IP

PAGES = [
    Page(url="https://trendwise.io/pricing", path="/pricing", title="Pricing",
         body="Starter $299/month. Professional $399/month. Annual: save 10%."),
    Page(url="https://trendwise.io/about", path="/about", title="About",
         body="Trendwise monitors 6 platforms."),
]
FALLBACK = "fallback"


def guard(answer: str, visitor: str = "") -> tuple[bool, list[str]]:
    """Return (blocked, violations) for an answer."""
    result = check_answer(answer, PAGES, visitor, FALLBACK)
    assert result.text == (FALLBACK if result.blocked else answer)
    return result.blocked, result.violations


def test_clean_answer_passes_with_cited_source() -> None:
    """Stated prices with a whitelisted source pass unchanged."""
    result = check_answer(
        "Starter is $299/month.\nSources: https://trendwise.io/pricing",
        PAGES, "", FALLBACK)
    assert not result.blocked
    assert result.cited_urls == ["https://trendwise.io/pricing"]


@pytest.mark.parametrize(("answer", "violation"), [
    (f"See http://{FAKE_PRIVATE_IP}/x", "pattern:private_ip"),
    ("Running on localhost.", "pattern:localhost"),
    ("Data comes from /api/leads", "pattern:api_endpoint"),
    ("The site is in dev mode.", "pattern:build_state"),
    ("Watch intro.mp4", "pattern:bucket_or_file"),
    (f"Code {SYSTEM_PROMPT_CANARY}", "pattern:canary"),
    ("Velmora is a customer.", "pattern:fake_brand"),
    ("Alternate view: Annual", "pattern:alt_view_marker"),
])
def test_leak_patterns_block(answer: str, violation: str) -> None:
    """Infra, canary, fake-data and raw-dump markers are blocked."""
    blocked, violations = guard(answer)
    assert blocked
    assert violation in violations


def test_echo_exemption_for_visitor_terms() -> None:
    """A name the visitor used may be echoed back."""
    blocked, _ = guard("I have no information about Velmora.",
                       visitor="Is Velmora a customer?")
    assert not blocked


def test_unknown_url_is_blocked() -> None:
    """Only corpus page URLs may appear."""
    blocked, violations = guard("See https://competitor.com/pricing")
    assert blocked
    assert violations == ["url:https://competitor.com/pricing"]


@pytest.mark.parametrize("answer", [
    "Professional yearly is $4788.",       # 399 x 12
    "Starter is $100 less than Professional.",  # 399 - 299
    "Annual billing saves 10%.",
])
def test_derived_numbers_pass(answer: str) -> None:
    """Simple arithmetic on stated prices is allowed."""
    assert not guard(answer)[0]


@pytest.mark.parametrize(("answer", "violation"), [
    ("Enterprise is $999/month.", "number:$999"),
    ("Save 25% with a coupon.", "number:25%"),
    ("There is a 14-day free trial.", "number:14 (duration)"),
])
def test_invented_numbers_are_blocked(answer: str, violation: str) -> None:
    """Prices, percentages and durations not in the corpus are blocked."""
    blocked, violations = guard(answer)
    assert blocked
    assert violation in violations


def test_numbers_must_come_from_cited_page() -> None:
    """With a citation, numbers are checked against the cited page only."""
    blocked, _ = guard("Starter is $299/month.\n"
                       "Sources: https://trendwise.io/about")
    assert blocked


def test_leak_settings_parse_lists() -> None:
    """LEAK_* values are comma-separated; non-numeric ports are ignored."""
    leak = LeakSettings(_env_file=None, internal_ports=" 1234, x ,5678",
                        internal_names="bucket-a, ,file-b")
    assert leak.ports() == ["1234", "5678"]
    assert leak.names() == ["bucket-a", "file-b"]


def test_default_corpus_dir_is_repo_corpus() -> None:
    """Without CORPUS_DIR, the committed corpus/ (8 public pages) loads."""
    corpus_dir = AppSettings.model_fields["corpus_dir"].default
    assert corpus_dir == Path(__file__).resolve().parent.parent / "corpus"
    pages = load_pages(corpus_dir)
    assert len(pages) == 8
    assert all(p.url.startswith("https://trendwise.io") for p in pages)


def test_load_pages_reads_corpus_format(tmp_path: Path) -> None:
    """Corpus files: JSON-quoted front matter + Markdown body, sorted by path."""
    for slug, path in (("pricing", "/pricing"), ("home", "/")):
        meta = {"url": f"https://trendwise.io{path}", "path": path,
                "title": slug.title(), "description": "",
                "crawled_at": "2026-11-01T00:00:00+00:00"}
        head = "\n".join(f"{k}: {json.dumps(v)}" for k, v in meta.items())
        (tmp_path / f"{slug}.md").write_text(
            f"---\n{head}\n---\n\n# {slug}\n", encoding="utf-8")
    pages = load_pages(tmp_path)
    assert [p.path for p in pages] == ["/", "/pricing"]
    assert pages[1] == Page("https://trendwise.io/pricing", "/pricing",
                            "Pricing", "# pricing")
