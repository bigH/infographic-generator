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

from infographic_generator.composition.layout import build_page
from infographic_generator.core.models import (
    Brief,
    Composition,
    ImageAsset,
    ResearchContent,
)

TEMPLATE_DIR: Final = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME: Final = "infographic.html.j2"


class HtmlComposer:
    """Lays a brief's content out as a tall portrait infographic page."""

    __slots__ = ("_environment", "_template_name")

    def __init__(self, *, template_name: str = TEMPLATE_NAME) -> None:
        self._environment = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self._template_name = template_name

    async def compose(
        self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
    ) -> Composition:
        """Build the page. Raises ``OSError`` if a ``Path`` asset is unreadable."""
        page = build_page(brief, content, images)
        html = self._environment.get_template(self._template_name).render(page=page)
        return Composition(
            html=html,
            width_px=brief.options.width_px,
            height_px=brief.options.height_px,
            device_scale_factor=brief.options.device_scale_factor,
            title=page.title,
        )
