"""Wires the four stages into one run. Implemented by a follow-on agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from infographic_generator.core.models import Brief, RenderResult
from infographic_generator.core.ports import Composer, ImageSourcer, Renderer, Researcher


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Research -> source images -> compose HTML -> render PNG."""

    researcher: Researcher
    image_sourcer: ImageSourcer
    composer: Composer
    renderer: Renderer

    async def run(self, brief: Brief, output_path: Path) -> RenderResult:
        """Execute all four stages in order and return the render result.

        Raises whatever the underlying stages raise; performs no error recovery.
        """
        raise NotImplementedError
