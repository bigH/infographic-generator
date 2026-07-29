# Proposal: a six-template registry and a two-call composer

**To:** the zone-3 owner (`composition/`, `render/`)
**From:** the author of the closed `infographic-agent-scaffold` branch
**Status:** part record, part answered ask. Most of this shipped in four commits on `main`: the
six-entry registry and deterministic selection (`0d40d6d`), the `PageChrome`/per-template-body
split with `_base.html.j2` and per-template CSS (`4ba3a4d`), the `process_flow` and `ranked_list`
bodies (`eed91b9`), and the two-call `AgentComposer` (`e6cc575`). Five further commits have
fenced that work (`6e473b3`, `55bbfa6`, `7f2df66`, `dab1ded`, `1d6e04c`), and their measurements
falsified several claims this document used to make — see §"What shipped, and where it deviated"
and §"Fences considered, and the one that was rejected". Three templates render; three are
registered and `blocked_on` a core field. The body below is unchanged apart from additions and
corrections — its reasoning is the rationale for what was built. The three closing questions are
now answered in §"The ask"; the questions that were still genuinely open are answered in
§"Open questions, decided". One question is deliberately left open, and it is Hiren's, not
zone 3's: §"The one question nobody here gets to answer".

*(An earlier revision of this line cited `3244ad6`, `cdaa330`, `8f0ecde` and `f34ee08`. None of
those four resolve in this repository — they were pre-rebase hashes, written down before the
branch was replayed onto `main` and never re-checked. The four above are verified with
`git show --stat`, and they map to the four described changes in the order given.)*

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

*(Written before implementation. `composition/` has since been added to — see the status line and
§"What shipped, and where it deviated". `core/` and `pyproject.toml` are still untouched.)*

## What I got wrong, so you can discount it appropriately

Corrections that matter for reading the rest:

- **The template seam already exists.** At the time of writing, `HtmlComposer.__init__` was
  `def __init__(self, *, template_name: str = TEMPLATE_NAME)`, with `FileSystemLoader(TEMPLATE_DIR)`
  behind it. Any `.j2` file dropped in `templates/` was already loadable by name. What was missing
  was not a seam — it was more than one template, and something that *chooses*. My branch would
  have built a second, redundant seam. *(It is now
  `def __init__(self, *, template_name: str = TEMPLATE_NAME, template_id: str | None = None)`,
  `composer.py:50-52`.)*
- **`layout.py` already is a layout engine.** *Correction to my own arithmetic:* I wrote "`build_page`
  is 287 lines"; 287 was the length of the whole **module** at `8648c21`, and `build_page` itself was
  27 (`8648c21`'s `layout.py:138-164`). The ~679 lines of tests behind it is right — that was
  `tests/test_composition.py` at the same commit, exactly 679 lines. Either way the point stood and still stands: I was
  not proposing you throw it away, and §"Where this lands" argued it becomes the `stat_grid` entry,
  which is what happened.
- **Theme is not a template.** `Theme.LIGHT/DARK` already works end to end via `data-theme`
  (`models.py:38`, now `_base.html.j2:16` and `:root[data-theme="dark"]` at `css/_chrome.css:57`)
  and is tested. None of the six registry entries encode a palette. Six templates × two themes, not
  twelve templates. *(The original line numbers here pointed into `infographic.html.j2`, which
  `4ba3a4d` deleted; a later fence also measured that theme is insurance rather than information —
  ~11,600 computed values compared light against dark across 82–122 elements, **zero** differ.)*
- **`ports.py:103-105` already claims this idea.** The `Composer` docstring says the real
  implementation is "an agent that chooses a layout for the shape of the content and emits the
  HTML and CSS." A registry that picks from a fixed set is *not* an addition to that plan — it is
  a cheaper, deterministic, testable version of it. See §"What this replaces"; please don't let
  both get built.

## The contract this has to fit

Unchanged since `8664d82`, re-verified against `main` at `1d6e04c`:

```python
async def compose(
    self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> Composition: ...
```

That signature does not change. Neither does `Composition`. The renderer reads exactly four
fields — `html`, `width_px`, `height_px`, `device_scale_factor`
(`playwright_renderer.py:116,136,141,180-183`) — and `Composition.title` is never read anywhere in
`src/`: `composer.py:88` writes it and nothing consumes it outside tests. **A
template registry is invisible to the renderer. No `Composition` field is needed for it**, and
adding one would be dead weight.

The invariants a registry must not break, all of which apply per-template and none of which get
easier with six templates:

- One `Environment(autoescape=True)`. There is exactly one *construction* in `src/`
  (`composer.py:35-42`, inside `build_environment` at `:32`) and it must stay exactly one — the only
  other textual occurrence is the prose mention in the `Composer` docstring at `ports.py:86`, which is
  why the fence's regex deliberately excludes backticked text. Six
  templates mean six times as many chances for a second environment to be built without it. Route
  every template through the existing one. *(`6e473b3` lifted `build_environment` to module level so
  the registry-integrity fence can `get_template` without reaching into `HtmlComposer._environment`
  — still exactly one construction, pinned by `tests/test_chrome_split.py:124-132`.)*
- `StrictUndefined` (`composer.py:38`) means a template referencing a view-model field that does
  not exist fails at render time, not review time. This is the single biggest design constraint
  on a registry — see §"Per-template view models".
- Self-contained output: `to_data_uri` for every displayed image, no `<link>`, no remote `src`,
  generic font stacks.
- Attribution rendered as *visible text*, per licence. Not just present in the markup.
- `images` may be empty and there may be no `HERO` (`ports.py:88-89`). Every one of the six must
  degrade to a text-only layout rather than raise.
- "Render every fact you are given — capping is the researcher's job" (`ports.py:91-92`). This one
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
    image_roles: Sequence[ImageRole] = ()   # advisory only -- see below
    blocked_on: str | None = None           # added during implementation
```

`TEMPLATE_REGISTRY: Mapping[str, TemplateSpec]`, module-level, immutable — it shipped as a
`MappingProxyType` over a `_SPECS` tuple (`registry.py:129-131`), which is what makes
`RENDERABLE_TEMPLATE_IDS` derivable rather than hand-listed.

**`image_roles` enforces nothing, and the comment above is the aspiration, not the behaviour.** It
is read in exactly one place in the whole tree — `agent_composer.py:299`, joined into prose for a
model prompt — and no builder filters on it. Measured: handing all five panda assets to every body
as `ImageRole.BACKGROUND` (a role listed only for the blocked `quote_spotlight`) renders output
identical to the role-varied run, and `ranked_list`, whose `image_roles` is `(HERO,)` alone, happily
places four non-hero figures. Role affects exactly one thing anywhere: `_hero_index`
(`layout.py:682`) picks the first `HERO` and otherwise index 0. `6e473b3` repaired the docstring that
had claimed assets in other roles are "surplus"; it deliberately did **not** add enforcement, because
nothing wants it yet. **`TODO:`** the repaired docstring (`registry.py:37-39`) now says the read is in
`agent_composer`'s *selection prompt*; it is in the **mapping** prompt (`agent_composer.py:299`, inside
`mapping_prompt` at `:292` — `selection_prompt` at `:266` shows the model only `id` and
`selection_hint`). One word, in zone 3's file, not mine.

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
should fall through to the rule table rather than be trusted. *(Shipped as
`MIN_CONFIDENCE: Final = 0.55` at `selection.py:50`, gated by `_is_trusted` at `:230-232`, which
requires renderability **and** the threshold.)*

## `learning_preference` — the one input this document left out

The archived branch's selection prompt read a third input I omitted above, and it deserves its own
section because it is the only orphan of that prompt: `learning_preference`, one of `text_heavy`,
`image_heavy` or `balanced`. On the branch it was a bare `str` on the content payload
(`infographic_agent/contracts/content.py:21` — the branch had no `src/` layout, and my original
citation dropped the package prefix), interpolated straight into both prompts.

It now lives in `brief.extras["composition.learning_preference"]`, read by
`selection.learning_preference_of`, which lowercases and strips the value and returns `BALANCED`
for anything absent, empty or unrecognised. It never raises — `models.py:81-84` documents "ignore
keys outside your namespace and never raise on an unknown key" for the `composition.*` namespace,
and a garbled hint must not cost a render.

`extras` rather than a field on `Brief` for the same reason as the template override: **zero
`core/` change**, and no `cli.py` change, so it is outside both the Architect's zone and the shared
no-owner zone. The cost is that `extras` is a `Mapping[str, str]`, so the value arrives as a string
and is parsed into a `LearningPreference` `StrEnum` at the edge — and that enum lives in
`composition/selection.py`, not in `core/`, which is where it belongs while it is one zone's
concern.

It is a **tiebreaker only, never a driver**. `choose_template` decides on content shape first: many
facts with few sections gives `ranked_list`, sections outnumbering facts gives `process_flow`. The
preference is read only in the third branch, where neither shape rule fired — `TEXT_HEAVY` with at
least one section tips to `process_flow` at confidence 0.6, `IMAGE_HEAVY` with at least one image
keeps `stat_grid` but raises confidence from 0.5 to 0.6. When a shape rule fires the preference is
not consulted at all, so it cannot overturn an unambiguous signal;
`test_preference_never_overrides_an_unambiguous_shape` pins that by asserting the choice is
identical across all three preferences. That ranking mirrors the archived prompt, which listed
content shape first and the preference second.

Two honest limits. No registry entry's `selection_hint` mentions image-weight at all, so an LLM
selector handed the census has to infer what the preference should imply on its own — the hints
describe content shape only. And nothing in the pipeline populates the key today: `cli.py` builds its
`Brief` at `:121` without passing `extras` at all, and neither it nor `pipeline.py` mentions `extras`
anywhere. It is a hook for a caller, not a live feature — **and that is also true of
`composition.template`, so the whole override-and-preference surface is unreachable from the CLI.**
Worth stating plainly, because it changes how much the silent-override question in §"Open questions,
decided" costs today: the answer is nothing, today. It costs on the day someone wires `extras` up.

## Per-template view models — the part that is actually hard

This is where a registry stops being cheap, and I want to be straight about it rather than sell
you a two-day job that is a two-week one.

`layout.Page` **was** one fixed shape: `hero`, `ledger: Sequence[Sequence[Stat]]`, `band`, `coda`,
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
  The existing template already separates these concerns visually; this makes it structural.
- `to_data_uri` gets called **only for images the chosen template actually displays.** Keep credit
  generation keyed off *displayed* figures: crediting an image you never display is noise, and it is
  the same code path that would otherwise fail to credit one you do.

*(All three landed in `4ba3a4d`. `Page` is now two fields — `chrome: PageChrome` and `body: PageBody`
— at `layout.py:261-266`; `PageChrome` is 14 fields at `:177-194`, having since gained `fonts` and
`title_fit`. The template that got split was **496** lines when `4ba3a4d` deleted it, not the 432 I
wrote here: `49dc67b` grew it between my writing this and the split landing. And my two `layout.py`
citations in the third bullet were wrong on the day I wrote them — `:142` and `:160` were the `Stat`
and `Reference` docstrings, and `_imagery` had already read "Choose what to display, then embed only
that" since `e3dbca9`. The encode-only-what-you-place saving was never missing; see the correction in
§"What shipped, and where it deviated".)*

## Which `core/models.py` fields would have to be added, and why

I am not editing `core/`. This section exists so you can decide whether to take any of it to the
Architect. I have tried hard to keep the list minimal, and **three** of the six templates need
**nothing**: `stat_grid`, `process_flow`, `ranked_list`. Those three shipped renderable; the other
three are registered and blocked. *(An earlier revision of this section said "four", and so did
question 3 in §"The ask". There was never a fourth — the list below has three templates in it plus
two non-template entries, "Selection input" and "Image slots", and I miscounted my own bullets. The
count is load-bearing, because "ship the four that need nothing" reads as a complete v1 and "ship
the three that need nothing" reads as half a registry, which is the honest description.)*

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
- **Image slots** — `ImageRole` already has four members and `layout.py` reads only `HERO`, in
  `_hero_index` at `:682`. `ICON` and `BACKGROUND` are what `quote_spotlight` and icon-bearing
  layouts want. Use them; do not invent a parallel role concept. *(They are now named in
  `registry.py:97` and `:108` respectively, and `imagery/panda.py:55` tags one fixture `BACKGROUND`
  — but naming them in a spec is all that happens: see the `image_roles` note under §"Shape 1".)*

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

If the Architect declines all three, you still get **three** working templates and the selection
machinery, and `timeline`/`comparison`/`quote_spotlight` simply never get chosen — the registry
degrades to the templates whose data it can actually fill. That is a reasonable v1 and a reason
to build selection before asking for fields. It is also, verbatim, what shipped.

## The ask to the Architect — three additive core fields

**To: the Architect.** One decidable question, and §"Which `core/models.py` fields would have to be
added" is the argument for it; this section is only the ask. Three additions to `core/models.py`:

- `Fact.when: str | None = None`
- `Quote` (`text`, `speaker`, `context`, `source`, all but `text` defaulted) and
  `ResearchContent.quotes: Sequence[Quote] = ()`
- `ComparisonPair` (`dimension`, `left_value`, `right_value`, plus defaulted `left_label`,
  `right_label`, `unit`, `source`) and `ResearchContent.comparisons: Sequence[ComparisonPair] = ()`

All three are additive and defaulted, so a frozen slotted dataclass takes them without breaking a
single existing construction site: every existing test and the panda stub keep passing untouched.
Each unblocks exactly one template — `Fact.when` → `timeline`, `Quote` → `quote_spotlight`,
`ComparisonPair` → `comparison`.

**Nothing is blocked on this decision.** All three are already registered in `registry.py` with
their `selection_hint` and a `blocked_on` string naming the missing field, and the selector is
structurally incapable of choosing one: `choose_template` only ever returns literals from
`RENDERABLE_TEMPLATE_IDS`, `resolve_choice` and `build_page_for` both re-check, and the LLM
selector's response schema types `template_id` as a `Literal` over the renderable ids only. The
registry degrades to what it can render, and three of six templates ship working.

On a **yes**, the three bodies and templates get written; unblocking is then a `blocked_on = None`
plus a body builder in `layout._BUILDERS` and a `.j2`/`.css` pair. On a **no**, the three entries
stay registered and unreachable, carrying their reasoning in `blocked_on` where the next reader will
find it. That is a stable end state, not debt, and it is an acceptable outcome — say no if the
answer is no.

**The one thing that must not happen is a yes that stops at the field.** Adding `Fact.when` without
writing the `timeline` body leaves `registry.py:66-70` asserting *"needs `Fact.when: str | None` --
there is nowhere to put an event date"* about a field that now exists, and **no test catches it.**
Both registry fences key off `blocked_on` itself, not off its prose: `RENDERABLE_TEMPLATE_IDS` is
`frozenset(spec.id for spec in _SPECS if spec.blocked_on is None)` (`registry.py:135-137`), and
`test_the_builder_table_covers_exactly_the_renderable_templates` asserts
`set(_BUILDERS) == set(RENDERABLE_TEMPLATE_IDS)` (`tests/test_selection.py:196-201`). Both stay green
while the string lies, because a `blocked_on` that names a field which exists is still a non-`None`
string. Nothing type-checks prose. So: **take the three asks one at a time, each paired with the
commit that unblocks its template**, and if a field lands ahead of its body, the same commit should
rewrite `blocked_on` to say what is *actually* missing. Verbatim, the three strings that would need
to change are at `registry.py:66-70` (`timeline`), `:82-86` (`comparison`) and `:109-113`
(`quote_spotlight`).

Two things this work did **not** resolve, and neither is yours to decide — both need a human:
whether a PNG composed from the two `CC-BY-SA-4.0` panda images inherits ShareAlike (parked in
`CLAUDE.md` and `docs/plan.md`), and the `quote_spotlight` legibility problem — a `BACKGROUND`
image carries full attribution obligations, and attribution over a busy photo is exactly where
"visible in the rendered output" quietly stops being true. The second is a reason to answer the
`Quote` question and the legibility question together.

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
declared dependency on `main` (`ce39001`, `pyproject.toml:13`), and both
`CLAUDE.md` and that commit message scope it to one job: `client.messages.parse(...,
output_format=...)` structured output. Call 1 and call 2 are structured-output calls, so the two
response schemas would be pydantic models — **converted to frozen dataclasses at the boundary,
with nothing downstream of the parse seeing pydantic.** That is the repo's own stated position
rather than a reintroduction of the branch's approach, and it asks for no new dependency. If you
would rather the boundary schemas be hand-rolled too, say so; it changes one module.

*(Two corrections. The line was `pyproject.toml:13`, not `:12` — `:12` is `playwright>=1.61.0`. And
"nothing imports it yet", which this sentence used to say, is now doubly stale:
`composition/agent_composer.py:45` imports it as planned, and `research/agent.py:69` imports it
independently over in the text zone. That second importer is another zone's call, and it makes the
same bet this document made, which is mild evidence the bet was the repo's and not just mine.)*

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

*(That requirement held: `cli.py`, `pipeline.py`, `render/`, `core/` and `pyproject.toml` are all
still untouched by this work. What the tree above missed is `templates/css/` — five `{% include %}`d
partials rather than a `<style>` block per body. See §"What actually shipped".)*

### What this replaces

`ports.py:103-105` plans an agentic composer that chooses a layout and *emits HTML*.
`docs/plan.md` parks a compose→render→critique loop as out of scope for v1. This proposal is a
deterministic middle: the model picks from six hand-written, hand-tested layouts and fills their
slots, but never writes markup — so `autoescape`, self-containment and attribution stay
structurally guaranteed by templates you wrote, instead of being things a model has to remember
each time. If you would rather go straight to the emit-HTML agent, this proposal should be
dropped rather than built alongside it. Please pick one.

*(Nobody picked, and both got built. That was the wrong framing on my part — it assumed the two were
substitutes, and they are not: `AgentComposer` is not the emit-HTML agent, it selects and maps into
templates somebody wrote and never emits markup. So the either/or was between the registry and a
thing that still does not exist. §"Open questions, decided" answers what the end state should be, and
the answer is not "delete one".)*

### Two licensing notes I would not want to discover late

- **`quote_spotlight` puts an image behind text.** A `BACKGROUND` image still carries full
  attribution obligations, and attribution over a busy photo is exactly where "visible in the
  rendered output" quietly stops being true. It needs a legibility treatment, not just a slot.
- **`docs/plan.md` and `CLAUDE.md` both park the ShareAlike question** — whether a PNG composed
  from the two `CC-BY-SA-4.0` panda images inherits ShareAlike. Six templates multiply the
  compositions that question applies to; it still needs a human, and it is not mine to answer.

## What shipped, and where it deviated

Where the four commits departed from what is proposed above, so the rest of the document can be
trusted. The five fence commits after them corrected two of these bullets, and those corrections are
marked:

- **`infographic.html.j2` was deleted, not kept as an alias.** All 496 lines of it, in `4ba3a4d`.
  `composer.TEMPLATE_NAME` is now `stat_grid.html.j2` (`composer.py:27`), extending `_base.html.j2`,
  and no pre-existing test changed. `stat_grid` output was verified byte-identical to the old
  template's across twelve cases — both themes, zero through five images, a fact list crossing the row
  break, fixed-height and full-page, RTL, empty content. **Correction: that twelve-case check was a
  one-off and left no standing test.** Nothing in `tests/` pins twelve cases, and nothing could pin
  them now — the old template is deleted, so there is no longer anything to compare against. What
  stands today is three single-case current-vs-current identities:
  `test_default_and_explicit_stat_grid_render_identical_html` (`tests/test_chrome_split.py:64`),
  `test_composing_stat_grid_by_id_is_byte_identical_to_the_default`
  (`tests/test_template_bodies.py:384`), and a view-model equality at `:379`. If you want the
  twelve-case sweep to be a fence rather than an anecdote, it has to be rebuilt as a sweep over the
  *current* renderer, which is a different and weaker claim. Worth knowing before anyone leans on the
  sentence I wrote.
- **Per-template CSS lives in `templates/css/*.css`** and is pulled into the single inline `<style>`
  with Jinja `{% include %}` through the one autoescaped environment — deliberately not
  `Path.read_text` plus `|safe`, which would have put a second escaping story in the package.
- **`chrome.credits` is keyed off the figures the body actually places** — `layout._credits_of`
  (`:384-398`) over `_figures_of` (`:401-410`), wired into chrome at `:377`, with `_credit` itself at
  `:706-715`. As proposed.
- **The companion idea was already true, and I said the opposite.** This bullet used to claim assets
  are "still encoded eagerly" and that the render-time saving was "not realised". Wrong on the day it
  was written. `_imagery` (`layout.py:659-670`) has read *"Choose what to display, then embed only
  that"* since `e3dbca9`: it splits the hero, slices to `_BAND_CAPACITY` (`= 3`, `layout.py:57`), and
  only then maps `_figure` — so `to_data_uri` (the single call site, `layout.py:698`) sees at most four
  assets on `stat_grid` no matter how many arrive, and `build_page`'s docstring now says so at
  `:287-288`. **What is genuinely unresolved is different and smaller:** the encode is still what
  *proves* readability, so an unreadable asset among the four it does place raises, and
  `_readable_figures` — the two new bodies' path — has no cap at all and encodes every readable asset.
  That residual asymmetry is deliberate: closing it means capping the new bodies' figure count, which
  is a layout change wearing error-handling clothes. Leave it.
- **Graceful image degradation applies to the new bodies only.** `build_process_flow_page` and
  `build_ranked_list_page` go through `_readable_figures`, which skips an unreadable asset with a
  logged warning — never a placeholder, and therefore never credited either. `build_page` /
  `stat_grid` still propagates `OSError`. The asymmetry is deliberate and documented in
  `build_page`'s docstring: the default path is what `ports.py` and the test pin, while a layout
  chosen for a *shape* should not lose the whole page to one bad file. Making the two consistent is
  a `ports.py` conversation.
- **`HtmlComposer` gained a keyword-only `template_id`**, which picks both the body builder and the
  template file and degrades to `stat_grid` on an unknown or blocked id. `template_name` stays the
  raw escape hatch.
- **`AgentComposer` is opt-in, unwired, and not even exported.** Neither `pipeline.py` nor `cli.py`
  references it; `cli.py:16` imports `HtmlComposer` and `cli.py:139` constructs `HtmlComposer()`; and
  `composition/__init__.py` re-exports `HtmlComposer` alone (`__all__ = ["HtmlComposer"]`), so
  `AgentComposer` is reachable only by importing its module directly. With no key and no injected
  selector or mapper it is exactly the deterministic path: `_maybe_client`
  (`agent_composer.py:683-689`) only reaches for credentials if a call is left to make,
  `default_client` (`:541-559`) returns `None` without `ANTHROPIC_API_KEY`, `_select` and `_map` then
  return `None` (`:627-628`, `:653-654`), and `apply_mapping` short-circuits at `:162-163`.
- **The `ports.py:103-105` overlap question in §"What this replaces" is now answered** in §"Open
  questions, decided". Short version: both are on `main`, neither should be deleted, and the emit-HTML
  agent `ports.py` describes is the thing that should *not* get built.

### The ask

1. Do you want a registry at all, or is the emit-HTML agent the plan?
2. If yes: is the `PageChrome` / per-template-body split the right shape, given it touches a file
   you already shipped with substantial test coverage?
3. Should the three core fields go to the Architect, or should v1 ship the three templates that
   need nothing?

I have not touched `core/`, `pyproject.toml`, `composition/` or `render/`. Happy to implement
whatever survives this, in your zone, to your design.

### The ask, answered

Written after ten commits, so these are answers with evidence rather than preferences. `core/`,
`pyproject.toml`, `render/`, `pipeline.py` and `cli.py` are still untouched by any of it.

**1. The registry, and the emit-HTML agent should not be built.** The registry is the plan, and the
question was mis-framed as an either/or — see the correction under §"What this replaces". What
`ports.py:103-105` describes is an agent that "emits the HTML and CSS", and that is the one part of the
plan the registry deliberately declines: a single autoescaped `Environment`, `StrictUndefined`, credits
derived from placed figures, four embedded woff2 faces and zero external requests are all guarantees
that hold because a human wrote the markup once. A model that re-emits markup each
render has to *remember* every one of them, and nothing in the test suite could tell you the run it
forgot. So: keep the registry, keep `AgentComposer` selecting and mapping into it, and let
`ports.py:103-105` be the sentence that goes stale. Rewriting it is a `ports.py` conversation and
therefore the Architect's, not a cleanup somebody does in passing.

**2. Yes, the split is right, and I would argue against taking it back out.** It landed in `4ba3a4d` at
a real cost — 496 lines of template deleted, 9 files, +806/−512 — but the cost was paid once and it
bought a structural property that a wide `Page` cannot have: `PageBody` is a `TypeAlias` union
(`layout.py:257`) discriminated by `assert_never` (`:410`, in `_figures_of`), so a body arm nobody handles is a
`mypy --strict` error rather than a `StrictUndefined` surprise at render time, and
`set(_BUILDERS) == set(RENDERABLE_TEMPLATE_IDS)` is a test rather than a convention. The evidence that
the substantial test coverage survived: no pre-existing test changed in `4ba3a4d`, and `main` is at 705
passed, 1 skipped, with `mypy --strict` clean over 23 source files. The honest caveat is in answer to the next
question about scaling — the union is right, and the *arms* have started repeating themselves.

**3. Take all three to the Architect, one at a time, each paired with the body that unblocks it — and
v1 already shipped the three that need nothing.** The count in the original question was wrong (three,
not four; see §"Which `core/models.py` fields"), which matters because "ship the four that need nothing"
sounds like a finished v1 and three-of-six is visibly half a registry. But shipping the half was still
right: it proved the selection machinery, the chrome split and the fence surface with zero `core/`
change, so the ask now arrives with working code behind it instead of a promise. The one hard
constraint is in §"The ask to the Architect": **a field that lands without its template leaves
`blocked_on` lying and no test catches it**, because every registry fence keys off whether `blocked_on`
is `None`, not off whether its prose is still true. Sequence them, or say no — a no is a stable end
state, not debt.

## What actually shipped

Verified against `main` at **`8863713`**; `composition/` is unchanged since `1d6e04c`, and every
`file:line` below is as of that tree. **Ten** commits touched this work: four that built it, one that
put the new bodies on the ramp, and five that fenced it. *(An earlier revision of this line said nine
and skipped `f320e12` — see the last bullet.)*

Two cautions for whoever reads this next. Another session is working in this checkout, so
`composition/layout.py` and four CSS partials were dirty in the working tree while this section was
written; the citations here are to **committed** `main`, deliberately, and a read of the working tree
will show some of them shifted. And `8863713` landed `research/agent.py`, which is why the test count
below is bigger than the one this document quoted an hour earlier.

- **A six-entry registry**, `TEMPLATE_REGISTRY` (`registry.py:129-131`) as a `MappingProxyType` over a
  `_SPECS` tuple (`:45-127`), in catalogue order `stat_grid`, `timeline`, `comparison`, `process_flow`,
  `quote_spotlight`, `ranked_list`. **Three renderable, three `blocked_on`**, each blocked entry naming
  the `core/models.py` field it wants: `timeline` → `Fact.when` (`:66-70`), `comparison` →
  `ComparisonPair` + `ResearchContent.comparisons` (`:82-86`), `quote_spotlight` → `Quote` +
  `ResearchContent.quotes` (`:109-113`). None of the three exists in `core/models.py`; `Fact`
  (`models.py:98-107`) is still `label, value, unit, detail, source`, and `ResearchContent`
  (`:119-132`) is still `title, subtitle, summary, facts, sections, keywords, sources`.
- **`RENDERABLE_TEMPLATE_IDS` is derived, not hand-listed** —
  `frozenset(spec.id for spec in _SPECS if spec.blocked_on is None)` (`registry.py:135-137`). One
  source of truth, so unblocking a template is a one-word edit and cannot desynchronise from a second
  list. `TemplateSpec` is `@dataclass(frozen=True, slots=True)` with six fields, `blocked_on` last.
- **`selection.py` is pure, synchronous, and contains zero `raise` statements.** `resolve_choice`
  (`:210-227`) resolves in exactly one order: **explicit override → a trusted `TemplateChoice` at
  `confidence >= 0.55` → the rule table → the `stat_grid` floor**. `MIN_CONFIDENCE` is `:50`,
  `_DEFAULT_TEMPLATE_ID = "stat_grid"` is `:53`, and every subscript is either renderability-guarded or
  a `.get` with the floor as its default, so `KeyError` is unreachable rather than merely unlikely. One
  honest caveat on that: the `.get`'s default is itself an unguarded
  `TEMPLATE_REGISTRY[_DEFAULT_TEMPLATE_ID]` (`:226`), safe only because `"stat_grid"` is a literal that
  happens to be in `_SPECS`, and nothing pins that coupling. Unreachable, but for one reason more than
  the code admits.
  `choose_template` (`:143-182`) and `_tiebreak` (`:185-207`) return hard literals only, pinned by a
  census sweep at `tests/test_selection.py:229-232`.
- **`PageChrome` + `PageBody`.** `Page` (`layout.py:261-266`) is two fields. `PageChrome`
  (`:177-194`) is 14, having gained `fonts` and `title_fit` since this document proposed 12.
  `PageBody` (`:257`) is `StatGridBody | ProcessFlowBody | RankedListBody`, with `assert_never` at
  `:410` inside `_figures_of` (`:401-410`). `_BUILDERS` (`:336-342`) maps template id to builder;
  `build_page_for` (`:345-360`)
  dispatches; `build_page` (`:274-297`) is the `stat_grid` arm and is now 24 lines, 13 of them
  docstring.
- **CSS as `{% include %}`d partials.** Five files under `templates/css/` — `_chrome.css`,
  `_chrome_apparatus.css`, and one per renderable body — pulled into the single `<style>` element
  (`_base.html.j2:20-24`) through the one autoescaped environment, with each body filling the
  `body_css` block. Deliberately not `Path.read_text` plus `|safe`: **zero occurrences of either
  anywhere under `composition/`**. One consequence to keep in mind, flagged in `_base.html.j2:8-13`:
  the partials are Jinja-processed and autoescape escapes for HTML, *not* for CSS.
- **The ramp, and the fences that have landed.** `f320e12` put the two new bodies on the ramp and
  added the `--fit` cap. **`--fit` is not a CLI flag** — there is no such option in `build_parser`
  (`cli.py:29-109`); it is a CSS custom property computed by `layout._fit` (`:514-522`), emitted as
  `style="--fit: …"` by `_base.html.j2:31` and each body, and consumed at `css/_chrome.css:109` as
  `.fitted { font-size: min(var(--size), var(--fit)); }`. What it caps is the rendered font size of a
  title or value, clamped to the width its container can actually hold, so the character-count-derived
  `Scale` ceiling can never overflow a narrow column. Then: `6e473b3` added registry-integrity tests
  (every renderable template's file exists and compiles; `_BUILDERS` ≡ `RENDERABLE_TEMPLATE_IDS`),
  lifted `build_environment` to module level, and repaired the `image_roles` docstring. `55bbfa6` put
  `overflow-wrap: anywhere` on `.title` (`css/_chrome.css:143`). `7f2df66` widened `_TITLE_ADVANCE`
  (`layout.py:65`) from 0.50 to 0.60 against a measured realistic-word maximum of 0.5907. `dab1ded`
  widened five browser fences to cross all three renderable bodies × both themes — 54 cells through a
  frozen `BODY_SELECTORS` table, module 46 → 94 tests. `1d6e04c` stopped two query containers
  collapsing (`columns: 3 260px`, `.rank__figure: 0 0 37.5%`) and replaced an overlap-clustering line
  counter that reported 1 line for a visibly four-line headline. **The two `stat_grid` identity fences
  stayed green throughout — which is not the same as `stat_grid`'s bytes being unchanged, and they
  are not:** `55bbfa6` edits a partial `stat_grid` includes, and `7f2df66` feeds `chrome.title_fit`
  through `_fit`, so both move rendered output. Anyone reaching for a golden should read that twice.
- **705 tests pass and 1 skips** (the live-network one); 451 of those are everything outside the
  research-agent module `8863713` just landed. `mypy --strict src` is clean over 23 source files.

## Open questions, decided

### Silent degradation: an ignored override should be reported, but never raised

`resolve_choice` never raises and floors at `stat_grid`, and `template_override_of`
(`selection.py:128-135`) is a one-liner — `return requested if is_renderable(requested) else None` — so
`extras["composition.template"] = "timelien"` produces a stat grid and says nothing to anybody.

**Decision: the caller needs to know, and the right mechanism is a return value — not a raise, and not
a log.** Three reasons, in order of weight.

Not a raise, because `models.py:82-84` documents "ignore keys outside your namespace and never raise on
an unknown key" for the `composition.*` namespace and `ports.py` forbids `compose()` failing on a
garbled hint. A typo'd override must not cost a render. That part of the current behaviour is correct
and stays.

Not a log, and this is the argument that actually decides it: **there is no logging configuration
anywhere in `src/` or `tests/`.** A `logging.warning` in this repo reaches `logging.lastResort` —
unformatted, on stderr, easy to miss — which is precisely how the `OSError` swallow in
`_readable_figures` managed to drop an unreadable hero, silently promote a `SUPPORTING` asset into the
hero slot, print a normal CLI summary and exit `0`. Adding a second invisible warning to fix a silent
failure would be repeating the mistake in the same module.

So: `resolve_choice` should return the chosen spec **and why** — the template plus a small closed set of
reasons, one of which is "you asked for an id I do not have". That is testable, it forces the caller to
decide what to do rather than letting the information evaporate, and it costs one type inside
`composition/` and zero `core/` change.

**Does the fourth renderable template change the answer? It is what makes the answer necessary.** With
three, `stat_grid` is a defensible answer to almost any input and the floor is nearly always what you
wanted anyway. With four or more, a mis-typed `timeline` silently becomes a stat grid while `ranked_list`
and `process_flow` sit there unconsidered — the floor stops being a graceful degradation and starts
being an arbitrary one. Note also what happens *today* on a **blocked** id: `is_renderable("timeline")`
is `False`, so asking for a registered-but-blocked template is indistinguishable from asking for a
typo, and both are indistinguishable from asking for nothing. Those are three different situations and
a caller can act differently on each.

It costs nothing today, because nothing writes `extras` at all (`cli.py:121` builds its `Brief` without
them). **The right moment to land it is the commit that adds the fourth renderable template**, and the
fence is a test that a typo'd override is reported rather than merely survived. This is a decision
recorded, not a behaviour change made.

### The `PageBody` union scales to six. Its arms do not.

**The union itself: yes, six arms is fine, and the mechanism gets *better* with more of them.**
`PageBody` is a `TypeAlias` (`layout.py:257`) discriminated by `assert_never` (`:410`, in `_figures_of`), so adding an
arm without handling it everywhere is a `mypy --strict` failure at the point of the omission — a
compile error whose cost is constant per arm, not quadratic. Pair that with
`set(_BUILDERS) == set(RENDERABLE_TEMPLATE_IDS)` and a registry whose renderable set is derived from
`blocked_on`, and the bookkeeping for a sixth arm is: one dataclass, one builder, one `.j2`, one `.css`,
one registry line changed from a string to `None`. Nothing has to be edited in six places. Contrast the
alternative this split replaced — a wide `Page` where every template can reference every field and only
the tests tell you which are populated — and the union is not close to being the weaker option.

**What does not scale is the arms sharing a copy-pasted spine.** At three arms this is already visible:
every `PageBody` variant carries `hero: Figure | None` and all three do the same thing with it, the hero
markup and lede are duplicated verbatim across all three body templates, and `Rank` is `Stat` minus
`feature`/`full_width` plus `ordinal` — with the scale-threshold tuple `(7, 13, 20)` written out twice.
Six arms means six copies of that, and the failure mode is not a type error, it is a fix applied to four
of six bodies.

**So, what I would do — and it is not widening `Page` back out.** Hoist the shared spine into something
the arms *embed* rather than each redeclare: a small `BodyCommon` (hero, lede) held by each arm, and a
`Stat`/`Rank` convergence that puts the threshold tuple in one place. Prefer that over moving `hero` up
into `PageChrome`, even though all three current arms would allow it: `quote_spotlight` wants a
`BACKGROUND` image and not a hero figure at all, so a chrome-level `hero` would be a field two of six
bodies carry and never place — which is the wide-`Page` disease reintroduced one field at a time. The
duplication in the templates is the cheaper half: Jinja block defaults in `_base.html.j2` remove it
without touching a type.

These two options were genuinely close, because `hero` really is identical across everything renderable
today. **The tiebreaker is the blocked entries** — `quote_spotlight`'s `image_roles` is `(BACKGROUND,)`,
so the arm that would break a chrome-level `hero` is already written down in the registry, and designing
around three arms when six are catalogued is how a union that scales stops scaling.

### The `OSError` asymmetry, and the direction that is *not* a cleanup

`ports.py:100-101` promises "`OSError` if an asset backed by a `Path` is unreadable". `build_page` keeps
that promise. `build_process_flow_page` and `build_ranked_list_page` go through `_readable_figures`,
which catches `OSError` at `layout.py:431-440`, logs a warning naming the mime type and role — **not the
path** — and continues. Both sides are now pinned by tests, on purpose:
`tests/test_composition.py:1228-1232` for `stat_grid` and `tests/test_template_bodies.py:237-244` for the
two new bodies, whose docstring reads *"The asymmetry is deliberate -- see the note on `build_page`."*

**Reconciling it by making the new bodies raise is the direction that is specified, sequenced as its
own commit, and not landed as of `8863713`** — `_readable_figures` collapses to a three-line
`_all_figures`, `_LOG` and `import logging` go with it, and `ports.py` is not edited. Nothing in the
history implements it yet; if you grep for it and find nothing, that is the accurate state and not a
missing commit. Recording the rest here because the *other* direction keeps looking like the tidier one
and is not:

- **Making `build_page` swallow is a `ports.py` change, and therefore a conversation, not a cleanup.**
  `ports.py:100-101` is a documented promise and `test_missing_path_backed_asset_raises_oserror` pins
  it. Deleting a promise from a port that three zones code against is not something zone 3 does on the
  way past.
- **The port arguably permits both readings, which is exactly why it needs a human and not a judgement
  call.** `ports.py:88-89` says "`images` may be empty -- produce a text-only layout, never raise", and
  `:92-93` says "You may use a subset of `images`, but embed and credit exactly the ones you display".
  Read together, those are a licence to display fewer images than you were handed and never fail — which
  is precisely what the swallow does. The counter-reading, and the one the in-flight fix takes, is that
  subsetting is permission to choose a *layout*, not permission to lose a *file* silently. Both readings
  are available in the text as written. That is a defect in the port, not a disagreement about the code.
- **`TODO:`** — **`cli.py:158` catches `OSError` and mislabels it.** Verbatim, `cli.py:158-159` is
  `except OSError as exc:` / `return _fail(f"cannot write {output_path}: {exc.strerror or exc}", EXIT_FAILED)`
  — inside a `try` opened at `:156-157` around the whole pipeline run — and `_fail` (`:166-168`) prefixes
  `error: `. So an unreadable *input* asset is reported as a failure to
  write the *output* PNG. **This is already wrong today** on `stat_grid`, before anything changes;
  widening the raise to all three bodies widens the mislabel to all three. `cli.py` is the shared
  no-owner zone, so this TODO lives here rather than in the file — whoever picks it up should split the
  handler so an asset read failure says what it actually was, and mention it before editing `cli.py`.

One consequence the in-flight fix accepts rather than solves: `AgentComposer.compose` delegates at
`agent_composer.py:621-623` outside any `try`, and it is the one caller that routinely picks a
non-`stat_grid` template, so making the new bodies raise turns a degraded-but-shipped page into a hard
failure exactly there. Its docstring's promise — "never raises because a model misbehaved" — survives
intact: an unreadable file is the file system misbehaving, not the model.

### The registry and the agentic composer: a ramp, not a rivalry

I asked you to pick one and said the loser should be dropped rather than built alongside. Both shipped,
and **deleting either one is out of bounds** — that is not the answer, and it was never the answer to a
question I had framed wrong.

**The right end state is a ramp with the deterministic path as its floor.** `HtmlComposer` stays the
default and the only composer `cli.py` constructs; it is what the offline suite exercises, and it is
literally what `AgentComposer` becomes without a key. `AgentComposer` stays opt-in and stays out of
`composition/__init__.py`'s `__all__` until there is evidence it beats the rule table — not because it
is suspect, but because an export is a promise to callers and there is nothing yet to promise. The floor
is the invariant: whatever the model does, the worst case is the page `HtmlComposer` would have made.

**What evidence would settle it.** The cheap experiment first, because it needs no renders: the selection
call already returns a `template_id`, a confidence and a rationale, so log both picks — the model's and
`choose_template`'s — over a corpus of briefs and diff them. Three outcomes, three different answers.
If they agree, the rule table wins on cost and latency and `AgentComposer` stays a research path.
If they disagree and the model's picks are better — judged by a human on a sample, and cross-checked by
the fences that already exist for contrast and overflow rather than by taste alone — then promote it
behind an explicit `--composer` flag, which is a `cli.py` conversation and needs the shared zone's
consent. If they disagree and the model's picks are *worse*, the interesting artifact is the disagreement
set, because every case in it is a rule the table is missing.

Two things worth knowing before running that experiment. The model's view of the registry is almost
entirely `selection_hint`: `selection_prompt` (`agent_composer.py:266-269`) shows it `id` and
`selection_hint` and nothing else, so the experiment measures the hints as much as the model. And with
three renderable templates the rule table is a small target — the comparison gets much more interesting
at six, which is another reason to sequence the core-field asks rather than sit on them.

**The actual duplication risk is not these two.** It is the emit-HTML agent from `ports.py:103-105` being
built as a third thing by somebody who reads that docstring and not this document. See answer 1 in
§"The ask, answered".

## Fences considered, and the one that was rejected

Recorded so nobody re-derives either of these from first principles. Both were measured, not reasoned
about.

### A perceptual-hash visual-regression fence does not work here. Do not re-propose it.

The idea is obvious and wrong: render each template, hash the PNG, commit the hash, fail on a Hamming
distance. Measured under `imagery/prepare.py`'s existing convention — 64-bit average hash, greyscale,
LANCZOS to 8×8, threshold each pixel against the image's own mean:

- **Noise floor: 0.** Two identical-input renders, six template×theme cells, distance 0 in all six.
  Not "small" — zero.
- **Every realistic perturbation also measured 0.** An `--accent-paper` nudge ~8% lighter (9,619 pixels
  changed, peak channel delta 9): **0**. A background hue swap touching **41.35%** of the page's pixels:
  **0**. A body-text colour swap (34,067 pixels, peak delta 48): **0**. The credit line turned red
  (8,267 pixels, peak delta 135): **0**. A body `font-size` change of 16.5 → 18px, reflowing the page
  from 2347 to 2357 pixels tall: **0**. DARK repeats of the first three: 0 for all three.
- **The control that ends the argument: `stat_grid`/DARK versus `ranked_list`/DARK — two entirely
  different templates — measured 3.** A real palette regression is 0 bits away from clean; two different
  layouts are 3 bits apart. The signal band sits inside the noise band. Noise floor 0, smallest real
  change 0, gap 0: **no valid threshold exists.** A threshold of 0 catches nothing; anything ≥1 sleeps
  through swapping one template for another.
- **Sides 16 and 32 do not fix it.** The accent nudge, the body-text swap and the credit-colour change
  are all still 0 at a32. Only gross changes register — background to mid-grey is 9/35/137, background
  to black is 46/172/640.

The cause is structural, not tunable: **aHash discards colour** (it converts to greyscale) **and
thresholds against the image's own mean** (so global brightness and contrast shifts cancel). A palette
regression is the exact class of change average-hash is *designed* to be invariant to. No amount of
resolution fixes an invariance.

**This is not a criticism of `prepare.fingerprint`, which is correct for its actual job.** Its job is
spotting the same photograph after a re-encode, and it does that well — distinct seeds measure more than
6 bits apart while a 1600×1200 q95 and a 400×300 q60 of the same seed stay within 6, which is what
`wikimedia.near_duplicate_distance = 6` is calibrated against. It is a *near-duplicate detector* being
misread as a *change detector*. Those are opposite requirements, and the function is right for the one
it was written for. Do not repurpose it, and do not add a Hamming tolerance to make it fit.

**What is proposed instead — and has NOT landed.** The replacement is 18 hex assertions on sampled
computed colours, written as literals in a test: three templates × two themes × three targets, at
`dsf=1.0` with `images=()`,
with **exact equality and no tolerance**, since even ±1 swallows the `--accent-paper` nudge that is the
whole reason the fence exists. The frozen table is `.rule` → `--accent-paper` (`#5c6a12` / `#bccb4e`),
`.masthead` → `--patch` (`#17150f` / `#eceae2`), `.apparatus` → `--paper` showing through (`#eceae2` /
`#14130d`), and the sampling was verified scale-invariant across all 36 points with a 5×5 device-pixel
uniformity guard as a checked precondition.

**None of it is in the repository.** Grepped: not one of those six hex literals appears anywhere under
`tests/`, and no test reads a colour out of `getComputedStyle` — the four `getComputedStyle` sites in
`tests/test_composition.py` read `textTransform`, `fontSize`, `--size` and `containerType`. So the
honest state is: the phash fence is *rejected with measurements*, and the colour fence that should
replace it is *specified and not yet written*. Do not read this section as "we already have a colour
fence"; read it as "the obvious fence is a dead end, and here is the one to build instead."

Two traps the spec records for whoever writes it. With `images=()` the `.colophon` element **does not
exist**, because credits derive from placed figures — verified `null` in all six cells. And `body`
sampled at mid-height reads `#17150f` (`--patch`) rather than `--paper` in `stat_grid`/LIGHT and
`ranked_list`/LIGHT, because the midpoint lands on an `on-ink` panel.

### Byte-exact goldens are macOS-local. The OS is the risk, not chromium and not the fonts.

Byte-exactness holds remarkably well *here*: all six template×theme cells are byte-identical across
separate chromium launches and separate Python processes hours apart, `HtmlComposer.compose` is
string-deterministic 6/6, and the hash is even scale-invariant — `dsf=1.0` and `dsf=2.0` produce the same
perceptual hash, 6/6.

- **Chromium build is not the risk.** Three cached `chrome-headless-shell` builds — **1223, 1228 and
  1234** — rendered the same composition **byte-identical**, same sha256 in all three.
- **The fonts are not the risk.** `layout.font_faces()` (`layout.py:461-476`) inlines four bundled woff2
  files as data URIs, and `css/_chrome.css` names only "Ledger Slab", "Ledger Text" and "Ledger Mono"
  plus CSS generics. Zero system-font dependence, enforced by test.
- **The OS is the risk, and it has a name.** `-webkit-font-smoothing: antialiased` at
  `css/_chrome.css:79` is a CoreText-only declaration. Removing it moves **62,541 pixels — 2.22% of the
  page, peak channel delta 188** — and changes the sha256.

**Consequence for CI:** a committed byte-exact golden is a laptop artifact. Off this laptop it is
approximately 100% false positives, and for the palette-regression class it was supposed to catch it is
0% true positives — the class that motivated goldens is the class the hash cannot see anyway. The
storage is the smaller objection and still an objection: six cells at the `RenderOptions` default is
5,611,870 bytes each — about **33.6 MB** of binary in a repository that currently has zero committed
`.png` files. **Do not commit goldens.** Sampled colour assertions are the portable half of the same
idea, because a computed colour is a value and not a rasterisation — see the section above for why that
fence is specified rather than shipped.

The honest gap: the actual Linux delta was never measured — the Docker daemon was not running. The
experiment that would settle it is named and small: render `stat_grid`/LIGHT with `images=()` at
`dsf=1.0` inside `mcr.microsoft.com/playwright/python:v1.61.0-noble` and diff against the local sha.
Until someone runs it, the conclusion above rests on mechanism rather than measurement.

**And a correction, because the obvious reason to keep that declaration is not the real one.**
`-webkit-font-smoothing: antialiased` does **not** suppress subpixel colour fringing here. Measured
`#000` on `#fff` at 16.5px serif in headless chromium across all four smoothing modes — absent,
`antialiased`, `subpixel-antialiased`, `none` — at both `dsf=1.0` and `dsf=2.0`, counting pixels whose
R/G/B are not all equal: **zero chromatic pixels in every mode at both scale factors.** macOS headless
chromium rasterises to a non-LCD surface and does no subpixel antialiasing at all, independently
confirmed by `subpixel-antialiased` being pixel-count-identical to no declaration — impossible if there
were an LCD path to opt into. There are no coloured fringes to suppress. What the declaration actually
does is **stem darkening**: `antialiased` renders about 11% fewer non-white pixels, so glyphs come out
lighter and thinner. That is the entire 62,541-pixel delta, and it is pure luminance. **Keep it — but
keep it as a typographic choice, not a portability one.** The reasoning matters because the plausible
version of it is false, and a comment repeating it would have written a falsehood into `_chrome.css`.

## Findings worth keeping

Two things that cost real time to discover and will cost it again.

- **`TemplateSpec.image_roles` enforces nothing.** Covered under §"Shape 1" with the measurements. The
  short version for anyone skimming: it is read once, as prose, in a model prompt, and every readable
  asset is placed whatever role it carries. Do not build a feature on the assumption that it filters.
- **`cqw` resolves against the query container's *content* box, not its track.** This invalidated several
  pixel predictions during the fence work. A predicted 12.2px minimum title size measured 6.55px,
  because 6.55px is `3.00cqw` of `.masthead__text`'s **218.39px content box** and not of the 294.39px
  grid track it sits in — the gutter padding takes 294 down to 218. The predictions that were *counts*
  reproduced exactly; every prediction that was a *pixel value* was wrong by the padding. If you are
  reasoning about container query units on paper, subtract the padding first, and expect to be wrong
  until you have measured it in the browser.

## The one question nobody here gets to answer

**Whether a PNG composed from the two `CC-BY-SA-4.0` panda images inherits ShareAlike is unresolved, it
is Hiren's, and nothing in this document depends on the answer.**

`CLAUDE.md:110` parks it — "Unresolved, and it needs a human — see `docs/plan.md`" — and
`docs/plan.md:98-100` parks it too, saying only "Decide it before shipping anything publicly". Neither
resolves it and neither names an owner, so this document names one rather than adding a third parking
space. Note the cross-reference is one-directional: `CLAUDE.md` points at `docs/plan.md`, and
`docs/plan.md` does not point back or say a human is required. Worth fixing in whichever file its owner
prefers; it is not mine to edit.

Six templates multiply the number of compositions the question applies to, which raises the stakes and
changes nothing about the answer. Two related things do sit downstream of it and should be decided
together, both already flagged above: `quote_spotlight` putting a `BACKGROUND` image behind text, where
"attribution visible in the rendered output" quietly stops being true; and the measured contrast failure
on `.hero__credit`, which is a *legal* attribution line failing WCAG AA's 4.5 on **every** hero in the
pool. Two runs, two methods, so quote them separately rather than as a range: one measured **3.09:1** on
the default hero and **2.30:1** on an alternate; a later sweep across all five put the worst at
**2.63:1**. Either way nothing is close to passing, and the large-text exemption rescues none of it,
because nothing measured lands between 3.0 and 4.5. Mechanism, as of `8863713`: the scrim is only 0.40
alpha at cap height, its 0.78 stop spent on padding below the text. That one is not a licensing question
and does not need Hiren — it is a legibility bug in zone 3, and it may already be fixed by the time you
read this, because a shared `.hero__credit` gradient was in the working tree while this was written.

I have deliberately not let any recommendation in this document turn on the ShareAlike answer. If one
appears to, that is a defect in my writing — say so and I will remove the dependency, not resolve the
question.
