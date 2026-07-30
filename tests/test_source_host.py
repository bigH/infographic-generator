"""Fences around the display host a source is attributed to (``layout.py::_host``).

This file exists because ``_host`` used to be built from ``urlsplit(url).netloc``, and
``.netloc`` keeps three things a rendered attribution must never carry: the *userinfo*,
so ``https://user:pw@host/x`` printed a password into the PNG; the *case*, so a host a
reader is meant to retype came out as the search result happened to spell it; and, with
an ``or url`` fallback, the *whole URL* whenever there was no host at all -- a
``mailto:``, a ``data:`` URI or a relative path setting itself into ``.tick``, a 10.5px
uppercase line, as though it were a publisher.

The research zone already documents the convention this now follows, at
``research/agent.py::_publisher_of``: built from ``.hostname``, never ``.netloc``. That
zone refuses a userinfo URL outright at admission; this one has to survive being handed
one anyway, because ``ImageCredit.source`` comes from the imagery zone and a stub or a
future sourcer is under no such rule.

Two layers, both asserted, because either alone is half a fence. The unit cells prove
the function -- what it returns for a bad port, an unparseable authority, an IPv6
literal. The rendered cells prove it is *wired to the page*: an attribution reaches
three different elements in three different bodies (``.row__src``, ``.chip__src``,
``.rank__src``) and a host reaches the bibliography through a fourth door,
``_reference``'s ``source.title or _host(source.url)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Final

import pytest

from infographic_generator.composition.layout import (
    _attribution,
    _authority,
    _host,
    _reference,
)
from infographic_generator.composition.registry import RENDERABLE_TEMPLATE_IDS
from infographic_generator.core.models import Fact, Source, Theme
from tests.test_composition import (
    ZWSP_HOST_PAYLOAD,
    compose_cell,
    parse,
)
from tests.test_template_bodies import sparse_content

# --------------------------------------------------------------------------- #
# The one hostile URL every layer here is measured against
# --------------------------------------------------------------------------- #

PASSWORD: Final = "tr0ub4dor!3"
"""The secret the fences look for by name.

Carries a ``!`` on purpose: the document embeds ~70 kB of base64 font payload, and a
needle made only of base64 alphabet characters could be found *inside* a woff2 blob and
turn a leak assertion into a coin flip. ``!`` is not in the base64 alphabet, is legal in
URL userinfo (a sub-delim), and is left alone by both ``_legible_url`` and autoescape --
so if it appears on the page, something put it there."""

USERINFO_URL: Final = f"https://reader:{PASSWORD}@Example.COM:8443/x"
"""One URL carrying all three ``.netloc`` defects at once: userinfo, mixed case, port."""

USERINFO_HOST: Final = "example.com:8443"
"""What ``_host(USERINFO_URL)`` owes the page.

Lowercased, so this exact string cannot be a substring of ``USERINFO_URL`` -- which is
what lets a rendered assertion tell the *title* door apart from the *URL* door when both
print in the same ``<li>``."""

HOSTLESS_URL: Final = "/relative/path/2024/"
"""A relative reference: the shape the old ``or url`` fallback printed in full."""

ZERO_WIDTH: Final = "\u200b"
REPLACEMENT: Final = "�"
"""Named rather than written into the assertions, where one is invisible in an editor
and the other is indistinguishable from a font that lacks the glyph."""


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

HOSTED: Final[tuple[tuple[str, str], ...]] = (
    ("https://www.example.com/a", "example.com"),
    ("https://EXAMPLE.com", "example.com"),
    ("https://sub.www.example.org/a", "sub.www.example.org"),
    ("//example.com/a", "example.com"),
    ("HTTPS://A.Example/x", "a.example"),
    ("https://example.com:443/p", "example.com:443"),
)
"""URLs that do have a host, and the display string each owes.

``sub.www.example.org`` is here because ``removeprefix`` and ``replace`` are one
character apart in a diff and worlds apart in behaviour; ``//example.com/a`` because a
scheme-relative reference is hostful despite having no scheme, and reading the host off
the string rather than off the parse would lose it."""

USERINFO: Final[tuple[tuple[str, str], ...]] = (
    (USERINFO_URL, USERINFO_HOST),
    ("https://user:pw@example.com/x", "example.com"),
    ("https://user:pw@www.example.com/x", "example.com"),
    ("http://user:pw@[::1]:99/z", "[::1]:99"),
    (f"https://reader:{PASSWORD}@example.com:99999/x", "example.com"),
)
"""Every shape where a credential sits in front of the host.

The last row is the compound case: userinfo *and* a port outside 0-65535. A single
``try`` around both the parse and the port lookup would return ``""`` here and lose the
host along with the whole attribution."""

HOSTLESS: Final[tuple[tuple[str, str], ...]] = (
    ("relative-path", HOSTLESS_URL),
    ("mailto", "mailto:a@b.c"),
    ("data-uri", "data:text/plain,hi"),
    ("about", "about:blank"),
    ("empty-string", ""),
    ("the-legal-host-www-dot", "https://www./x"),
    ("unclosed-ipv6-bracket", "https://[::1"),
)
"""``(id, url)`` for every URL that has no host to show, hence owes the page nothing.

``https://www./x`` is the one *legal* host whose ``www.``-stripped form is empty --
research keeps it via an ``or hostname`` fallback because it is structurally present
there; here there is nothing to print, and an empty attribution is the honest answer.
``https://[::1`` is in this table and again in the raising fence below: it is hostless
*because* ``urlsplit`` refuses it, which is a different claim from the other six."""

PRINTABLE_HOSTLESS: Final[tuple[tuple[str, str], ...]] = tuple(
    (case_id, url) for case_id, url in HOSTLESS if url
)
"""``HOSTLESS`` minus the empty string, for the regression guard below.

``_host("")`` returned ``""`` under the old code too, so the empty string cannot tell
"the whole URL is printed" apart from "nothing is printed" -- it is the one row where
the old bug and the fix agree, and asserting on it only ever measured ``"" != ""``."""

MIN_HOSTED: Final = 6
MIN_USERINFO: Final = 4
MIN_HOSTLESS: Final = 7
MIN_PRINTABLE_HOSTLESS: Final = 6
"""Floors written as literals, so deleting rows fails here instead of quietly
shrinking a parametrized fence into fewer cells."""


def test_the_url_tables_still_have_cells_to_run() -> None:
    """An emptied table skips; it does not fail. Pin the sizes so it cannot.

    Also pins that the three tables are disjoint by URL. A URL that drifted into two of
    them would be asserted to have a host and to have none, and whichever cell ran
    second would be measuring the opposite of what its name claims.
    """
    assert len(HOSTED) >= MIN_HOSTED, (
        f"HOSTED has {len(HOSTED)} rows, under the {MIN_HOSTED} this fence needs"
    )
    assert len(USERINFO) >= MIN_USERINFO, (
        f"USERINFO has {len(USERINFO)} rows, under the {MIN_USERINFO} this fence needs"
    )
    assert len(HOSTLESS) >= MIN_HOSTLESS, (
        f"HOSTLESS has {len(HOSTLESS)} rows, under the {MIN_HOSTLESS} this fence needs"
    )
    assert len(PRINTABLE_HOSTLESS) >= MIN_PRINTABLE_HOSTLESS, (
        f"PRINTABLE_HOSTLESS has {len(PRINTABLE_HOSTLESS)} rows, under the "
        f"{MIN_PRINTABLE_HOSTLESS} this fence needs -- it is HOSTLESS minus the one "
        "row the regression guard cannot read anything from"
    )

    hosted = {url for url, _ in HOSTED}
    userinfo = {url for url, _ in USERINFO}
    hostless = {url for _, url in HOSTLESS}
    overlap = (hosted & hostless) | (userinfo & hostless) | (hosted & userinfo)
    assert not overlap, (
        f"{sorted(overlap)} appear in two tables at once, so one cell asserts a host "
        "and another asserts there is none"
    )

    expected = {host for _, host in (*HOSTED, *USERINFO)}
    assert "" not in expected, (
        "a row in HOSTED or USERINFO expects an empty host, which is the HOSTLESS "
        "claim wearing the wrong table's name"
    )


@pytest.mark.parametrize(("url", "host"), HOSTED, ids=[url for url, _ in HOSTED])
def test_a_display_host_is_built_from_the_hostname_and_not_the_netloc(
    url: str, host: str
) -> None:
    """``.hostname`` lowercases and drops the userinfo; ``.netloc`` keeps both.

    A failure here means the attribution line and the bibliography title are showing a
    host the reader cannot retype -- either cased as the search result spelled it, or
    carrying something that is not a host at all.
    """
    assert _host(url) == host, (
        f"_host({url!r}) displayed {_host(url)!r}, expected {host!r}"
    )


@pytest.mark.parametrize(("url", "host"), USERINFO, ids=[url for url, _ in USERINFO])
def test_userinfo_never_survives_into_a_display_host(url: str, host: str) -> None:
    """The defect this whole file is named after: a password rendered into the PNG.

    Asserted three ways, because "the password is gone" and "the right string is left"
    are separate claims and the interesting bug satisfies one of them. A leak of
    ``reader@example.com`` -- credential dropped, account name kept -- passes a bare
    ``PASSWORD not in host`` check and is still someone's identity on a poster.
    """
    shown = _host(url)

    assert shown == host, f"_host({url!r}) displayed {shown!r}, expected {host!r}"
    assert "@" not in shown, (
        f"_host({url!r}) displayed {shown!r}, which still contains an '@': userinfo "
        "reached the rendered host"
    )
    assert PASSWORD not in shown, (
        f"_host({url!r}) displayed {shown!r}, which contains the password {PASSWORD!r}"
    )


def test_a_port_is_kept_even_when_it_is_the_scheme_default() -> None:
    """``:443`` is shown, not elided, because this string is here to be retyped.

    Research drops the port from its publisher and this zone keeps it. The two can
    afford to disagree: ``_attribution`` only ever reaches a host when the source
    carried no publisher for research to have built one from. What they must not do is
    disagree about *which* host -- hence the case and userinfo cells above.
    """
    assert _host("https://example.com:443/p") == "example.com:443", (
        "_host dropped the default-looking port from 'https://example.com:443/p' and "
        f"displayed {_host('https://example.com:443/p')!r}: a host on an explicit port "
        "is a different origin, and a reader copying it out of the PNG needs it"
    )
    assert _host("https://example.com/p") == "example.com", (
        "_host invented a port for a URL that carried none: "
        f"{_host('https://example.com/p')!r}"
    )


def test_a_malformed_port_costs_the_url_its_port_and_not_its_host() -> None:
    """``.port`` raises on its own, separately from ``urlsplit``, and one bad port is
    no reason to drop a good host and with it the entire attribution.

    This is the branch a single ``try`` around both the parse and the port lookup gets
    wrong: it returns ``""``, ``_attribution`` reads that as "nothing to attribute", and
    a fact that had a perfectly readable source renders with no source line at all.
    """
    assert _host("http://h:99999/p") == "h", (
        "_host('http://h:99999/p') displayed "
        f"{_host('http://h:99999/p')!r}, expected 'h': the port is out of range and "
        "unprintable, the host is not, and the attribution is owed the host"
    )
    assert _attribution(Source(url="http://h:99999/p")) == "h", (
        "a source whose only defect is an out-of-range port lost its whole "
        f"attribution: {_attribution(Source(url='http://h:99999/p'))!r}"
    )
    assert _host("http://[::1]:99]/p") == "[::1]", (
        "_host('http://[::1]:99]/p') displayed "
        f"{_host('http://[::1]:99]/p')!r}, expected '[::1]': the port is not an "
        "integer, so .port raises where urlsplit did not, and the host survives it"
    )
    assert _host("https://exam ple.com:port/p") == "exam ple.com", (
        "_host('https://exam ple.com:port/p') displayed "
        f"{_host('https://exam ple.com:port/p')!r}: urlsplit tolerates both the space "
        "and the non-numeric port, so this reaches the page as a host and the only "
        "thing dropped is the port"
    )


def test_an_ipv6_host_stays_reconstructible() -> None:
    """``.hostname`` unbrackets an IPv6 literal, and ``::1:99`` is not an authority.

    Re-bracketing is not cosmetic: without it the printed string cannot be pasted back
    into a URL bar, which is the only thing an attribution line is for.
    """
    assert _host("http://[::1]:99/z") == "[::1]:99", (
        f"_host('http://[::1]:99/z') displayed {_host('http://[::1]:99/z')!r}, "
        "expected '[::1]:99' -- an unbracketed literal with a port appended is "
        "ambiguous with the address itself"
    )
    assert _host("http://[2001:db8::1]/z") == "[2001:db8::1]", (
        "a portless IPv6 host lost its brackets: "
        f"{_host('http://[2001:db8::1]/z')!r}"
    )


@pytest.mark.parametrize(
    ("case_id", "url"), HOSTLESS, ids=[case_id for case_id, _ in HOSTLESS]
)
def test_a_url_with_no_host_displays_nothing(case_id: str, url: str) -> None:
    """Empty, so ``_attribution`` can read it as "nothing to attribute".

    Nothing verifiable is lost by staying quiet: a source in the bibliography still
    prints its URL in full in ``.refs__meta``. What the page gains is not claiming that
    ``mailto:a@b.c`` is a publisher.
    """
    assert _host(url) == "", (
        f"_host({url!r}) ({case_id}) displayed {_host(url)!r}, expected '' -- this URL "
        "has no host, so there is no host to show"
    )


@pytest.mark.parametrize(
    ("case_id", "url"),
    PRINTABLE_HOSTLESS,
    ids=[case_id for case_id, _ in PRINTABLE_HOSTLESS],
)
def test_a_hostless_url_is_never_printed_whole(case_id: str, url: str) -> None:
    """Regression guard on the exact old shape: ``_legible_url(_netloc(url) or url)``.

    That ``or url`` is why ``/relative/path/2024/`` and ``mailto:a@b.c`` used to set
    themselves, entire, into ``.tick``. The cell above asserts the new value; this one
    asserts the old one is unreachable, so a future author restoring the fallback for
    "we should show *something*" reasons trips a test that names what they broke.
    """
    shown = _host(url)

    assert shown != url, (
        f"_host({url!r}) ({case_id}) returned the whole URL again -- the old "
        "'_netloc(url) or url' fallback is back, and a path or a scheme is rendering "
        "as though it were a publisher"
    )
    assert url not in shown, (
        f"_host({url!r}) ({case_id}) displayed {shown!r}, which still contains the URL"
    )


MALFORMED: Final[tuple[str, ...]] = (
    "https://[::1",
    "http://[::1]x/p",
    "https://a[::1]b/p",
    "https://user@[::1",
)
"""Authorities ``urlsplit`` refuses outright, every one with ``ValueError: Invalid IPv6
URL``.

All four are bracket damage, and that is not a gap in the table -- it is the whole of
what ``urlsplit`` raises on. It tolerates a space in a host and a non-numeric port
without complaint (``https://exam ple.com:port/p`` parses to the host ``exam ple.com``),
so those are *not* cells for this fence and belong to the port branch instead. The
userinfo row is here because the guard has to fire before a credential could be read
off a URL that never parsed."""


@pytest.mark.parametrize("url", MALFORMED, ids=MALFORMED)
def test_a_malformed_url_yields_no_host_instead_of_raising(url: str) -> None:
    """``core.ports`` defines ``ValueError`` as "a brief the stage cannot act on".

    A ``ValueError`` escaping here would blame the caller for a bad *search result* --
    aborting a whole composition, and every fact in it, over one unparseable authority
    that the page was always free to say nothing about.
    """
    assert MALFORMED, "the malformed-URL table is empty, so this fence measures nothing"

    shown = _host(url)

    assert shown == "", f"_host({url!r}) displayed {shown!r}, expected ''"
    assert _attribution(Source(url=url)) is None, (
        f"a source with the unparseable URL {url!r} produced the attribution "
        f"{_attribution(Source(url=url))!r} instead of None"
    )


def test_an_invisible_character_in_a_host_is_still_replaced() -> None:
    """The new ``.hostname`` path must still run the host through ``_legible_url``.

    ``_host`` is ``_legible_url(_authority(url))``, and this asserts both halves
    separately: ``_authority`` hands over the raw host with U+200B intact, and ``_host``
    hands the page U+FFFD in its place. Asserting only the second could not tell a
    working sanitiser from a URL that never carried the character.

    U+200B in a *host* is always a spoof -- ``exa<ZWSP>mple.com`` reads as
    ``example.com`` and resolves somewhere else -- which is why ``_legible_url``
    replaces it where ``_legible_text`` keeps it (Khmer puts its word boundaries there).
    """
    raw = _authority(ZWSP_HOST_PAYLOAD)
    shown = _host(ZWSP_HOST_PAYLOAD)

    assert raw == f"exa{ZERO_WIDTH}mple.com", (
        f"_authority({ZWSP_HOST_PAYLOAD!r}) returned {raw!r}: this fixture is supposed "
        "to deliver an invisible character to the sanitiser, and it no longer does"
    )
    assert shown == f"exa{REPLACEMENT}mple.com", (
        f"_host({ZWSP_HOST_PAYLOAD!r}) displayed {shown!r}, expected "
        f"{f'exa{REPLACEMENT}mple.com'!r} -- the hostname path skipped _legible_url"
    )
    assert ZERO_WIDTH not in shown, (
        f"_host({ZWSP_HOST_PAYLOAD!r}) displayed {shown!r}, which still contains "
        "U+200B: it reads as 'example.com' and resolves somewhere else"
    )
    assert len(shown) == len(raw), (
        f"_host replaced {len(raw) - len(shown)} characters by deleting them "
        f"({raw!r} -> {shown!r}); a citation host with bytes silently removed is a "
        "quieter spoof, not a fixed one"
    )


# --------------------------------------------------------------------------- #
# _attribution: the host is the last resort, and its absence is None
# --------------------------------------------------------------------------- #


def test_an_attribution_prefers_publisher_then_title_then_host() -> None:
    """Why every fixture below has to be built by hand rather than from ``make_source``.

    ``_attribution`` is ``source.publisher or source.title or _host(source.url)``, and
    the shared builder always sets ``publisher="WWF"`` -- so a fence written on top of
    it never reaches the host branch at all, and would have passed throughout the era
    when that branch printed a password.
    """
    url = USERINFO_URL

    assert _attribution(Source(url=url, title="A title", publisher="WWF")) == "WWF"
    assert _attribution(Source(url=url, title="A title", publisher=None)) == "A title"
    assert _attribution(Source(url=url, title=None, publisher=None)) == USERINFO_HOST, (
        "with no publisher and no title, the attribution is the display host: got "
        f"{_attribution(Source(url=url))!r}, expected {USERINFO_HOST!r}"
    )


def test_an_attribution_with_nothing_to_attribute_is_none_and_not_empty() -> None:
    """``""`` would be a third state for a field typed ``str | None``.

    Every template guards with ``{% if stat.attribution %}``, so an empty string is
    treated as absent -- by luck rather than by contract. Identity to ``None`` is the
    assertion, not falsiness: ``not _attribution(...)`` passes for both states and so
    proves nothing about which one shipped.
    """
    nothing = Source(url=HOSTLESS_URL, title=None, publisher=None)

    assert _attribution(nothing) is None, (
        f"a source with no publisher, no title and the hostless URL {HOSTLESS_URL!r} "
        f"produced {_attribution(nothing)!r}; expected None, and note that '' would "
        "have satisfied a falsiness check"
    )
    assert _attribution(None) is None, "a fact with no source at all must attribute None"


def test_a_reference_titles_an_untitled_source_with_its_host() -> None:
    """The second door a host walks through: ``_reference``'s ``title or _host(url)``.

    Note the asymmetry with ``_attribution``, and that it is correct: a reference falls
    back to the host on a missing *title* alone, publisher or no publisher, because the
    publisher has its own slot in ``.refs__meta`` beside the URL.

    The URL itself stays byte-exact, userinfo included. That is not a leak the fence
    forgot -- a citation URL is a verification key, and a bibliography that prints a URL
    nobody published is worse than one that prints an embarrassing one.
    """
    reference = _reference(Source(url=USERINFO_URL, title=None, publisher=None))

    assert reference.title == USERINFO_HOST, (
        f"an untitled source was titled {reference.title!r} in the bibliography, "
        f"expected {USERINFO_HOST!r}"
    )
    assert PASSWORD not in reference.title, (
        f"the bibliography title {reference.title!r} carries the password"
    )
    assert reference.url == USERINFO_URL, (
        f"the reference URL was rewritten to {reference.url!r}: a verification key has "
        "to match the page that was actually retrieved"
    )


# --------------------------------------------------------------------------- #
# The rendered page: every body, both doors
# --------------------------------------------------------------------------- #

ATTRIBUTION_CLASS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stat_grid": "row__src",
        "process_flow": "chip__src",
        "ranked_list": "rank__src",
    }
)
"""Where each body prints a fact's attribution. Three bodies, three class names, one
promise -- and a per-body table so a cell that finds nothing fails by name."""

BODIES: Final = pytest.mark.parametrize(
    "template_id", sorted(ATTRIBUTION_CLASS), ids=sorted(ATTRIBUTION_CLASS)
)


def test_every_renderable_body_declares_where_it_prints_an_attribution() -> None:
    """A new body with no row here would render an unfenced attribution."""
    assert set(ATTRIBUTION_CLASS) == set(RENDERABLE_TEMPLATE_IDS), (
        "ATTRIBUTION_CLASS and RENDERABLE_TEMPLATE_IDS have diverged: missing "
        f"{sorted(RENDERABLE_TEMPLATE_IDS - set(ATTRIBUTION_CLASS))}, stale "
        f"{sorted(set(ATTRIBUTION_CLASS) - RENDERABLE_TEMPLATE_IDS)}"
    )


def hostile_fact(url: str) -> Fact:
    """One fact whose source has a URL and nothing else.

    No publisher and no title, so ``_attribution`` reaches ``_host``. A *fact* source
    rather than a document source, so the URL never enters the bibliography -- which is
    what makes "the password is nowhere in this document" a legitimate bar to hold the
    attribution door to.
    """
    return Fact(
        label="Bamboo metric 01",
        value="017.5",
        unit="kg",
        source=Source(url=url, title=None, publisher=None),
    )


@BODIES
async def test_no_password_reaches_the_page_through_an_attribution(
    template_id: str,
) -> None:
    """The whole point, measured on a real ``Composition`` rather than on a function.

    The premise is asserted before the leak is: this page must not print
    ``USERINFO_URL`` anywhere, because a fact's source is not a bibliography source. If
    that ever changes, the first assertion fails and says to rescope rather than
    quietly turning the second one into a test of ``.refs__meta``.
    """
    content = sparse_content(facts=(hostile_fact(USERINFO_URL),), sections=())

    composition = await compose_cell(template_id, Theme.LIGHT, content)
    shown = parse(composition.html).text_in(ATTRIBUTION_CLASS[template_id])

    assert USERINFO_URL not in composition.html, (
        f"{template_id} printed the full source URL somewhere on the page, so a fact "
        "source now reaches the bibliography and this fence is measuring the wrong "
        "door"
    )
    assert PASSWORD not in composition.html, (
        f"{template_id} rendered the password {PASSWORD!r} into the document: a "
        "credential is on the poster"
    )
    assert shown == USERINFO_HOST, (
        f"{template_id} rendered .{ATTRIBUTION_CLASS[template_id]} as {shown!r}, "
        f"expected {USERINFO_HOST!r}"
    )
    assert "@" not in shown, (
        f"{template_id} rendered .{ATTRIBUTION_CLASS[template_id]} as {shown!r}, "
        "which still contains an '@'"
    )


@BODIES
async def test_a_hostless_source_renders_no_attribution_line_at_all(
    template_id: str,
) -> None:
    """Not "a shorter line" -- no element. The old fallback rendered the whole path.

    Both halves matter. The element being absent is what proves ``_attribution``
    returned ``None`` rather than a string the template dutifully printed; the path
    being absent from every rendered string is what proves it did not land somewhere
    else on the page instead.
    """
    content = sparse_content(facts=(hostile_fact(HOSTLESS_URL),), sections=())

    composition = await compose_cell(template_id, Theme.LIGHT, content)
    parsed = parse(composition.html)
    elements = parsed.classed(ATTRIBUTION_CLASS[template_id])

    assert elements == (), (
        f"{template_id} rendered {len(elements)} .{ATTRIBUTION_CLASS[template_id]} "
        f"element(s) containing {parsed.text_in(ATTRIBUTION_CLASS[template_id])!r} for "
        "a source with no publisher, no title and no host -- there was nothing to "
        "attribute and the page said something anyway"
    )
    assert HOSTLESS_URL not in parsed.rendered_strings, (
        f"{template_id} printed the relative path {HOSTLESS_URL!r} on the page; a path "
        "is not a publisher, and .tick sets it 10.5px and uppercased"
    )


async def test_the_bibliography_prints_the_password_only_in_its_url() -> None:
    """The fourth door, where the same source legitimately prints twice.

    A bare "the password is not in the html" assertion cannot run here: this source *is*
    a document source, so ``.refs__meta`` prints its URL in full and must. So the
    measurement is a count instead -- the password may appear exactly once, and that
    once must be inside ``.refs__meta``. A leak through the title door would make it
    two.

    The title is identified by case, not by position: ``_host`` lowercases, so
    ``example.com:8443`` is a string only the title door can produce, while the URL
    keeps ``Example.COM:8443``. Counting it in ``.refs`` and again in ``.refs__meta``
    says which element it landed in without needing a browser.
    """
    source = Source(url=USERINFO_URL, title=None, publisher=None)
    content = replace(sparse_content(facts=(), sections=()), sources=(source,))

    composition = await compose_cell("stat_grid", Theme.LIGHT, content)
    parsed = parse(composition.html)
    in_page = composition.html.count(PASSWORD)
    in_meta = parsed.text_in("refs__meta").count(PASSWORD)

    assert in_page == 1, (
        f"the password {PASSWORD!r} appears {in_page} time(s) in the document; it may "
        "appear exactly once, in the citation URL it is part of"
    )
    assert in_meta == 1, (
        f"the password appears {in_meta} time(s) inside .refs__meta and {in_page} "
        "time(s) in the document, so the occurrence is somewhere other than the URL "
        "span"
    )
    assert parsed.text_in("refs").count(USERINFO_HOST) == 1, (
        f"the bibliography entry does not title itself {USERINFO_HOST!r}: .refs reads "
        f"{parsed.text_in('refs')!r}"
    )
    assert parsed.text_in("refs__meta").count(USERINFO_HOST) == 0, (
        f"the lowercased host {USERINFO_HOST!r} turned up inside .refs__meta, so it "
        "is not the marker that distinguishes the title door from the URL door and "
        "the count above proves nothing"
    )
