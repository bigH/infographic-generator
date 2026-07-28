"""Stub researcher: loads the panda content from JSON on disk. No AI, no network.

This is a placeholder standing in the ``Researcher`` seam. The real implementation
will run web search and fetch, extract the facts itself, and record each page's
actual URL, publisher and retrieval time rather than reading them off disk.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict, cast

from infographic_generator.core.models import (
    Brief,
    Fact,
    NarrativeSection,
    ResearchContent,
    Source,
)

if TYPE_CHECKING:
    from infographic_generator.core.ports import Researcher


def _panda_asset_dir() -> Path:
    """``assets/panda``: bundled inside the wheel, or at the repo root in a checkout."""
    package_root = Path(__file__).resolve().parents[1]
    repo_root = package_root.parents[1]
    bundled = package_root / "assets" / "panda"
    return bundled if bundled.is_dir() else repo_root / "assets" / "panda"


PANDA_FACTS: Final[Path] = _panda_asset_dir() / "facts.json"


class _SourceEntry(TypedDict):
    title: str
    url: str
    publisher: NotRequired[str]
    accessed: NotRequired[str]


class _FactEntry(TypedDict):
    label: str
    value: str
    detail: NotRequired[str]
    source_title: NotRequired[str]
    source_url: NotRequired[str]


class _SectionEntry(TypedDict):
    heading: str
    body: str


class _Document(TypedDict):
    title: str
    subtitle: str
    summary: str
    keywords: list[str]
    facts: list[_FactEntry]
    sections: list[_SectionEntry]
    sources: list[_SourceEntry]


def _load_document(path: Path) -> _Document:
    """The one boundary where untyped JSON becomes typed data.

    Only the top-level shape is checked here; a missing required key surfaces as a
    ``KeyError`` while mapping, which is the loud failure a broken fixture deserves.
    """
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} is not a JSON object")
    return cast(_Document, parsed)


def _to_utc(iso_date: str) -> datetime:
    """``YYYY-MM-DD`` to midnight UTC; ``Source.retrieved_at`` must be aware."""
    return datetime.fromisoformat(iso_date).replace(tzinfo=UTC)


def _to_source(entry: _SourceEntry) -> Source:
    accessed = entry.get("accessed")
    return Source(
        url=entry["url"],
        title=entry["title"],
        publisher=entry.get("publisher"),
        retrieved_at=_to_utc(accessed) if accessed else None,
    )


def _to_fact_source(
    entry: _FactEntry, bibliography: Mapping[str, _SourceEntry]
) -> Source | None:
    """Attribute a fact only from what it carries; never invent a URL."""
    url = entry.get("source_url")
    if url is None:
        return None
    listed = bibliography.get(url)
    if listed is None:
        return Source(url=url, title=entry.get("source_title"))
    enriched = _to_source(listed)
    return replace(enriched, title=entry.get("source_title") or enriched.title)


def _to_fact(entry: _FactEntry, bibliography: Mapping[str, _SourceEntry]) -> Fact:
    """``unit`` stays ``None``: the values arrive pre-formatted, units included."""
    return Fact(
        label=entry["label"],
        value=entry["value"],
        detail=entry.get("detail"),
        source=_to_fact_source(entry, bibliography),
    )


def _to_section(entry: _SectionEntry) -> NarrativeSection:
    """No per-section citations in the stub data; the bibliography covers them."""
    return NarrativeSection(heading=entry["heading"], body=entry["body"])


def _capped(facts: Sequence[_FactEntry], limit: int | None) -> Sequence[_FactEntry]:
    """JSON order is significance order, so the cap is a prefix."""
    return facts if limit is None else facts[:limit]


class PandaResearcher:
    """Fixture-backed ``Researcher``: the same panda content on every call."""

    def __init__(self, facts_path: Path = PANDA_FACTS) -> None:
        self._facts_path = facts_path

    async def research(self, brief: Brief) -> ResearchContent:
        """Return the panda content, keeping at most ``brief.max_facts`` facts.

        ``brief.prompt`` is ignored -- this stub always returns pandas. Capping
        facts never shortens ``sources``: that is the document's full bibliography.

        Raises ``ValueError`` for a negative ``max_facts``; a missing or malformed
        facts file surfaces as ``OSError`` or ``json.JSONDecodeError``.
        """
        if brief.max_facts is not None and brief.max_facts < 0:
            raise ValueError(f"max_facts cannot be negative: {brief.max_facts}")

        # TODO: replace with the AI researcher -- everything below is fixture reading.
        document = _load_document(self._facts_path)
        bibliography = {entry["url"]: entry for entry in document["sources"]}
        return ResearchContent(
            title=document["title"],
            subtitle=document["subtitle"],
            summary=document["summary"],
            facts=tuple(
                _to_fact(entry, bibliography)
                for entry in _capped(document["facts"], brief.max_facts)
            ),
            sections=tuple(map(_to_section, document["sections"])),
            keywords=tuple(document["keywords"]),
            sources=tuple(map(_to_source, document["sources"])),
        )


if TYPE_CHECKING:
    _conforms: Researcher = PandaResearcher()
