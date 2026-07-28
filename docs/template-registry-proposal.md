# Proposal: a six-template registry and a two-call composer

**To:** the zone-3 owner (`composition/`, `render/`)
**From:** the author of the closed `infographic-agent-scaffold` branch
**Status:** proposal only — no code in this change. Nothing here has been implemented.

## Why you are reading this

I built a parallel infographic agent on branch `infographic-agent-scaffold` (commit `71840e5`)
before I had read `CLAUDE.md`. It was branched from `0e7d226`, the init commit, which contained
only `.gitignore` — so it never saw the conventions, the ports, or the ownership zones. It
duplicated your zone with a root-level package, `pip`, synchronous functions, pydantic as the
domain spine, and its own Jinja2 environment.

None of that is being merged. The branch stays where it is as an archived reference. Two ideas in
it are worth your time, and this document is those two ideas mapped onto the contracts that
actually exist on `main`:

1. **A registry of six layout templates** — `stat_grid`, `timeline`, `comparison`,
   `process_flow`, `quote_spotlight`, `ranked_list` — instead of one fixed layout.
2. **A two-call split**: one cheap call picks the template from the *shape* of the content, a
   second maps content into that template's slots. Selection and mapping are different problems
   and conflating them makes both worse.

Everything below is a request, not a change. `composition/` is yours; `core/` and
`pyproject.toml` are the Architect's. The only file I have added is this one.

## What I got wrong, so you can discount it appropriately

Corrections that matter for reading the rest:

- **The template seam already exists.** `composer.py:34` is
  `def __init__(self, *, template_name: str = TEMPLATE_NAME)`, with
  `FileSystemLoader(TEMPLATE_DIR)` behind it. Any `.j2` file dropped in `templates/` is already
  loadable by name. What is missing is not a seam — it is more than one template, and something
  that *chooses*. My branch would have built a second, redundant seam.
- **`layout.py` already is a layout engine.** `build_page` is 287 lines of real decisions with
  ~679 lines of tests behind it. I am not proposing you throw it away; §"Where this lands"
  argues it becomes the `stat_grid` entry.
- **Theme is not a template.** `Theme.LIGHT/DARK` already works end to end via `data-theme`
  (`models.py:38`, template line 7, `:root[data-theme="dark"]` at line 30) and is tested. None of
  the six registry entries encode a palette. Six templates × two themes, not twelve templates.
- **`ports.py:103-105` already claims this idea.** The `Composer` docstring says the real
  implementation is "an agent that chooses a layout for the shape of the content and emits the
  HTML and CSS." A registry that picks from a fixed set is *not* an addition to that plan — it is
  a cheaper, deterministic, testable version of it. See §"What this replaces"; please don't let
  both get built.

## The contract this has to fit

Unchanged since `8664d82`, verified against `main` at `76a9db5`:

```python
async def compose(
    self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> Composition: ...
```

That signature does not change. Neither does `Composition`. The renderer reads exactly four
fields — `html`, `width_px`, `height_px`, `device_scale_factor`
(`playwright_renderer.py:56,63,68,80-84`) — and `Composition.title` is never read at all. **A
template registry is invisible to the renderer. No `Composition` field is needed for it**, and
adding one would be dead weight.

The invariants a registry must not break, all of which apply per-template and none of which get
easier with six templates:

- One `Environment(autoescape=True)`. There is exactly one in the repo (`composer.py:35-42`) and
  it must stay exactly one. Six templates mean six times as many chances for a second
  environment to be built without it. Route every template through the existing one.
- `StrictUndefined` (`composer.py:39`) means a template referencing a view-model field that does
  not exist fails at render time, not review time. This is the single biggest design constraint
  on a registry — see §"Per-template view models".
- Self-contained output: `to_data_uri` for every displayed image, no `<link>`, no remote `src`,
  generic font stacks.
- Attribution rendered as *visible text*, per licence. Not just present in the markup.
- `images` may be empty and there may be no `HERO`. Every one of the six must degrade to a
  text-only layout rather than raise.
- "Render every fact you are given — capping is the researcher's job" (`ports.py:92`). This one
  bites: see §"The honest problem with slot mapping".

## Shape 1 — the registry

A frozen dataclass, in `composition/`, no pydantic:

```python
@dataclass(frozen=True, slots=True)
class TemplateSpec:
    id: str
    display_name: str
    selection_hint: str      # prose fed to the selection call; the only LLM-facing field
    template_name: str       # a filename under composition/templates/
    image_roles: Sequence[ImageRole] = ()   # which slots this layout can fill
```

`TEMPLATE_REGISTRY: Mapping[str, TemplateSpec]`, module-level, immutable.

The six, with the content shape each one is for:

| id | Use when | Ordering axis |
|---|---|---|
| `stat_grid` | 3–6 standalone numeric facts, no inherent order | none |
| `timeline` | dated events — history, roadmap, milestones | chronological |
| `comparison` | exactly two things contrasted — A/B, before/after | paired |
| `process_flow` | an ordered procedure with no dates, just steps | sequential |
| `quote_spotlight` | one strong quote or claim carries the piece | none |
| `ranked_list` | a prioritised list — top 5 risks, biggest causes | rank |

`selection_hint` is the only part of a spec the model ever sees, which keeps the prompt and the
registry from drifting apart: add a template, and selection learns about it automatically.

## Shape 2 — the two-call split

Both calls live inside one `compose()`. The port still sees a single coroutine.

**Call 1 — select.** Inputs are the *shape* of the content, not its prose: `len(content.facts)`,
`len(content.sections)`, `len(images)`, whether a `HERO` exists, `brief.audience`,
`brief.locale`, the width/height aspect, plus the title, subtitle and summary for topic sense.
Output: a template id, a confidence, and a one-line rationale. This call is small and cheap
because it does not need the full fact list — only its census.

**Call 2 — map.** Now that the template is known, map content into *that template's* slots:
which fact is the headline stat, which image goes in which role, what each step's caption is,
what gets dropped when there are nine facts and six cells.

Why split rather than one call:

- **Different inputs.** Selection needs a census; mapping needs the whole payload. One call pays
  for the full payload just to answer a question the census settles.
- **Different failure modes, so different fallbacks.** If selection fails you fall back to a
  deterministic rule; if mapping fails you fall back to a different template or to the current
  layout. A single call gives you one all-or-nothing failure.
- **Testability.** Selection is assertable — "dated events pick `timeline`" is a test. A single
  blob that emits both a choice and a slot filling is testable only through its rendered HTML.
- **You can skip call 1 entirely.** `brief.extras["composition.template"]` (see below) or a rule
  table can supply the id, and call 2 still does the useful work.

Non-negotiable given `ports.py`: **`compose()` must never raise because a model misbehaved.**
The chain should be `selection → deterministic rule table → today's layout`, and the current
`HtmlComposer` stays the always-works default that offline tests use. Low confidence from call 1
should fall through to the rule table rather than be trusted.

## Per-template view models — the part that is actually hard

This is where a registry stops being cheap, and I want to be straight about it rather than sell
you a two-day job that is a two-week one.

`layout.Page` is one fixed shape: `hero`, `ledger: Sequence[Sequence[Stat]]`, `band`, `coda`,
`sections`, `references`, `credits`, plus chrome. Combined with `StrictUndefined`, six templates
sharing one `Page` are six *skins of one information architecture* — a timeline rendered through
`ledger` is a stat grid with dates in the labels. Genuinely different layouts need either
per-template view models or a much wider `Page`, and a wider `Page` means every template can
reference every field and only the tests tell you which are actually populated.

What I would suggest, entirely your call:

- Split `Page` into `PageChrome` (lang, direction, theme, title, title_scale, subtitle, summary,
  references, credits, width_px, gutter_px, min_height_px — all of which `layout.py` already
  computes and none of which are layout-specific) and a per-template body model.
- `_base.html.j2` holds the chrome: masthead, references, credits colophon. Six bodies extend it.
  The existing 432-line template already separates these concerns visually; this makes it
  structural.
- `to_data_uri` gets called **only for images the chosen template actually displays.** Today
  `layout.py:142` encodes every asset and `:160` credits every figure. With slot mapping that
  becomes both a real cost saving on render time and a licensing correctness fix: crediting an
  image you never display is noise, and it is the same code path that would otherwise fail to
  credit one you do. Keep credit generation keyed off *displayed* figures.

## Which `core/models.py` fields would have to be added, and why

I am not editing `core/`. This section exists so you can decide whether to take any of it to the
Architect. I have tried hard to keep the list minimal, and four of the six templates need
**nothing**.

### Needs nothing new

- **`stat_grid`** — `ResearchContent.facts` with `label`/`value`/`unit`/`detail`/`source` is
  already exactly a stat card. This is today's layout.
- **`process_flow`** — steps are `content.sections` in order. `NarrativeSection(heading, body)`
  is a step title and its explanation. `Sequence` order is the sequence.
- **`ranked_list`** — rank is list order, which `ResearchContent` already guarantees for `facts`
  and `sections`, and `ports.py:53-56` already mandates significance ordering for images. No
  `rank`/`priority`/`order` field is needed and adding one would duplicate list order.
- **Selection input** — every census signal comes from existing fields. No "layout hint" field on
  `Brief` is needed, because `Brief.extras` already reserves the `composition.*` namespace
  (`models.py:81-84`) with a documented "ignore unknown keys, never raise" rule, and has no
  consumer yet. `brief.extras["composition.template"]` gives you a manual override with **zero**
  core changes and without touching `cli.py`, which is the shared no-owner zone. I would start
  here.
- **Image slots** — `ImageRole` already has four members and `layout.py:247` reads only `HERO`.
  `ICON` and `BACKGROUND` are unused and are what `quote_spotlight` and icon-bearing layouts
  want. Use them; do not invent a parallel role concept.

### Genuinely cannot be expressed today

Three asks, all additive, all defaulted, so **every existing test and the panda stub keep
passing** — a frozen slotted dataclass accepts new trailing fields with defaults without
breaking a single existing construction site:

**1. `Fact.when: str | None = None`** — a display date or period.
`timeline` has a chronological axis and there is nowhere to put a date. `Source.retrieved_at` is
when *we read the page*, not when the event happened, and is wrong here. A string rather than a
`datetime` because real sources give "1869", "mid-1980s", "Q3 2024", and the composer only ever
sets it as text — a `datetime` would force the researcher to invent a precision it does not have.
The alternative is prefixing `Fact.label` with a date and having the template parse it back out
of untrusted web text, which is exactly the kind of parsing that turns escaping bugs into
rendering bugs.

**2. `Quote` + `ResearchContent.quotes: Sequence[Quote] = ()`** — `quote_spotlight` needs a
verbatim passage and *who said it*. A `Fact` cannot carry it: `label`/`value` splits a sentence
in the wrong place, and `Source` is the page the quote was found on, not the person who said it.
Attribution-to-a-person and provenance-to-a-URL are different things and a licensing-careful
project should not collapse them.

```python
@dataclass(frozen=True, slots=True)
class Quote:
    text: str
    speaker: str | None = None
    context: str | None = None     # role/affiliation, e.g. "WWF lead scientist"
    source: Source | None = None
```

**3. `ComparisonPair` + `ResearchContent.comparisons: Sequence[ComparisonPair] = ()`** —
`comparison` needs two values that belong to the same dimension. Two `Fact`s side by side lose
the thing that makes it a comparison: that both answer one question. Nothing in
`ResearchContent` expresses a pair.

```python
@dataclass(frozen=True, slots=True)
class ComparisonPair:
    dimension: str                 # "Daily bamboo intake"
    left_value: str
    right_value: str
    left_label: str | None = None  # subject names; the template may hoist these to a header
    right_label: str | None = None
    unit: str | None = None
    source: Source | None = None
```

If the Architect declines all three, you still get four working templates and the selection
machinery, and `timeline`/`comparison`/`quote_spotlight` simply never get chosen — the registry
degrades to the templates whose data it can actually fill. That is a reasonable v1 and a reason
to build selection before asking for fields.

## What we drop from the branch, explicitly

- **`pip`** → `uv` for everything.
- **Synchronous `def select_template(...)` / `def map_content(...)`** → `async`, matching the
  ports. All four stage methods are coroutines and that decision is already made
  (`docs/plan.md`).
- **The root-level `infographic_agent/` package and its own `pyproject.toml`** → everything lives
  under `src/infographic_generator/composition/`.
- **Its own Jinja2 environment and loader** → the one `Environment(autoescape=True)` at
  `composer.py:35`. The branch's `html_builder.py` built its own and I cannot promise it
  escaped correctly.
- **Its `ContentPayload` / `SourceRef` / `ContentFact` pydantic domain types** → these are a
  second, competing spine for `ResearchContent`, `Source` and `Fact`. Deleted outright. The
  domain is frozen dataclasses and stays that way.
- **`TemplateSpec` and the slot models as pydantic `BaseModel`s** → frozen dataclasses, as above.

**One deliberate exception, flagged for your judgement.** `pydantic>=2.13.4` is *already* a
declared dependency on `main` (`ce39001`, `pyproject.toml:12`), nothing imports it yet, and both
`CLAUDE.md` and that commit message scope it to one job: `client.messages.parse(...,
output_format=...)` structured output. Call 1 and call 2 are structured-output calls, so the two
response schemas would be pydantic models — **converted to frozen dataclasses at the boundary,
with nothing downstream of the parse seeing pydantic.** That is the repo's own stated position
rather than a reintroduction of the branch's approach, and it asks for no new dependency. If you
would rather the boundary schemas be hand-rolled too, say so; it changes one module.

## Where this lands, and what I am asking for

Everything is inside `composition/`, except the three optional core fields:

```
composition/
  composer.py          # HtmlComposer unchanged and still the default
  agent_composer.py    # new: the two-call Composer implementation
  registry.py          # new: TemplateSpec + TEMPLATE_REGISTRY
  selection.py         # new: census + deterministic rule table (no LLM; independently testable)
  layout.py            # PageChrome split out; today's body becomes the stat_grid body
  templates/
    _base.html.j2      # chrome extracted from infographic.html.j2
    stat_grid.html.j2  # today's layout, unchanged in appearance
    timeline.html.j2 …
```

No `cli.py` change is required if the manual override reads
`brief.extras["composition.template"]`. That keeps the whole thing out of the shared no-owner
zone, which I would treat as a hard requirement rather than a nicety.

### What this replaces

`ports.py:103-105` plans an agentic composer that chooses a layout and *emits HTML*.
`docs/plan.md` parks a compose→render→critique loop as out of scope for v1. This proposal is a
deterministic middle: the model picks from six hand-written, hand-tested layouts and fills their
slots, but never writes markup — so `autoescape`, self-containment and attribution stay
structurally guaranteed by templates you wrote, instead of being things a model has to remember
each time. If you would rather go straight to the emit-HTML agent, this proposal should be
dropped rather than built alongside it. Please pick one.

### Two licensing notes I would not want to discover late

- **`quote_spotlight` puts an image behind text.** A `BACKGROUND` image still carries full
  attribution obligations, and attribution over a busy photo is exactly where "visible in the
  rendered output" quietly stops being true. It needs a legibility treatment, not just a slot.
- **`docs/plan.md` and `CLAUDE.md` both park the ShareAlike question** — whether a PNG composed
  from the two `CC-BY-SA-4.0` panda images inherits ShareAlike. Six templates multiply the
  compositions that question applies to; it still needs a human, and it is not mine to answer.

### The ask

1. Do you want a registry at all, or is the emit-HTML agent the plan?
2. If yes: is the `PageChrome` / per-template-body split the right shape, given it touches a file
   you already shipped with substantial test coverage?
3. Should the three core fields go to the Architect, or should v1 ship the four templates that
   need nothing?

I have not touched `core/`, `pyproject.toml`, `composition/` or `render/`. Happy to implement
whatever survives this, in your zone, to your design.
