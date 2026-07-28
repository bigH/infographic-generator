"""Stage contracts. Four async ports, one per arrow of the pipeline.

Implementations live in sibling packages and are wired together by the pipeline.
Today they are hard-coded stubs; the docstrings below describe what the eventual
AI-backed implementations owe their callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from infographic_generator.core.models import (
    Brief,
    Composition,
    ImageAsset,
    RenderResult,
    ResearchContent,
)


class Researcher(Protocol):
    """Turns a brief into the content of the infographic.

    Contract: return a ``ResearchContent`` with a title, subtitle and summary
    always populated, at most ``brief.max_facts`` facts when that is set, and
    ``keywords`` good enough to drive image search. Every fact it can attribute
    carries a ``Source``; unattributed facts leave ``source`` as ``None`` rather
    than inventing a URL. Document-level ``sources`` is the bibliography.

    Raises: ``ValueError`` for a brief it cannot act on, ``RuntimeError`` or a
    transport error when its backing service fails.

    Real implementation: an agent that runs web searches, reads results, and
    extracts facts -- recording the actual URL, publisher and retrieval time of
    each page it read. Fabricated citations are the one unacceptable failure.
    """

    async def research(self, brief: Brief) -> ResearchContent: ...


class ImageSourcer(Protocol):
    """Finds imagery for the researched content.

    Contract: pick images relevant to ``content.keywords``, facts and title.
    Every returned asset is already fetched and decoded -- ``content`` is bytes
    or a local path, never a remote URL for someone else to download -- and every
    asset carries an ``ImageCredit``; licensing is not optional. ``mime_type``,
    ``width_px``, ``height_px`` and ``alt_text`` describe the real bytes, and
    ``ImageCredit.modified`` is set whenever you crop or resample.

    Deliver display-ready assets: return 0-6, most significant first, no
    dimension above 2000 px and roughly 1 MB or less encoded. Resampling is this
    stage's job; the composer only scales with CSS. The first ``HERO`` asset is
    the lead image; returning ``()`` is valid and the composer must cope.

    Raises: ``RuntimeError`` or a transport error when a provider fails.

    Real implementation: an agent that queries licensed image APIs, filters for
    usable licenses, downloads and (if needed) resizes, then writes attribution
    straight from the provider's metadata.
    """

    async def source_images(
        self, brief: Brief, content: ResearchContent
    ) -> Sequence[ImageAsset]: ...


class Composer(Protocol):
    """Lays content and images out as a self-contained HTML document.

    Contract: the returned ``Composition.html`` makes zero external requests at
    render time -- CSS inline in a ``<style>`` element, fonts and images embedded
    via :func:`infographic_generator.core.encoding.to_data_uri`, no ``<link>``,
    no CDN, no remote ``src``. Fonts must be a generic stack
    (``system-ui, sans-serif``) or a woff2 embedded as a data URI; a family that
    only exists on your machine makes the render machine-dependent.

    Honour ``brief.options``: set ``width_px`` to match, render the theme, and
    copy ``brief.options.height_px`` and ``brief.options.device_scale_factor``
    into the ``Composition`` unchanged -- the renderer cannot see the brief.

    Every string reaching this stage -- titles, facts, section bodies, alt text,
    credits -- is untrusted web text. HTML-escape all of it: with Jinja2 that
    means ``Environment(autoescape=True)``, which is not the default.

    ``images`` may be empty -- produce a text-only layout, never raise. There may
    be no ``HERO`` asset either; do not assume ``images[0]`` is one. Never rely on
    an asset's intrinsic size. Constrain every image with CSS; ``width_px``/
    ``height_px`` are for aspect-ratio decisions only. Render every fact you are
    given -- capping is the researcher's job. You may use a subset of ``images``,
    but embed and credit exactly the ones you display.

    Attribution lands in a PNG, so hyperlinks are worthless -- render ``license``
    and ``license_url`` as visible text in the rendered output, not just in the
    markup. Never interpolate a researched URL into an ``href`` or a CSS
    ``url(...)``.

    Raises: ``ValueError`` when the content cannot be laid out, ``OSError`` if an
    asset backed by a ``Path`` is unreadable.

    Real implementation: an agent that chooses a layout for the shape of the
    content and emits the HTML and CSS, still bound by the self-containment,
    escaping and attribution rules above.
    """

    async def compose(
        self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
    ) -> Composition: ...


class Renderer(Protocol):
    """Rasterises a composition to a PNG on disk.

    Contract: set the viewport from ``composition.width_px`` and
    ``composition.device_scale_factor`` before loading content, then
    ``await page.set_content(html, wait_until="load")`` -- data URIs still need a
    load turn before they paint. Abort all network in the browser context
    (``page.route("**/*", ...)`` -> abort) so a composition that reaches for a CDN
    fails loudly instead of rendering blanks; no base URL is needed or accepted,
    the HTML is self-contained.

    Write a PNG to ``output_path``, creating parent directories, and report the
    true pixel dimensions and byte count in the ``RenderResult``. A
    ``Composition.height_px`` of ``None`` means a full-page screenshot; otherwise
    clip to that height.

    Raises: ``OSError`` when the path is unwritable, ``RuntimeError`` or a
    browser-specific timeout when rendering fails.

    Real implementation: stays a headless browser screenshot. Determinism comes
    from the composition, so this stage should remain dumb.
    """

    async def render(
        self, composition: Composition, output_path: Path
    ) -> RenderResult: ...
