"""Pipeline wiring and the CLI around it.

No stage implementations and no browser: the four ports are satisfied
structurally by recording fakes, so what is under test is the order of the
calls, the objects handed between them, and the process exit code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from infographic_generator.cli import build_parser, main, parse_args
from infographic_generator.core.models import (
    Brief,
    Composition,
    ImageAsset,
    ImageCredit,
    RenderResult,
    ResearchContent,
    Theme,
)
from infographic_generator.pipeline import Pipeline

CONTENT = ResearchContent(title="t", subtitle="s", summary="m")
IMAGES: tuple[ImageAsset, ...] = (
    ImageAsset(
        content=b"\x89PNG\r\n\x1a\n",
        mime_type="image/png",
        width_px=1,
        height_px=1,
        alt_text="a pixel",
        credit=ImageCredit(license="CC0-1.0"),
    ),
)
COMPOSITION = Composition(html="<main></main>", width_px=1200)
RESULT = RenderResult(
    output_path=Path("unused.png"), width_px=1234, height_px=5678, bytes_written=99
)


@dataclass(slots=True)
class FakeResearcher:
    log: list[str]
    briefs: list[Brief] = field(default_factory=list)

    async def research(self, brief: Brief) -> ResearchContent:
        self.log.append("research")
        self.briefs.append(brief)
        return CONTENT


@dataclass(slots=True)
class FakeImageSourcer:
    log: list[str]
    calls: list[tuple[Brief, ResearchContent]] = field(default_factory=list)

    async def source_images(
        self, brief: Brief, content: ResearchContent
    ) -> Sequence[ImageAsset]:
        self.log.append("source_images")
        self.calls.append((brief, content))
        return IMAGES


@dataclass(slots=True)
class FakeComposer:
    log: list[str]
    error: Exception | None = None
    calls: list[tuple[Brief, ResearchContent, Sequence[ImageAsset]]] = field(
        default_factory=list
    )

    async def compose(
        self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
    ) -> Composition:
        self.log.append("compose")
        self.calls.append((brief, content, images))
        if self.error is not None:
            raise self.error
        return COMPOSITION


@dataclass(slots=True)
class FakeRenderer:
    log: list[str]
    result: RenderResult = RESULT
    calls: list[tuple[Composition, Path]] = field(default_factory=list)

    async def render(
        self, composition: Composition, output_path: Path
    ) -> RenderResult:
        self.log.append("render")
        self.calls.append((composition, output_path))
        return self.result


@dataclass(frozen=True, slots=True)
class Fakes:
    """One recording implementation of each port, sharing a single call log."""

    log: list[str]
    researcher: FakeResearcher
    image_sourcer: FakeImageSourcer
    composer: FakeComposer
    renderer: FakeRenderer

    @property
    def pipeline(self) -> Pipeline:
        return Pipeline(
            researcher=self.researcher,
            image_sourcer=self.image_sourcer,
            composer=self.composer,
            renderer=self.renderer,
        )


def make_fakes(
    *, composer_error: Exception | None = None, result: RenderResult = RESULT
) -> Fakes:
    log: list[str] = []
    return Fakes(
        log=log,
        researcher=FakeResearcher(log),
        image_sourcer=FakeImageSourcer(log),
        composer=FakeComposer(log, error=composer_error),
        renderer=FakeRenderer(log, result=result),
    )


async def test_the_pipeline_runs_every_stage_in_order_and_threads_data_through() -> None:
    fakes = make_fakes()
    output_path = Path("out.png")

    result = await fakes.pipeline.run(Brief(prompt="p"), output_path)

    assert fakes.log == ["research", "source_images", "compose", "render"]
    assert fakes.image_sourcer.calls[0][1] is CONTENT
    assert fakes.composer.calls[0][1] is CONTENT
    assert fakes.composer.calls[0][2] is IMAGES
    assert fakes.renderer.calls[0][0] is COMPOSITION
    assert fakes.renderer.calls[0][1] is output_path
    assert result is RESULT


async def test_every_stage_that_takes_a_brief_receives_the_same_one() -> None:
    fakes = make_fakes()
    brief = Brief(prompt="p", audience="kids", max_facts=3)

    await fakes.pipeline.run(brief, Path("out.png"))

    assert fakes.researcher.briefs == [brief]
    assert fakes.image_sourcer.calls[0][0] is brief
    assert fakes.composer.calls[0][0] is brief


async def test_a_stage_failure_propagates_and_the_renderer_never_runs() -> None:
    fakes = make_fakes(composer_error=ValueError("cannot lay this out"))

    with pytest.raises(ValueError, match="cannot lay this out"):
        await fakes.pipeline.run(Brief(prompt="p"), Path("out.png"))

    assert fakes.renderer.calls == []
    assert "render" not in fakes.log


def test_parse_args_defaults_match_the_documented_render_options() -> None:
    brief, output_path = parse_args(["a panda"])

    assert brief.prompt == "a panda"
    assert brief.audience is None
    assert brief.options.width_px == 1200
    assert brief.options.height_px is None
    assert brief.options.theme is Theme.LIGHT
    assert brief.options.device_scale_factor == 2.0
    assert output_path == Path("infographic.png")


def test_parse_args_maps_every_flag_onto_the_brief() -> None:
    brief, output_path = parse_args(
        [
            "a panda",
            "--width",
            "800",
            "--height",
            "600",
            "--theme",
            "dark",
            "--scale",
            "1.0",
            "--max-facts",
            "4",
            "-o",
            "some/path.png",
        ]
    )

    assert brief.prompt == "a panda"
    assert brief.max_facts == 4
    assert brief.options.width_px == 800
    assert brief.options.height_px == 600
    assert brief.options.theme is Theme.DARK
    assert brief.options.device_scale_factor == 1.0
    assert output_path == Path("some/path.png")


def test_main_runs_the_pipeline_and_reports_where_the_png_landed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "out.png"
    fakes = make_fakes(
        result=RenderResult(
            output_path=output_path, width_px=1234, height_px=5678, bytes_written=99
        )
    )

    exit_code = main(["a panda", "-o", str(output_path)], build=lambda: fakes.pipeline)

    assert exit_code == 0
    assert [path for _, path in fakes.renderer.calls] == [output_path]
    captured = capsys.readouterr()
    assert str(output_path) in captured.out
    assert "1234" in captured.out
    assert "5678" in captured.out


def test_main_reports_a_stage_failure_without_leaking_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "out.png"
    fakes = make_fakes(composer_error=RuntimeError("compositor exploded"))

    exit_code = main(["a panda", "-o", str(output_path)], build=lambda: fakes.pipeline)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "compositor exploded" in captured.err
    assert "Traceback" not in captured.err
    assert str(output_path) not in captured.out


def test_help_exits_zero_and_documents_the_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--help"])

    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    assert all(
        flag in help_text
        for flag in ("--width", "--height", "--theme", "--scale", "--max-facts", "-o")
    )
