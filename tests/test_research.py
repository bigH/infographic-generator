"""Contract tests for the research stage.

These pin down what :class:`~infographic_generator.research.panda.PandaResearcher`
owes its callers -- the ``Researcher`` port plus the promises a file-backed stub
makes on top of it. Expectations are derived from the uncapped result, never
copied from ``facts.json``, so swapping the data does not break the suite.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import pytest

from infographic_generator.core.models import Brief, ResearchContent, Source
from infographic_generator.core.ports import Researcher
from infographic_generator.research.panda import PANDA_FACTS, PandaResearcher

PROMPT: Final[str] = "a panda"


def panda_brief(max_facts: int | None = None) -> Brief:
    return Brief(prompt=PROMPT, max_facts=max_facts)


async def research(max_facts: int | None = None) -> ResearchContent:
    return await PandaResearcher().research(panda_brief(max_facts))


def fact_sources(content: ResearchContent) -> tuple[Source, ...]:
    return tuple(fact.source for fact in content.facts if fact.source is not None)


def every_source(content: ResearchContent) -> tuple[Source, ...]:
    section_sources = tuple(
        source for section in content.sections for source in section.sources
    )
    return (*content.sources, *fact_sources(content), *section_sources)


def test_default_facts_path_is_absolute_and_readable() -> None:
    assert PANDA_FACTS.is_absolute()
    assert PANDA_FACTS.is_file()


async def test_research_returns_a_fully_populated_document() -> None:
    content = await research()

    assert content.title and content.subtitle and content.summary
    assert len(content.facts) >= 5
    assert len(content.sections) >= 1
    assert 3 <= len(content.keywords) <= 8
    assert all(keyword.strip() for keyword in content.keywords)
    assert content.sources


async def test_every_fact_and_section_carries_its_own_text() -> None:
    content = await research()
    assert content.facts, "an empty facts makes the first clause vacuous"
    assert content.sections, "an empty sections makes the second clause vacuous"

    assert all(fact.label and fact.value for fact in content.facts)
    assert all(section.heading and section.body for section in content.sections)


@pytest.mark.parametrize("max_facts", [None, 0, 1, 2, 5, 9, 10, 11, 50, 10_000])
async def test_max_facts_caps_the_fact_count(max_facts: int | None) -> None:
    available = len((await research()).facts)

    facts = (await research(max_facts)).facts

    expected = available if max_facts is None else min(max_facts, available)
    assert len(facts) == expected


@pytest.mark.parametrize("max_facts", [-1, -10])
async def test_negative_max_facts_is_rejected(max_facts: int) -> None:
    with pytest.raises(ValueError, match="max_facts cannot be negative"):
        await research(max_facts)


@pytest.mark.parametrize("max_facts", range(0, 13))
async def test_capping_keeps_the_most_significant_facts_in_order(
    max_facts: int,
) -> None:
    uncapped = await research()

    capped = await research(max_facts)

    assert tuple(capped.facts) == tuple(uncapped.facts)[:max_facts]


@pytest.mark.parametrize("max_facts", [0, 1, 3, 7])
async def test_capping_facts_does_not_shrink_the_bibliography(max_facts: int) -> None:
    uncapped = await research()

    capped = await research(max_facts)

    assert tuple(capped.sources) == tuple(uncapped.sources)


async def test_fact_sources_are_well_formed_https_urls() -> None:
    sources = fact_sources(await research())
    assert sources, "the stub should attribute at least one fact"

    for source in sources:
        parsed = urlparse(source.url)
        assert parsed.scheme == "https", source.url
        assert parsed.netloc, source.url


async def test_every_fact_source_appears_in_the_bibliography() -> None:
    content = await research()

    bibliography = {source.url for source in content.sources}

    assert {source.url for source in fact_sources(content)} <= bibliography


async def test_retrieval_timestamps_are_timezone_aware_utc() -> None:
    stamped = [
        source.retrieved_at
        for source in every_source(await research())
        if source.retrieved_at is not None
    ]
    assert stamped, "the fixture must fetch something for this to mean anything"

    for retrieved_at in stamped:
        assert retrieved_at.tzinfo is not None
        assert retrieved_at.utcoffset() == timedelta(0)


async def test_the_returned_content_is_frozen() -> None:
    content = await research()

    with pytest.raises(dataclasses.FrozenInstanceError):
        content.title = "other"  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        content.facts[0].value = "other"  # type: ignore[misc]


async def test_sequence_fields_are_tuples() -> None:
    content = await research()
    assert content.sections, "an empty sections makes the last clause vacuous"

    assert isinstance(content.facts, tuple)
    assert isinstance(content.sections, tuple)
    assert isinstance(content.keywords, tuple)
    assert isinstance(content.sources, tuple)
    assert all(isinstance(section.sources, tuple) for section in content.sections)


async def test_panda_researcher_satisfies_the_researcher_port() -> None:
    researcher: Researcher = PandaResearcher()

    content = await researcher.research(panda_brief())

    assert content.title


async def test_research_ignores_the_process_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = await research()
    assert baseline.facts, "baseline must be non-trivial for this test to mean anything"

    monkeypatch.chdir(tmp_path)

    assert await research() == baseline


async def test_research_is_deterministic() -> None:
    assert await research() == await research()


async def test_a_missing_facts_file_raises(tmp_path: Path) -> None:
    absent = tmp_path / "absent.json"

    with pytest.raises(FileNotFoundError):
        await PandaResearcher(absent).research(panda_brief())


async def test_unknown_extras_and_unusual_brief_fields_are_tolerated() -> None:
    brief = Brief(
        prompt=PROMPT,
        audience="curious eight-year-olds",
        locale="ar-EG",
        extras={"research.tone": "playful", "wholly.unknown": "ignore me"},
    )

    content = await PandaResearcher().research(brief)

    assert content.title and content.facts
