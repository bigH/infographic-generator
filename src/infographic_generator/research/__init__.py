"""Web research stage: implementations of core.ports.Researcher.

:class:`PandaResearcher` is the offline stub, serving ``assets/panda/facts.json``.
:class:`LlmResearcher` is the real one -- one model call with web search and web
fetch attached, whose every citation is cross-checked in Python against the URLs
the server actually retrieved. Both satisfy the same ``research`` coroutine, so
the pipeline picks one in its constructor and nothing outside this package
changes.

Re-exporting the real researcher costs the CLI some start-up: ``cli.py`` imports
``research.panda``, which runs this file first, so the stub-only path now pays for
``anthropic`` and ``pydantic``. Measured at ~0.077 s -> ~0.24 s for ``import
infographic_generator.cli``. Accepted -- both are already hard dependencies,
``imagery/__init__.py`` made the same trade for the same reason, and 0.16 s in
front of a headless Chromium render is noise. If it ever stops being noise, drop
the ``agent`` re-export and let callers import from the module; do **not** add a
lazy ``__getattr__``, which defeats ``mypy --strict`` and surprises every reader.
"""

from infographic_generator.research.agent import (
    FOCUS_KEY,
    SOURCES_KEY,
    TONE_KEY,
    LlmResearcher,
    ResearchFailedError,
    ResearchSettings,
    retrieved_sources,
)
from infographic_generator.research.panda import PANDA_FACTS, PandaResearcher

__all__ = [
    "FOCUS_KEY",
    "PANDA_FACTS",
    "SOURCES_KEY",
    "TONE_KEY",
    "LlmResearcher",
    "PandaResearcher",
    "ResearchFailedError",
    "ResearchSettings",
    "retrieved_sources",
]
