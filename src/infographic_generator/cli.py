"""Command-line entry point. Implemented by a follow-on agent."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from infographic_generator.core.models import Brief


def parse_args(argv: Sequence[str]) -> tuple[Brief, Path]:
    """Parse argv into a Brief and an output PNG path."""
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Returns a process exit code."""
    raise NotImplementedError
