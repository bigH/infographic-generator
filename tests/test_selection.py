"""Registry shape and selection behaviour. Everything here is pure -- no mocks."""

from __future__ import annotations

import dataclasses
import itertools
import math
import re
from collections.abc import Iterator, Sequence
from types import MappingProxyType

import pytest

from infographic_generator.composition.composer import TEMPLATE_DIR, build_environment
from infographic_generator.composition.layout import _BUILDERS
from infographic_generator.composition.registry import (
    RENDERABLE_TEMPLATE_IDS,
    TEMPLATE_REGISTRY,
    _SPECS,
    TemplateSpec,
)
from infographic_generator.composition.selection import (
    LEARNING_PREFERENCE_EXTRA_KEY,
    MIN_CONFIDENCE,
    TEMPLATE_EXTRA_KEY,
    ContentCensus,
    LearningPreference,
    TemplateChoice,
    TemplateSelector,
    build_census,
    choose_template,
    is_renderable,
    learning_preference_of,
    resolve_choice,
    template_override_of,
)
from infographic_generator.core.models import (
    Brief,
    Fact,
    ImageAsset,
    ImageCredit,
    ImageRole,
    NarrativeSection,
    RenderOptions,
    ResearchContent,
)

EXPECTED_IDS = (
    "stat_grid",
    "timeline",
    "comparison",
    "process_flow",
    "quote_spotlight",
    "ranked_list",
)
BLOCKED_IDS = frozenset({"timeline", "comparison", "quote_spotlight"})
RENDERABLE_IDS = frozenset({"stat_grid", "process_flow", "ranked_list"})

CORE_NAME = re.compile(r"`([A-Z][A-Za-z0-9]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")
"""A backtick-quoted core type or attribute -- ``Quote``, ``Fact.when``. Anchored
on the opening backtick only, because the real notes quote an annotation too
(```Fact.when: str | None```)."""


def census(
    *,
    fact_count: int = 0,
    section_count: int = 0,
    image_count: int = 0,
    has_hero: bool = False,
    preference: LearningPreference = LearningPreference.BALANCED,
    aspect: float | None = None,
) -> ContentCensus:
    return ContentCensus(
        fact_count=fact_count,
        section_count=section_count,
        image_count=image_count,
        has_hero=has_hero,
        audience=None,
        locale="en-US",
        aspect=aspect,
        title="Title",
        subtitle="Subtitle",
        summary="Summary",
        learning_preference=preference,
    )


def brief_with(**extras: str) -> Brief:
    return Brief(prompt="a panda", extras=extras)


def image(role: ImageRole = ImageRole.SUPPORTING) -> ImageAsset:
    return ImageAsset(
        content=b"\x89PNG not really",
        mime_type="image/png",
        width_px=800,
        height_px=600,
        alt_text="a panda",
        credit=ImageCredit(license="CC0-1.0"),
        role=role,
    )


def sweep() -> Iterator[ContentCensus]:
    """Every census shape the rule table could plausibly meet."""
    for facts, sections, images, hero, preference, aspect in itertools.product(
        range(13),
        range(9),
        range(7),
        (False, True),
        tuple(LearningPreference),
        (None, 0.75, 1.5),
    ):
        yield census(
            fact_count=facts,
            section_count=sections,
            image_count=images,
            has_hero=hero,
            preference=preference,
            aspect=aspect,
        )


# --- registry -------------------------------------------------------------


def test_registry_has_exactly_the_six_expected_ids_in_order() -> None:
    assert tuple(TEMPLATE_REGISTRY) == EXPECTED_IDS


def test_registry_is_keyed_by_spec_id() -> None:
    assert all(key == spec.id for key, spec in TEMPLATE_REGISTRY.items())


def test_every_selection_hint_is_non_empty() -> None:
    assert all(spec.selection_hint.strip() for spec in TEMPLATE_REGISTRY.values())


def test_every_template_name_is_a_jinja_template() -> None:
    assert all(
        spec.template_name.endswith(".html.j2") for spec in TEMPLATE_REGISTRY.values()
    )


def test_every_display_name_is_non_empty() -> None:
    assert all(spec.display_name.strip() for spec in TEMPLATE_REGISTRY.values())


@pytest.mark.parametrize("template_id", sorted(BLOCKED_IDS))
def test_a_blocked_template_names_the_core_field_it_waits_on(template_id: str) -> None:
    blocked_on = TEMPLATE_REGISTRY[template_id].blocked_on
    assert blocked_on is not None and blocked_on.strip()
    assert CORE_NAME.search(blocked_on), f"{template_id} names no core type: {blocked_on}"
    assert template_id not in RENDERABLE_TEMPLATE_IDS


@pytest.mark.parametrize("template_id", sorted(RENDERABLE_IDS))
def test_renderable_entries_are_not_blocked(template_id: str) -> None:
    assert TEMPLATE_REGISTRY[template_id].blocked_on is None


def test_renderable_ids_are_exactly_the_three_unblocked() -> None:
    assert RENDERABLE_TEMPLATE_IDS == RENDERABLE_IDS


def test_image_roles_come_from_the_core_enum() -> None:
    roles = [role for spec in TEMPLATE_REGISTRY.values() for role in spec.image_roles]
    assert roles and all(isinstance(role, ImageRole) for role in roles)


def test_icon_and_background_roles_are_claimed_by_the_new_layouts() -> None:
    assert ImageRole.ICON in TEMPLATE_REGISTRY["process_flow"].image_roles
    assert ImageRole.BACKGROUND in TEMPLATE_REGISTRY["quote_spotlight"].image_roles


def test_the_registry_is_unique_and_immutable() -> None:
    ids = [spec.id for spec in _SPECS]
    assert len(ids) == len(set(ids)), f"duplicate ids in _SPECS: {sorted(ids)}"
    assert isinstance(TEMPLATE_REGISTRY, MappingProxyType)
    with pytest.raises(TypeError):
        TEMPLATE_REGISTRY["stat_grid"] = TEMPLATE_REGISTRY["ranked_list"]  # type: ignore[index]


@pytest.mark.parametrize("template_id", sorted(RENDERABLE_IDS))
def test_every_renderable_template_has_a_template_file_that_parses(
    template_id: str,
) -> None:
    """``blocked_on`` is all that stands between the registry and a runtime
    ``TemplateNotFound``; this is the check that makes it a guarantee."""
    name = TEMPLATE_REGISTRY[template_id].template_name
    assert (TEMPLATE_DIR / name).is_file(), f"{template_id} points at a missing {name}"
    build_environment().get_template(name)


def test_the_builder_table_covers_exactly_the_renderable_templates() -> None:
    builders, renderable = set(_BUILDERS), set(RENDERABLE_TEMPLATE_IDS)
    assert builders == renderable, (
        f"renderable with no builder: {sorted(renderable - builders)}; "
        f"builders for nothing renderable: {sorted(builders - renderable)}"
    )


def test_template_spec_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        TEMPLATE_REGISTRY["stat_grid"].id = "nope"  # type: ignore[misc]


def test_content_census_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        census().fact_count = 3  # type: ignore[misc]


def test_template_choice_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        TemplateChoice("stat_grid", 1.0, "why").confidence = 0.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "cls", [TemplateSpec, ContentCensus, TemplateChoice], ids=lambda c: c.__name__
)
def test_dataclasses_are_slotted(cls: type) -> None:
    assert not hasattr(cls, "__dict__") or "__slots__" in cls.__dict__


# --- the sweep: choose_template can never name a blocked template ---------


def test_choose_template_never_returns_a_blocked_or_unknown_id() -> None:
    for candidate in sweep():
        choice = choose_template(candidate)
        assert choice.template_id in RENDERABLE_TEMPLATE_IDS, candidate
        assert choice.template_id in TEMPLATE_REGISTRY
        assert TEMPLATE_REGISTRY[choice.template_id].blocked_on is None
        assert 0.0 <= choice.confidence <= 1.0, candidate
        assert choice.rationale.strip()


def test_choose_template_is_deterministic() -> None:
    for candidate in sweep():
        assert choose_template(candidate) == choose_template(candidate)


# --- individual rules -----------------------------------------------------


def test_many_facts_and_few_sections_picks_ranked_list() -> None:
    choice = choose_template(census(fact_count=7, section_count=1))
    assert choice.template_id == "ranked_list"
    assert choice.confidence > MIN_CONFIDENCE


def test_section_dominant_content_picks_process_flow() -> None:
    choice = choose_template(census(fact_count=1, section_count=4))
    assert choice.template_id == "process_flow"
    assert choice.confidence > MIN_CONFIDENCE


def test_small_content_picks_stat_grid() -> None:
    assert choose_template(census(fact_count=3, section_count=1)).template_id == (
        "stat_grid"
    )


def test_empty_census_falls_back_to_stat_grid_with_low_confidence() -> None:
    choice = choose_template(census())
    assert choice.template_id == "stat_grid"
    assert choice.confidence <= MIN_CONFIDENCE


# --- learning preference is a tiebreaker, never a driver ------------------


@pytest.mark.parametrize(
    ("fact_count", "section_count", "expected"),
    [(9, 0, "ranked_list"), (1, 5, "process_flow")],
)
def test_preference_never_overrides_an_unambiguous_shape(
    fact_count: int, section_count: int, expected: str
) -> None:
    ids = {
        choose_template(
            census(
                fact_count=fact_count,
                section_count=section_count,
                image_count=4,
                has_hero=True,
                preference=preference,
            )
        ).template_id
        for preference in LearningPreference
    }
    assert ids == {expected}


def test_text_heavy_tips_an_ambiguous_census_to_process_flow() -> None:
    ambiguous = census(fact_count=3, section_count=2)
    assert choose_template(ambiguous).template_id == "stat_grid"
    tipped = choose_template(
        census(
            fact_count=3, section_count=2, preference=LearningPreference.TEXT_HEAVY
        )
    )
    assert tipped.template_id == "process_flow"


def test_image_heavy_keeps_the_hero_grid_but_more_confidently() -> None:
    plain = choose_template(census(fact_count=2, image_count=3))
    nudged = choose_template(
        census(
            fact_count=2, image_count=3, preference=LearningPreference.IMAGE_HEAVY
        )
    )
    assert plain.template_id == nudged.template_id == "stat_grid"
    assert nudged.confidence > plain.confidence


def test_preference_alone_cannot_pick_process_flow_without_sections() -> None:
    choice = choose_template(
        census(fact_count=2, section_count=0, preference=LearningPreference.TEXT_HEAVY)
    )
    assert choice.template_id == "stat_grid"


# --- reading the extras bag ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text_heavy", LearningPreference.TEXT_HEAVY),
        ("TEXT_HEAVY", LearningPreference.TEXT_HEAVY),
        (" balanced ", LearningPreference.BALANCED),
        ("  IMAGE_HEAVY  ", LearningPreference.IMAGE_HEAVY),
        ("", LearningPreference.BALANCED),
        ("   ", LearningPreference.BALANCED),
        ("nonsense", LearningPreference.BALANCED),
        ("image heavy", LearningPreference.BALANCED),
    ],
)
def test_learning_preference_of_degrades_to_balanced(
    raw: str, expected: LearningPreference
) -> None:
    brief = brief_with(**{LEARNING_PREFERENCE_EXTRA_KEY: raw})
    assert learning_preference_of(brief) is expected


def test_learning_preference_of_absent_key() -> None:
    assert learning_preference_of(Brief(prompt="a panda")) is LearningPreference.BALANCED


def test_template_override_accepts_a_renderable_id() -> None:
    brief = brief_with(**{TEMPLATE_EXTRA_KEY: " ranked_list "})
    assert template_override_of(brief) == "ranked_list"


@pytest.mark.parametrize("raw", ["", "   ", "nope", "timeline", "comparison", "STAT_GRID"])
def test_template_override_ignores_unusable_ids(raw: str) -> None:
    assert template_override_of(brief_with(**{TEMPLATE_EXTRA_KEY: raw})) is None


def test_template_override_of_absent_key() -> None:
    assert template_override_of(Brief(prompt="a panda")) is None


@pytest.mark.parametrize(
    ("template_id", "expected"),
    [
        ("stat_grid", True),
        ("process_flow", True),
        ("ranked_list", True),
        ("timeline", False),
        ("comparison", False),
        ("quote_spotlight", False),
        ("", False),
        ("made_up", False),
    ],
)
def test_is_renderable(template_id: str, expected: bool) -> None:
    assert is_renderable(template_id) is expected


# --- resolve_choice: the full fallback chain -----------------------------


def test_override_beats_a_high_confidence_choice() -> None:
    brief = brief_with(**{TEMPLATE_EXTRA_KEY: "process_flow"})
    choice = TemplateChoice("ranked_list", 1.0, "very sure")
    spec = resolve_choice(brief, census(fact_count=9), choice)
    assert spec.id == "process_flow"


def test_blocked_override_is_ignored_and_does_not_raise() -> None:
    brief = brief_with(**{TEMPLATE_EXTRA_KEY: "timeline"})
    spec = resolve_choice(brief, census(fact_count=8, section_count=1), None)
    assert spec.id == "ranked_list"


def test_unknown_override_is_ignored() -> None:
    brief = brief_with(**{TEMPLATE_EXTRA_KEY: "wat"})
    assert resolve_choice(brief, census(), None).id == "stat_grid"


def test_trusted_choice_is_honoured() -> None:
    choice = TemplateChoice("process_flow", MIN_CONFIDENCE, "just enough")
    spec = resolve_choice(Brief(prompt="a panda"), census(fact_count=8), choice)
    assert spec.id == "process_flow"


def test_choice_below_min_confidence_falls_to_the_rule_table() -> None:
    choice = TemplateChoice("process_flow", MIN_CONFIDENCE - 0.01, "hedging")
    spec = resolve_choice(
        Brief(prompt="a panda"), census(fact_count=8, section_count=0), choice
    )
    assert spec.id == "ranked_list"


def test_choice_naming_a_blocked_template_falls_to_the_rule_table() -> None:
    choice = TemplateChoice("quote_spotlight", 1.0, "confidently wrong")
    spec = resolve_choice(Brief(prompt="a panda"), census(section_count=5), choice)
    assert spec.id == "process_flow"


def test_none_choice_falls_to_the_rule_table() -> None:
    spec = resolve_choice(Brief(prompt="a panda"), census(section_count=4), None)
    assert spec.id == "process_flow"


def test_resolve_choice_returns_a_spec_not_an_id() -> None:
    spec = resolve_choice(Brief(prompt="a panda"), census(), None)
    assert isinstance(spec, TemplateSpec)


def test_resolve_choice_survives_adversarial_choices() -> None:
    adversarial: Sequence[TemplateChoice | None] = (
        None,
        TemplateChoice("timeline", 1.0, "blocked but sure"),
        TemplateChoice("comparison", 0.0, "blocked and unsure"),
        TemplateChoice("quote_spotlight", math.nan, "blocked and nan"),
        TemplateChoice("not_a_template", 1.0, "unregistered"),
        TemplateChoice("", 1.0, "empty id"),
        TemplateChoice("stat_grid", math.nan, "nan confidence"),
        TemplateChoice("ranked_list", -5.0, "negative"),
        TemplateChoice("process_flow", 42.0, "over-confident"),
    )
    briefs = (
        Brief(prompt="a panda"),
        brief_with(**{TEMPLATE_EXTRA_KEY: "timeline"}),
        brief_with(**{TEMPLATE_EXTRA_KEY: "garbage"}),
        brief_with(**{TEMPLATE_EXTRA_KEY: "stat_grid"}),
    )
    for brief, choice in itertools.product(briefs, adversarial):
        for candidate in (census(), census(fact_count=9), census(section_count=5)):
            spec = resolve_choice(brief, candidate, choice)
            assert spec.blocked_on is None
            assert spec.id in RENDERABLE_TEMPLATE_IDS


# --- the Protocol is satisfiable ----------------------------------------


class CannedSelector:
    """A ``TemplateSelector`` with no brain -- proves the Protocol is inhabitable."""

    def __init__(self, choice: TemplateChoice) -> None:
        self._choice = choice

    async def select(self, census: ContentCensus) -> TemplateChoice:
        return self._choice


async def test_canned_selector_satisfies_the_protocol() -> None:
    selector: TemplateSelector = CannedSelector(
        TemplateChoice("ranked_list", 0.9, "canned")
    )
    choice = await selector.select(census(fact_count=6))
    spec = resolve_choice(Brief(prompt="a panda"), census(fact_count=6), choice)
    assert spec.id == "ranked_list"


async def test_canned_selector_answer_is_still_vetted() -> None:
    selector: TemplateSelector = CannedSelector(
        TemplateChoice("timeline", 0.99, "canned but blocked")
    )
    choice = await selector.select(census())
    assert resolve_choice(Brief(prompt="a panda"), census(), choice).id == "stat_grid"


# --- build_census -------------------------------------------------------


def content_with(facts: int, sections: int) -> ResearchContent:
    return ResearchContent(
        title="Pandas",
        subtitle="A specimen sheet",
        summary="They eat bamboo.",
        facts=tuple(Fact(label=f"f{i}", value=str(i)) for i in range(facts)),
        sections=tuple(
            NarrativeSection(heading=f"s{i}", body="body") for i in range(sections)
        ),
    )


def test_build_census_counts_and_flags() -> None:
    brief = Brief(prompt="a panda", audience="kids", locale="fr-FR")
    images = (image(ImageRole.SUPPORTING), image(ImageRole.HERO))
    result = build_census(brief, content_with(4, 2), images)
    assert result.fact_count == 4
    assert result.section_count == 2
    assert result.image_count == 2
    assert result.has_hero is True
    assert result.audience == "kids"
    assert result.locale == "fr-FR"
    assert result.title == "Pandas"
    assert result.subtitle == "A specimen sheet"
    assert result.summary == "They eat bamboo."


def test_build_census_without_a_hero() -> None:
    result = build_census(
        Brief(prompt="a panda"), content_with(0, 0), (image(ImageRole.ICON),)
    )
    assert result.has_hero is False
    assert result.image_count == 1


def test_build_census_aspect_is_none_for_a_full_page_brief() -> None:
    brief = Brief(prompt="a panda", options=RenderOptions(width_px=1200))
    assert build_census(brief, content_with(1, 1), ()).aspect is None


def test_build_census_aspect_is_a_ratio_for_a_fixed_height_brief() -> None:
    brief = Brief(prompt="a panda", options=RenderOptions(width_px=1200, height_px=800))
    assert build_census(brief, content_with(1, 1), ()).aspect == pytest.approx(1.5)


NO_AREA_HEIGHTS = (0, -1, -5, -1200)
"""Heights that reach ``build_census`` unchecked today: ``RenderOptions`` validates
nothing and ``argparse`` takes ``--height 0`` and ``--height -5`` at face value."""

NO_AREA_WIDTHS = (0, -1, -1200)
"""The same hole one flag over -- ``--width 0`` is equally unvalidated."""


def assert_well_formed(result: ContentCensus) -> None:
    """What every downstream reader of a census is entitled to assume of it.

    ``aspect`` is the only field that is *computed*, so it is the only one that can
    be quietly wrong; the rule table never reads it, which is precisely why a
    nonsense value would travel as far as a model's prompt before anyone noticed.
    """
    assert result.aspect is None or (
        math.isfinite(result.aspect) and result.aspect > 0.0
    ), f"aspect is not a usable ratio: {result.aspect!r}"
    assert min(result.fact_count, result.section_count, result.image_count) >= 0
    assert result.learning_preference in tuple(LearningPreference)
    assert choose_template(result).template_id in RENDERABLE_TEMPLATE_IDS


@pytest.mark.parametrize("height_px", NO_AREA_HEIGHTS)
def test_build_census_reads_a_height_with_no_area_as_full_page(height_px: int) -> None:
    """``--height 0`` was a ``ZeroDivisionError`` and ``--height -5`` a cheerful
    ``-240.0``; neither is a box, so both mean what ``None`` means."""
    brief = Brief(
        prompt="a panda", options=RenderOptions(width_px=1200, height_px=height_px)
    )
    result = build_census(brief, content_with(3, 1), (image(ImageRole.HERO),))
    assert result.aspect is None
    assert (result.fact_count, result.section_count, result.image_count) == (3, 1, 1)
    assert result.has_hero is True
    assert_well_formed(result)


@pytest.mark.parametrize("width_px", NO_AREA_WIDTHS)
def test_build_census_reads_a_width_with_no_area_as_full_page(width_px: int) -> None:
    brief = Brief(
        prompt="a panda", options=RenderOptions(width_px=width_px, height_px=800)
    )
    result = build_census(brief, content_with(1, 1), ())
    assert result.aspect is None
    assert_well_formed(result)


def test_a_degenerate_page_size_still_resolves_to_a_renderable_spec() -> None:
    """The whole point: geometry nobody validated must not cost the render."""
    for width_px, height_px in itertools.product((0, -1, 1200), (0, -5, None, 800)):
        brief = Brief(
            prompt="a panda",
            options=RenderOptions(width_px=width_px, height_px=height_px),
        )
        result = build_census(brief, content_with(8, 1), ())
        assert_well_formed(result)
        spec = resolve_choice(brief, result, None)
        assert spec.id in RENDERABLE_TEMPLATE_IDS and spec.blocked_on is None


def test_build_census_reads_the_learning_preference_from_extras() -> None:
    brief = brief_with(**{LEARNING_PREFERENCE_EXTRA_KEY: "IMAGE_HEAVY"})
    result = build_census(brief, content_with(1, 1), ())
    assert result.learning_preference is LearningPreference.IMAGE_HEAVY


def test_build_census_with_no_images_or_content() -> None:
    result = build_census(
        Brief(prompt="a panda"), ResearchContent(title="t", subtitle="s", summary=""), ()
    )
    assert (result.fact_count, result.section_count, result.image_count) == (0, 0, 0)
    assert choose_template(result).template_id == "stat_grid"
