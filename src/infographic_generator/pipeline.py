"""Wires the four stages into one run.

Dependency injection against the ``core.ports`` Protocols only: this module never
imports a concrete stage, which is what keeps the four ownership zones separable.
Concrete implementations are chosen in :func:`infographic_generator.cli.build_pipeline`.
"""

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

        Strictly sequential: image sourcing reads the researched content, and the
        composer needs both, so no stage may overlap another.

        Raises whatever the underlying stages raise; performs no error recovery.
        """
        content = await self.researcher.research(brief)
        images = await self.image_sourcer.source_images(brief, content)
        composition = await self.composer.compose(brief, content, images)
        return await self.renderer.render(composition, output_path)
