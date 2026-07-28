"""Image sourcing stage: implementations of core.ports.ImageSourcer.

:class:`PandaImageSourcer` is the offline stub, serving the fixtures in
``assets/panda/``. :class:`WikimediaImageSourcer` is the real one -- it searches
Wikimedia Commons once per ``ResearchContent.keywords`` entry, verifies each
candidate's licence before considering it, then downloads and resizes what it
keeps. Both satisfy the same ``source_images`` coroutine, so the pipeline picks
one in its constructor and nothing outside this package changes.
"""

from infographic_generator.imagery.panda import (
    MAX_DIMENSION_PX,
    MAX_IMAGES,
    PandaImageSourcer,
)
from infographic_generator.imagery.wikimedia import (
    Candidate,
    WikimediaImageSourcer,
    WikimediaSettings,
)

__all__ = [
    "MAX_DIMENSION_PX",
    "MAX_IMAGES",
    "Candidate",
    "PandaImageSourcer",
    "WikimediaImageSourcer",
    "WikimediaSettings",
]
