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
(`layout.py:850`) picks the first `HERO` and otherwise index 0. `6e473b3` repaired the docstring that
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
— at `layout.py:260-264`; `PageChrome` is 14 fields at `:176-192`, having since gained `fonts` and
`title_fit`. The template that got split was **496** lines when `4ba3a4d` deleted it, not the 432 I
wrote here: `49dc67b` grew it between my writing this and the split landing. And my two `layout.py`
citations in the third bullet were wrong on the day I wrote them — `:142` and `:160` were the `Stat`
and `Reference` docstrings, and `_imagery` had already read "Choose what to display, then embed only
that" since `49dc67b` — that same commit, not `e3dbca9`, which has no `_imagery` in it at all. The
encode-only-what-you-place saving was never missing; see the correction in §"What shipped, and where
it deviated".)*

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
  `_hero_index` at `:841`. `ICON` and `BACKGROUND` are what `quote_spotlight` and icon-bearing
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

One thing this work did **not** resolve, and it is not yours to decide: the `quote_spotlight`
legibility problem — a `BACKGROUND` image carries full attribution obligations, and attribution
over a busy photo is exactly where "visible in the rendered output" quietly stops being true. It is
a reason to answer the `Quote` question and the legibility question together. The ShareAlike
question that used to stand beside it here is closed; see §"The one question that was Hiren's, and
is now closed".

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
- **The ShareAlike question is closed, and six templates do not reopen it.** Whether a PNG composed
  from the two `CC-BY-SA-4.0` panda images inherits ShareAlike was Hiren's call and he made it: the
  Wikimedia images are test fixtures only and the sources are being replaced wholesale, so nothing
  CC-BY-SA is intended for distribution. The attribution machinery stays regardless — see
  §"The one question that was Hiren's, and is now closed".

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
  (`:435-470`) over `_figures_of` (`:472-481`), wired in by `build_chrome` (`:365`), with `_credit`
  itself at `:874`. As proposed.
- **The companion idea was already true, and I said the opposite.** This bullet used to claim assets
  are "still encoded eagerly" and that the render-time saving was "not realised". Wrong on the day it
  was written. `_imagery` (`layout.py:808-819`) has read *"Choose what to display, then embed only
  that"* since `49dc67b`, the commit that introduced the function — not `e3dbca9` eight commits earlier,
  which contains no `_imagery` at all. It splits the hero, slices to `_BAND_CAPACITY` (`= 3`,
  `layout.py:55`), and only then maps `_figure` — so `to_data_uri` (the single call site,
  `layout.py:866`) sees at most four assets on `stat_grid` no matter how many arrive, and `build_page`'s
  docstring says so at `:285-286`.
  **The one asymmetry that survives on purpose** is that `stat_grid` encodes only what it places, so an
  unreadable asset *past* the band's capacity costs nothing and raises nothing, while `_all_figures`
  encodes everything the two newer bodies are handed. Closing that would mean capping their figure
  count, which is a layout change wearing error-handling clothes. Leave it.
- **Image degradation is gone from the new bodies, and all three now raise.** This bullet used to say
  the opposite, and `fafec26` falsified it. `_readable_figures` no longer exists: it is `_all_figures`
  (`layout.py:822-838`), three lines of body, no `try`, no filter, and `_LOG` and `import logging` were
  deleted from `layout.py` outright as its only users — a grep for either returns nothing.
  `build_process_flow_page` (`:298`) and `build_ranked_list_page` (`:321`) both call it, so an
  unreadable `Path` asset now fails the page on every body rather than vanishing from one. See
  §"The `OSError` asymmetry, resolved" for the argument that settled it.
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
(`layout.py:255`) discriminated by `assert_never` (`:481`, in `_figures_of`), so a body arm nobody handles is a
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

Verified against `main` at **`1c44352`**, and every `file:line` below is a line number in
`git show HEAD:<path>` at that commit. **Nineteen** commits have touched this work: four that built it
(`0d40d6d`, `4ba3a4d`, `eed91b9`, `e6cc575`), one that put the new bodies on the ramp (`f320e12`), and
fourteen that fenced it (`6e473b3`, `55bbfa6`, `7f2df66`, `dab1ded`, `1d6e04c`, `9e2613f`, `5f829e8`,
`ffb6815`, `2afd3f2`, `937358e`, `fafec26`, `b7fc3c9`, `dd70176`, `1c44352`) — plus five cross-zone
test-hygiene commits from the other session (`41f1cfe`, `95cc997`, `570b4c6`, `d7e4c67`, `ea608d8`).
*(Earlier revisions of this line said nine and then ten. It keeps moving, which is the argument for
citing a commit rather than "now".)*

One standing caution, because it has already produced one wrong answer in this document's history: other
sessions work in this checkout, so **read committed code, not the working tree.** A validation pass that
read a dirty `layout.py` reported a phantom thirteen-line drift across every citation past line 400 and
was wrong about all of them. Every number here came from `git show HEAD:`.

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
- **`PageChrome` + `PageBody`.** `Page` (`layout.py:260-264`) is two fields. `PageChrome`
  (`:176-192`) is 14, having gained `fonts` and `title_fit` since this document proposed 12.
  `PageBody` (`:255`) is `StatGridBody | ProcessFlowBody | RankedListBody`, with `assert_never` at
  `:481` inside `_figures_of` (`:472-481`). `_BUILDERS` (`:338-344`) maps template id to builder;
  `build_page_for` (`:347-362`) dispatches and never raises on an unrecognised or blocked id;
  `build_page` (`:272-295`) is the `stat_grid` arm and is 24 lines, 13 of them docstring.
- **CSS as `{% include %}`d partials.** Five files under `templates/css/` — `_chrome.css`,
  `_chrome_apparatus.css`, and one per renderable body — pulled into the single `<style>` element
  (`_base.html.j2:20-24`) through the one autoescaped environment, with each body filling the
  `body_css` block. Deliberately not `Path.read_text` plus `|safe`: **zero occurrences of either
  anywhere under `composition/`**. One consequence to keep in mind, flagged in `_base.html.j2:8-13`:
  the partials are Jinja-processed and autoescape escapes for HTML, *not* for CSS.
- **The ramp, and the fences that have landed.** `f320e12` put the two new bodies on the ramp and
  added the `--fit` cap. **`--fit` is not a CLI flag** — there is no such option in `build_parser`
  (`cli.py:29-109`); it is a CSS custom property computed by `layout._fit` (`:550`), emitted as
  `style="--fit: …"` by `_base.html.j2:31` and each body, and consumed at `css/_chrome.css:109` as
  `.fitted { font-size: min(var(--size), var(--fit)); }`. What it caps is the rendered font size of a
  title or value, clamped to the width its container can actually hold, so the character-count-derived
  `Scale` ceiling can never overflow a narrow column. Then: `6e473b3` added registry-integrity tests
  (every renderable template's file exists and compiles; `_BUILDERS` ≡ `RENDERABLE_TEMPLATE_IDS`),
  lifted `build_environment` to module level, and repaired the `image_roles` docstring. `55bbfa6` put
  `overflow-wrap: anywhere` on `.title` (`css/_chrome.css:143`). `7f2df66` widened `_TITLE_ADVANCE`
  (`layout.py:63`) from 0.50 to 0.60 against a measured realistic-word maximum of 0.5907. `dab1ded`
  widened five browser fences to cross all three renderable bodies × both themes through a frozen
  `BODY_SELECTORS` table (`tests/test_composition.py:742`), self-checked against
  `RENDERABLE_TEMPLATE_IDS` at `:758` so a new body cannot be silently uncovered. `1d6e04c` stopped two
  query containers collapsing (`columns: 3 260px`, `.rank__figure: 0 0 37.5%`) and replaced an
  overlap-clustering line counter that reported 1 line for a visibly four-line headline. **The two
  `stat_grid` identity fences stayed green throughout — which is not the same as `stat_grid`'s bytes
  being unchanged, and they are not:** `55bbfa6` edits a partial `stat_grid` includes, and `7f2df66`
  feeds `chrome.title_fit` through `_fit`, so both move rendered output. Anyone reaching for a golden
  should read that twice.
- **The nine fences now standing**, because a list of them is the thing a newcomer to this zone most
  needs. The **registry-integrity** pair in `tests/test_selection.py`: every renderable template's file
  exists and really parses through the one environment (`:186`), and `set(_BUILDERS) ==
  set(RENDERABLE_TEMPLATE_IDS)` (`:196`). The **live-DOM matrix**, three renderable bodies × both themes,
  with both axes derived — `TEMPLATE_IDS` from `RENDERABLE_TEMPLATE_IDS`, and `THEMES` pinned
  `== set(Theme)` at `tests/test_composition.py:710` so a new `Theme` member cannot ship rendering in no
  cell. The **WCAG contrast fence** (`:3497`): every visible text node measured in the browser, ratios
  computed in Python to WCAG 2.x, with the large-text split spelled out rather than flattened onto a bare
  4.5 — **412 nodes, zero violations, tightest real pair 4.82:1, and nothing measuring inside [3.0, 4.5)**.
  It also asserts that the set of text nodes overlapping an `<img>` is *exactly* `{figcaption.hero__credit}`,
  which is how the fourth attribution site stays on the list. Its **canvas readback** (`:3965`) is the one
  place pixels are read back: `drawImage` the hero, `getImageData`, composite the scrim per pixel over the
  glyphs' **ink box** (not the line box), and assert the ratio at the *worst* band pixel — with the scrim
  parsed off the rendered page rather than modelled from source. The **palette fence**
  (`tests/test_palette_fence.py`, three layers, see §"Fences considered"). The **`OSError` symmetry** pin
  over `sorted(RENDERABLE_TEMPLATE_IDS)` (`tests/test_template_bodies.py:236-244`). The **adversarial DOM
  matrix** for the two new bodies (`b7fc3c9`), thirteen hostile content cases × two bodies plus clamped
  aspect and fit-floor cells, every case carrying a measured non-zero box floor. The **CSS-context fence**
  (`tests/test_css_injection.py`, `95cc997`), planting brace, semicolon, `url(`, `@import` and
  comment-opener payloads in every untrusted string. The **hostile-URL fence** (`5f829e8`), where an
  untitled `Source` falls back to `_host(url)` and a 203-character netloc widened the page from 1200 to
  1437px — the PNG comes out *wider* than asked for, which no overflow probe would have called a failure;
  fixed with `overflow-wrap: anywhere` on `.refs li` (`_chrome_apparatus.css:24`, `anywhere` and not
  `break-word`, because only `anywhere` shrinks the min-content contribution multicol sizes against). And
  the **invisible-character fence** (`dd70176`), where `layout._legible_url`/`_legible_text` (`:566-666`)
  **replace** `Cc`/`Cf` with U+FFFD rather than deleting them — a citation URL is a verification key, so a
  reader must see that something was removed — while deliberately keeping ZWNJ, ZWJ, LRM, RLM, ALM and the
  bidi isolates, which are legitimate markup for mixed-direction text.
- **Two fence-hygiene commits worth knowing about, because they are the failure mode of all of the
  above.** `570b4c6` found three fences reporting green while measuring nothing — a skip clause broad
  enough to swallow real Playwright errors, and an attribution check reading `document.body.innerText`,
  which silently degrades to `textContent` under `display: none` and therefore passed on a page showing
  nothing. `d7e4c67` pinned the collections three assertions walk, because a loop over an empty
  collection is a pass. `937358e` is the same lesson applied to the palette fence's own tables, and
  `ea608d8` is it applied to Protocol conformance: three port-conformance checks lived in
  test-function-local annotations, which Python discards at runtime, so `mypy` never saw them. **When you
  add a fence here, add the assertion that proves it is looking at something.**
- **914 tests pass and 1 skips** (the live-network one), and `mypy --strict src` is clean over 23 source
  files. Measured in a checkout carrying other sessions' uncommitted work in four files, so treat it as
  the order of magnitude rather than a golden; `fafec26`'s own message recorded 818 when it landed, which
  is how fast this number moves.

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
anywhere in `src/` or `tests/`.** A `logging.warning` in this repo reached `logging.lastResort` —
unformatted, on stderr, easy to miss — which is precisely how the `OSError` swallow in the old
`_readable_figures` managed to drop an unreadable hero, let `_hero_index` promote a `SUPPORTING` asset
into the empty slot, print a normal CLI summary and exit `0`. That swallow is gone as of `fafec26`,
and the module has no logger left at all — but the lesson is the point, not the code: adding an
invisible warning to fix a silent failure repeats a mistake this zone has already made once and paid
to undo.

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
`PageBody` is a `TypeAlias` (`layout.py:255`) discriminated by `assert_never` (`:481`, in `_figures_of`),
so adding an arm without handling it everywhere is a `mypy --strict` failure at the point of the omission — a
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

### The `OSError` asymmetry, resolved — and the direction that would still be a conversation

**This is answered in code now. `fafec26` made the two newer bodies raise, like `stat_grid`.** An
earlier revision of this section described that as specified and pending; it landed, and the section
below is the record plus the part that is still open.

What shipped: `_readable_figures` is gone. In its place `_all_figures` (`layout.py:822-838`) embeds every
asset it is handed, hero first, with no `try` and no filter, so an unreadable `Path` propagates
`OSError` out of `build_process_flow_page` (`:298`) and `build_ranked_list_page` (`:321`) exactly as it
always did out of `build_page` (`:272`). `_LOG` and `import logging` were deleted from `layout.py` as
its only users — grep returns nothing for either. `ports.py` was **not** edited. `build_page`'s
docstring (`:275-287`) now narrates the whole thing in past tense and drops the sentence that used to
ask for a `ports.py` conversation before anyone made the two consistent.

**The argument that settled it, because it is the reusable part.** The swallow looked like graceful
degradation and was actually a *silent success*. There is **no logging configuration anywhere** in
`src/` or `tests/`, so `_LOG.warning` reached `logging.lastResort` — unformatted, on stderr, naming the
mime type and role but **not the path**. Downstream of that, a page that lost its hero was
indistinguishable from a page that never had one, because `_hero_index` (`:841`, reading
`ImageRole.HERO` at `:850`) then promoted a `SUPPORTING` asset into the empty slot; the CLI printed a
normal summary and exited `0`. And the licensing consequence is the one that actually decides it: **a
dropped image is not credited either**, so the quiet path lost a licensed image with nothing in the
colophon to say so. In a project where attribution has to be visible in the output, "we silently didn't
show it, and silently didn't credit it" is not a degradation mode you can ship.

The fence is one parametrized test over `sorted(RENDERABLE_TEMPLATE_IDS)` —
`test_an_unreadable_asset_raises_oserror_from_every_renderable_body`
(`tests/test_template_bodies.py:236-244`) — so a fourth renderable body inherits the pin for free. Its
`match=r"absent\.png"` is load-bearing rather than decorative: `font_faces()` (`layout.py:497`) raises
its own `OSError` on a missing bundled woff2, so a bare `pytest.raises(OSError)` would pass against a
completely broken implementation. The composer-level mirror
(`test_missing_path_backed_asset_raises_oserror`, `tests/test_composition.py:1887-1891`) was left
untouched.

**What is still a conversation.** Reconciling in the *other* direction — making `build_page` swallow —
remains a `ports.py` change and therefore not a cleanup, and it is worth keeping written down because
the tidier-looking option is the one that costs a contract:

- **Making `build_page` swallow is a `ports.py` change, and therefore a conversation, not a cleanup.**
  `ports.py:100-101` is a documented promise and `test_missing_path_backed_asset_raises_oserror` pins
  it. Deleting a promise from a port that three zones code against is not something zone 3 does on the
  way past.
- **The port arguably permits both readings, which is exactly why it needs a human and not a judgement
  call.** `ports.py:88-89` says "`images` may be empty -- produce a text-only layout, never raise", and
  `:92-93` says "You may use a subset of `images`, but embed and credit exactly the ones you display".
  Read together, those are a licence to display fewer images than you were handed and never fail — which
  is precisely what the swallow did. The counter-reading, and the one `fafec26` took, is that subsetting
  is permission to choose a *layout*, not permission to lose a *file* silently. Both readings are
  available in the text as written, which is why the resolution went to the stricter promise at
  `:100-101` rather than to a judgement call: when a port supports two readings, prefer the one that
  cannot fail silently. The ambiguity itself is still a defect in the port and still worth an
  Architect's pass.
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

## Two more open items, recorded rather than resolved

### A page dimension was a CSS injection sink, and `core/` still enforces nothing

`_chrome.css:84` is `body { min-height: {{ page.chrome.min_height_px }}px; }`, guarded by an `{% if %}`
at `:83`, and it is fed from `RenderOptions.height_px`. That field is `int | None` on a frozen slotted
dataclass (`models.py:63`) with **no validation of any kind** — `RenderOptions(height_px='auto}
body{display:none} /*')` constructs happily and `height_px` is then a `str`. Autoescape does not help:
`_chrome.css:8-11` already warns that autoescape escapes for HTML and *not* for CSS. The payload
produced a full-page `display: none`, which also hides the **mandatory licence attribution** — so the
worst case here is not a broken layout, it is a render that silently stops meeting its licensing
obligations.

**A correction to how this was first described, because the difference matters.** Only `height_px`
reached CSS. `width_px` also lands in the sheet at `_chrome.css:24` (`--w: {{ page.chrome.width_px }}px`),
but a string never got that far: `layout._gutter` (`:534-535`) computes `round(width_px * _GUTTER_RATIO)`
and `_GUTTER_RATIO` (`:53`) is the float `0.06`, so `str * float` raises `TypeError` one frame earlier.
**That protection was entirely accidental.** `_gutter` is two lines with no docstring and no comment; it
names neither `RenderOptions` nor CSS nor validation, and it held only as long as that constant stayed a
float. One field was exploitable and the other was one edit away from being exploitable, which is the
same defect wearing two different amounts of luck.

Both are now coerced at the boundary. `1c44352` added `_css_px` (`layout.py:392-427`) and the
`None`-tolerant `_css_px_or_none` (`:430-432`), called from `build_chrome` at `:373` and `:388` — but it
did not close the sink; **`427fce2` did.** `_css_px` asked `isinstance(value, int)` and returned the
object unchanged, and an `int` subclass brings its own `__str__` that Jinja's `escape` calls, so `{`,
`}`, `;` and `/*` walked through the check built to stop them until `_css_px` started returning
`int(value)`. They **refuse** rather than coerce — **`ValueError`** naming the field and saying why —
and they refuse `bool` explicitly despite it being an `int` subclass. `ValueError` and not the
`TypeError` `1c44352` raised, because `ports.py:100-101` declares `Composer` raises `ValueError` and
`OSError` and nothing else: `TypeError` widened a shared contract without the conversation a `core/`
change requires, and bought nothing, since `ValueError` was already permitted. `core/` was not touched
by either commit. Note that this reorders the protection: `_css_px` now runs before `_gutter`, so the
accidental guard is no longer the operative one.
Not CLI-reachable either way: `cli.py:60`, `:67` and `:84` declare `--width`/`--height` as `type=int` and
`--scale` as `type=float`, so `argparse` refuses a non-numeric argument before `RenderOptions` exists.
The sink was library-API-reachable only. Pinned by `tests/test_composition.py:1341-1370` and `:1400-1424`
over a `GEOMETRY_SINKS` table; the refusal test matches the *message* text, deliberately, so it cannot
pass on `_gutter`'s accidental `TypeError`.

**The declinable ask, for Hiren.** Should `RenderOptions` gain a `__post_init__` that coerces or refuses
its geometry fields, or is boundary coercion in `layout.py` the permanent answer? I am not implementing
either, and there is a real argument each way: `models.py` says of itself, at `:5-6`, that "everything
here is pure data: frozen, slotted, fully typed, no behaviour and no validation", and a `__post_init__`
is behaviour, so the ask is partly whether that sentence is a principle or an accident.

**And the general shape, because it will outlive this instance.** `core/` declares types and enforces
nothing — there is no `__post_init__` and no `raise` anywhere in `models.py` — so every unvalidated
field is a bill paid by whichever zone happens to be downstream. `height_px` was the instance that bit.
`device_scale_factor` (`models.py:67`, a `float`) is the near neighbour: it never enters CSS, so it is
not this bug, but it does reach Playwright, and nothing between the two says a word about what values
are sane. Whoever answers the specific ask should answer the general one in the same breath.

### `agent_composer.py`'s refusal branch is dead code, precisely when it matters

Recorded only — `agent_composer.py` is read-only for this pass, and none of it changes what the registry
does.

`_ask` awaits `client.messages.parse` at `:451` and only then checks
`if response.stop_reason == "refusal"` at `:461-462`. That ordering does not do what its docstring says.
`messages.parse` validates **every text block** inside the await — the SDK's post-parser runs
`validate_json` per text block before the coroutine returns — so a narration or preamble block that is
not valid JSON raises `ValidationError` *inside* the await, before line 461 exists. `ValidationError` is
a `ValueError`, and both call wrappers catch bare `Exception` (`_select` at `:634`, `_map` at `:660`,
each annotated "every failure is a degradation"), so it degrades to the rule table. Nothing anywhere
re-routes a validation failure to the refusal branch — `ValidationError` does not appear in the module at
all.

**The reason this is worse than a cosmetic dead branch: a `"refusal"` stop reason means truncated text
that cannot validate.** So the branch is unreachable in exactly the situation it was written for, and
its only reachable case is a refusal whose text blocks happen to validate anyway — the case where
nothing was wrong. The docstring at `:443-444`, claiming `stop_reason` is checked *before*
`parsed_output` reads `content`, is true about line order and false about the code: the SDK already read
and validated `content` upstream.

Two things to keep straight. **Thinking blocks are safe** — the SDK's parser passes any non-`text` block
through untouched and never validates it, and `ParsedMessage.parsed_output` skips them, so thinking being
on by default costs nothing here. And the existing test does **not** catch this: the stub in
`tests/test_agent_composer.py:486` exposes `parsed_output` as a property that raises when `stop_reason`
is `"refusal"`, and its `parse` never validates anything, so it pins line order while modelling away the
exact SDK behaviour that makes line 461 unreachable. A green test there is not evidence.

The user-visible outcome is unchanged — both paths reach the same fallback — so this is a diagnostics
bug, not a correctness one. What is lost is the specific `ModelDeclinedError` message in favour of a
generic warning quoting a pydantic traceback.

## Fences considered, and the one that was rejected

Recorded so nobody re-derives either of these from first principles. Both were measured, not reasoned
about.

### A perceptual-hash visual-regression fence does not work here. Do not re-propose it.

The idea is obvious and wrong: render each template, hash the PNG, commit the hash, fail on a Hamming
distance. Measured under `imagery/prepare.py`'s existing convention — 64-bit average hash, greyscale,
LANCZOS to 8×8, threshold each pixel against the image's own mean:

- **Noise floor: 0.** Two identical-input renders, six template×theme cells, distance 0 in all six.
  Not "small" — zero.
- **Every realistic perturbation also measured 0.** An `--accent-paper` nudge ~8% lighter (**8,339
  pixels at peak channel delta 10**): **0**. A background hue swap touching **41%** of the page's
  pixels: **0**. A body-text colour swap: **0**. The credit line turned red: **0**. A body `font-size`
  change of 16.5 → 18px, reflowing the page: **0**. DARK repeats of the first three: 0 for all three.
  *(An earlier revision of this bullet gave the nudge as 9,619 pixels at peak 9, hung pixel counts and
  peak deltas on the colour swaps, gave the reflow as 2347 → 2357 pixels tall, and put the hue swap at
  41.35%. Every one of those figures except the 41% appears in no commit, no test and nowhere in `src/` —
  they were this document's own, the same defect class `75a721a` shipped to purge. The nudge is the
  8,339-at-peak-10 measurement `tests/test_palette_fence.py:63-64` and `2afd3f2` both record; the colour
  swaps and the reflow were only ever recorded qualitatively, so that is all they claim here now.)*
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

**What shipped instead — `tests/test_palette_fence.py`, in `2afd3f2`.** An earlier revision of this
section said the replacement was specified and not yet written. It is written. Its own module docstring
is now the best statement of this argument in the repository, and it is worth reading before touching
anything colour-related.

It pins the ten palette tokens `_chrome.css` declares, in both themes — **20 hex literals** (14 distinct
colours) transcribed at `test_palette_fence.py:159-172` — in **three layers**, and the docstring is
emphatic that they are not interchangeable. The transcription is deliberate: a fence that read these out
of the stylesheet it is fencing would pass any palette edit whatsoever.

1. **Pure Python, no browser.** `declared_tokens` parses the stylesheet's own declarations and
   `test_every_colour_the_chrome_declares_is_pinned_by_a_probe` (`:610`) compares them against the
   committed literals. The cheapest and strongest single assertion in the module.
2. **Computed style.** Catches a token that resolves wrongly on the page, and is also where the six
   tokens with no flat fill to sample get watched — among them the licence URI a reader has to retype
   out of the PNG.
3. **Sampled pixels**, for the one class the other two cannot see: **paint time** — three sample points ×
   three bodies × two themes, so **18 pixel assertions**, which is a different number from the 20 hex
   literals and worth not conflating. A colour can be declared correctly, resolve correctly, and still not
   reach the deliverable. Falsified against injected
   regressions: `.rule { opacity: 0.5 }` reads `#a4aa7a` where the token says `#5c6a12`;
   `body { filter: grayscale(1) }` greys all three samples; a masthead pulled up by `margin-top: -6px`
   covers the accent bar outright. **Every one of those leaves both other layers green.** Opacity,
   filters and occlusion are exactly what gets added while somebody is nudging a design, so do not delete
   the browser cells on the grounds that the CSS-parse layer already compares the colours — it does, and
   it cannot see any of this.

Three properties make the pixel layer trustworthy rather than lucky, and all three are worth copying into
the next fence: **coordinates are derived, never hardcoded** (every sample comes from the element's own
`getBoundingClientRect()` at run time, because rect `y` is fractional and differs per body, so a committed
offset would rot); **flatness is a checked precondition** (the 5×5 device-pixel neighbourhood is measured
to be a single colour *before* the colour is compared, so a gradient landing on a sampled region fails the
guard and names the selector rather than failing the comparison for an unreadable reason); and **exact
equality, no tolerance** — even ±1 per channel would swallow the `--accent-paper` nudge the module exists
for, which is a peak channel delta of **10** across **0.66%** of the page and an entirely realistic edit.

`937358e` then closed the last gap in it: the population check now guards `TOKENS` itself, because an
emptied table made every loop over it vacuously pass. A fence whose table can go empty is a fence that
can go quiet, and that shape is worth looking for deliberately — it was found by a validation pass doing
exactly that.

One live `TODO:` in the module, worth carrying here because it is a licensing exposure and not a
housekeeping note: `--dim-patch` and `--accent-patch` are pinned by **computed style only**, so a scrim
or an `opacity` over the colophon panel could leave legally load-bearing attribution unreadable with this
module fully green. The right instrument is a computed-contrast fence that composites the colour stack and
asserts a WCAG ratio — a sibling module, not another sampled pixel.

### Byte-exact goldens: determinism was never the obstacle. The platform moving more than the defect is.

**This is the correction that matters, because the intuitive objection to goldens is the wrong one.** I
originally framed this as "renders are reproducible here but might not be elsewhere". Not so — the
measured refutation is sharper and does not depend on any claim about other machines.

- **Determinism is not the problem.** Three renders of one composition are byte-identical within a
  process and across processes, and byte-identical across **chromium 148, 149 and 151**, with all four
  faces embedded as data URIs. *(An earlier revision of this bullet cited `chrome-headless-shell` cache
  revisions 1223, 1228 and 1234 instead. Those came from an exploratory run and appear nowhere in
  committed code; `148/149/151` is the number `2afd3f2` and `test_palette_fence.py:58-61` actually
  record, so cite that one. Nothing committed names the mechanism by which the three were swapped, so
  this document should not invent one either.)*
- **The fonts are not the problem.** `layout.font_faces()` (`layout.py:497`) inlines four bundled woff2
  faces as data URIs and `css/_chrome.css` names only "Ledger Slab", "Ledger Text" and "Ledger Mono" plus
  CSS generics. Zero system-font dependence, enforced by test.
- **The problem is that a sha256 is one bit, and the platform moves more of the page than the defect
  does.** Dropping the CoreText-only `-webkit-font-smoothing: antialiased` (`css/_chrome.css:80`) moves
  **26,921 pixels at peak channel delta 195**. The accent-colour regression the fence exists to catch
  moves **8,339 pixels at peak delta 10** — **3.2× less area and 19.5× less amplitude**. **The false
  positive strictly contains the signal.** No exact hash separates them, and neither does any per-pixel
  budget: any threshold loose enough to tolerate the platform is loose enough to sleep through the
  regression. That is the same shape of result that killed the perceptual hash, arrived at on a completely
  different axis — which is the strongest reason to believe it.

**An opt-in golden baselined into a temp directory was considered and dropped too.** It looks like it
sidesteps the portability problem, and it does — by never being able to fail on a fresh machine. Worse,
the first run after any change silently re-baselines the very regression it was bought to catch. A fence
that cannot fail on a clean checkout is not a weaker fence; it is a passing test that means nothing.

**Consequence for CI: do not commit goldens.** For the palette-regression class they were meant to catch,
a laptop sha is 0% true positives — that class is invisible to a hash — while firing on essentially every
other machine. Storage is the smaller objection and still one: six cells at the `RenderOptions` default is
5,611,870 bytes each, about **33.6 MB** of binary in a repository with zero committed `.png` files.

**What the palette fence does instead is keep the golden's sensitivity and drop its fragility.** Seen from
one side, the sampled pixels *are* a golden — but taken over a mask deliberately chosen to contain no
glyphs, with expected values transcribed as hex rather than committed as bytes. Text is the entire reason a
whole-frame golden fails here, so a fence that never samples text is sensitive to a peak-10 nudge and
indifferent to which rasteriser drew the page. That is the trick worth remembering.

**And a correction, because the obvious reason to keep that declaration is not the real one.**
`-webkit-font-smoothing: antialiased` does **not** suppress subpixel colour fringing here — now confirmed
by a third independent measurement and recorded in committed history. Measured `#000` on `#fff` at 16.5px
serif in headless chromium across all four smoothing modes — absent, `antialiased`,
`subpixel-antialiased`, `none` — at both `dsf=1.0` and `dsf=2.0`, counting pixels whose R/G/B are not all
equal: **zero chromatic pixels in every mode at both scale factors.** macOS headless chromium rasterises
to a non-LCD surface and does no subpixel antialiasing at all, independently corroborated by
`subpixel-antialiased` being pixel-count-identical to no declaration — impossible if there were an LCD
path to opt into. There are no coloured fringes to suppress. What the declaration actually does is **stem
darkening**: `antialiased` renders fewer non-white pixels, so glyphs come out lighter and thinner. That is
the whole of the 26,921-pixel delta, and it is pure luminance. **Keep it — but keep it as a typographic
choice, not a portability one.** The reasoning matters because the plausible version of it is false, and a
comment repeating it would have written a falsehood into `_chrome.css`.

## Findings worth keeping

Four things that cost real time to discover and will cost it again.

- **Attribution is rendered in four distinct places, and the fourth was on nobody's list.** This matters
  because attribution here is legally load-bearing, so a fence watching the colophon is watching one site
  of four. The authoritative enumeration now lives in code, as selectors rather than as a total —
  `REPLACEMENT_SITES` at `tests/test_composition.py:2151-2158`: the **bibliography** (`.refs`,
  `_base.html.j2:41-56`), the **colophon** (`.credits`, `_base.html.j2:58-78`), the **fact and section
  attributions** (`.row__src` in `stat_grid`, `.chip__src` in `process_flow`, `.rank__src` in
  `ranked_list`, plus `.section__src` in the two that render sourced sections), and the **hero credit**
  (`.hero__credit`). The hero credit is the one that was missing: `layout._caption` (`:898-902`) builds it
  from `Credit.work`, `Credit.author` and `Credit.license` — the same three fields the colophon prints —
  but each body emits it inside `{% block masthead_aside %}` (`stat_grid.html.j2:30`,
  `process_flow.html.j2:20`, `ranked_list.html.j2:20`), so it sits in `<header>`, **outside both `.refs`
  and `.credits`**, and four sanitised characters per body were rendering where no floor was watching.
  *(`_caption` builds no markup itself — it returns a bare `str` the templates place. Easy to assume
  otherwise.)*
- **A single whole-document floor cannot find a missing rendering site; a per-site table can.** That is
  how the hero credit surfaced, and it generalises. Measured: with the body block excised from the
  templates the page still rendered its bibliography and colophon and reported **9** replacement
  characters, so a single floor of **8** passed a document with **no body at all** — and losing the
  colophon, or losing one fact attribution, cleared it too. Per-site counts reject all three, and the
  failure names the site. When you are fencing something that must appear in several places, count the
  places, not the occurrences.

- **`TemplateSpec.image_roles` enforces nothing.** Covered under §"Shape 1" with the measurements. The
  short version for anyone skimming: it is read once, as prose, in a model prompt, and every asset is
  placed whatever role it carries. Do not build a feature on the assumption that it filters.
- **`cqw` resolves against the query container's *content* box, not its track.** This invalidated several
  pixel predictions during the fence work. A predicted 12.2px minimum title size measured 6.55px,
  because 6.55px is `3.00cqw` of `.masthead__text`'s **218.39px content box** and not of the 294.39px
  grid track it sits in — the gutter padding takes 294 down to 218. The predictions that were *counts*
  reproduced exactly; every prediction that was a *pixel value* was wrong by the padding. If you are
  reasoning about container query units on paper, subtract the padding first, and expect to be wrong
  until you have measured it in the browser.

## The one question that was Hiren's, and is now closed

**Whether a PNG composed from the two `CC-BY-SA-4.0` panda images inherits ShareAlike is closed, and it
closed the only way it could — by removing the exposure rather than by settling licence law.** Hiren
decided it: the Wikimedia images are test fixtures only, and the image sources are being replaced
wholesale, so no `CC-BY-SA-4.0` image is intended for distribution and there is no distributed composite
for ShareAlike to reach. Nothing in this document depended on the answer, and nothing depends on it now.

`docs/plan.md` records the decision, in place of the "decide it before shipping anything publicly" it
used to park it under.

**The attribution machinery stays, all of it.** ShareAlike was never what it was for: `ImageCredit`'s
mandatory `license`, attribution rendered visibly in the PNG rather than only stored in JSON, and the
four rendering sites the per-site floor counts are what *any* licensed source needs, and the replacement
sources will need them on day one. Do not thin any of it out on the grounds that the licence question
went away.

One thing that looked downstream of ShareAlike survives it, because it was never really about
ShareAlike: `quote_spotlight` puts a `BACKGROUND` image behind text, and that is where "attribution
visible in the rendered output" quietly stops being true — for a CC0 source as much as a CC-BY-SA one.
That is still a reason to answer the `Quote` field ask and the legibility question together.

**The other one is fixed, and this paragraph used to say otherwise.** An earlier revision recorded
`.hero__credit` — a *legal* attribution line — as failing WCAG AA on every hero in the pool, quoting
3.09:1, 2.30:1 and 2.63:1. Do not carry those numbers forward: none of them reproduce, and `9e2613f`
resolved the underlying bug. The scrim's gradient was running `to top`, so its 0.78 stop was spent on the
padding *below* the text and the ramp was still climbing where the glyphs actually sit. Reversing it to
`to bottom` with the peak at 24px — exactly the element's own `padding-top` — puts full opacity above the
first glyph. The rule now lives once in `css/_chrome.css:207-218` rather than three times in the body
sheets, and measures **9.04, 9.40, 9.80, 10.47 and 13.61 to one** across the five shipped heroes. There is
an analytic floor behind the measurement, which is the part worth keeping: 0.61 alpha is break-even for AA
against *any* possible hero, so 0.78 clears AA unconditionally and clears **AAA at 8.95:1**. It is no
longer a measurement that could go stale when imagery picks a different photograph.

`ffb6815` then pinned the thing that makes that durable. The hero readback asserts three independently
necessary properties — the contrast ratio at the **worst** band pixel rather than the mean, that the alpha
is *flat* across every band pixel, and that it equals `SCRIM_PEAK_ALPHA = 0.78` — plus
`borderTopWidth == 0`, because the gradient paints the padding box and that coincides with the border box
only while no border is declared. `css/_chrome.css:194-197` now names all three silent couplings in a
comment. The same commit corrected four figures in `9e2613f`'s own prose that did not reproduce, which is
worth noting for its own sake: a commit that fixes a real bug can still ship wrong numbers about it, and
someone re-measuring is how that gets caught.

No recommendation in this document ever turned on the ShareAlike answer, which is why the decision
changes nothing here beyond this section.
