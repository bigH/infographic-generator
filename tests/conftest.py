"""One suite-wide fixture, and deliberately nothing else.

``asyncio_mode = "auto"`` already comes from ``pyproject.toml``, and the file-local
credential scrubs in ``test_agent_composer.py`` and ``test_research_agent.py`` stay
exactly where they are -- redundant with this one, and correctly so.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Final

import pytest

API_KEY_ENV: Final = "ANTHROPIC_API_KEY"
AUTH_TOKEN_ENV: Final = "ANTHROPIC_AUTH_TOKEN"
LIVE_GATE: Final = "INFOGRAPHIC_LIVE_API"
"""Literals, not imports. The root ``conftest.py`` is imported ahead of every test in
the repo, so a zone renaming or moving its own copy of one of these must not be able
to take collection of the whole suite down with it. Two of the three are the SDK's
names anyway, and cannot drift."""


@pytest.fixture(scope="session", autouse=True)
def scrub_credentials_unless_live() -> Iterator[None]:
    """Strip both Anthropic credentials for the whole session unless the run is live.

    **Key presence is not consent to spend.** A real ``ANTHROPIC_API_KEY`` is exported
    in these shells and there is a populated ``.env`` at the repo root, so a client
    built with no arguments -- ``AgentComposer()`` resolves one from the environment --
    is a *billed* client, not an unauthenticated one.

    This is a belt, never the one thing standing between a test and a bill. The real
    defence is transport substitution: ``api_key="test"`` over an ``httpx.MockTransport``,
    which is what every Anthropic-shaped test here already does. This fixture only
    makes the test that forgets *inert* rather than *expensive*.

    Gated on ``== "1"`` and never on truthiness. ``INFOGRAPHIC_LIVE_API=0`` reads as
    "definitely not live" yet is a truthy string, so an ``if not os.environ.get(...)``
    gate hands that run its credentials straight back while the live-gated tests --
    which compare against ``"1"`` -- still skip. Setting the gate to ``0`` must never
    be the way the scrub gets switched off; that is the hole this closes.

    Session-scoped so that module- and session-scoped fixtures elsewhere are covered
    too: those are set up before any function-scoped fixture could scrub, and they are
    the natural place for a future test module to build a client once. Session scope
    rules out the function-scoped ``monkeypatch`` fixture, hence ``MonkeyPatch.context()``.

    The name collides with neither module-local scrub on purpose. A fixture here that
    shares a name with one in a test module is *shadowed* by it, silently, for that
    entire module -- which would reinstate the very gate bug described above.
    """
    with pytest.MonkeyPatch.context() as patch:
        if os.environ.get(LIVE_GATE) != "1":
            patch.delenv(API_KEY_ENV, raising=False)
            patch.delenv(AUTH_TOKEN_ENV, raising=False)
        yield
