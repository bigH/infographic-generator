"""The default :class:`~infographic_generator.core.ports.Composer`.

Renders a Jinja2 template into one self-contained HTML document: styles inline,
images as ``data:`` URIs, no ``<link>``, no ``<script>``, no remote font. The
environment is built with ``autoescape=True`` -- not Jinja2's default -- because
every string arriving here is untrusted web text.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from infographic_generator.composition.layout import build_page, build_page_for
from infographic_generator.composition.registry import TEMPLATE_REGISTRY
from infographic_generator.core.models import (
    Brief,
    Composition,
    ImageAsset,
    ResearchContent,
)

TEMPLATE_DIR: Final = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME: Final = "stat_grid.html.j2"
"""The registry's ``stat_grid`` layout: today's output, and the one that always
works. It extends ``_base.html.j2``, which is chrome only and not renderable."""


def build_environment() -> Environment:
    """The one Jinja environment. ``autoescape=True`` is not Jinja's default and
    every string arriving here is untrusted web text."""
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


class HtmlComposer:
    """Lays a brief's content out as a tall portrait infographic page."""

    __slots__ = ("_environment", "_template_id", "_template_name")

    def __init__(
        self, *, template_name: str = TEMPLATE_NAME, template_id: str | None = None
    ) -> None:
        """Compose with one layout.

        ``template_id`` is the supported way to ask for a registry layout: it
        picks both the body shape and the template file, and an unknown or
        blocked id degrades to ``stat_grid`` rather than raising. ``template_name``
        alone stays the raw escape hatch and always gets a ``stat_grid`` body, so
        pass it only for a template that reads that shape.
        """
        self._environment = build_environment()
        spec = None if template_id is None else TEMPLATE_REGISTRY.get(template_id)
        renderable = spec if spec is not None and spec.blocked_on is None else None
        self._template_id = template_id
        self._template_name = (
            template_name if renderable is None else renderable.template_name
        )

    async def compose(
        self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
    ) -> Composition:
        """Build the page.

        Raises ``OSError`` if a ``Path`` asset is unreadable on the default
        ``stat_grid`` path; the registry bodies skip an unreadable asset instead.
        """
        page = (
            build_page(brief, content, images)
            if self._template_id is None
            else build_page_for(self._template_id, brief, content, images)
        )
        html = self._environment.get_template(self._template_name).render(page=page)
        return Composition(
            html=html,
            width_px=brief.options.width_px,
            height_px=brief.options.height_px,
            device_scale_factor=brief.options.device_scale_factor,
            title=page.chrome.title,
        )
