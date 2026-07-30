# infographic-generator

Prompt in, infographic PNG out. Research the web for facts, find images, compose an HTML page, screenshot it with Playwright.

**This is a fun hack project.** Four people building in parallel for the joy of it. Bias toward shipping something that works and looks good over architectural purity. Play with it. If a stage produces something ugly, that's a bug worth fixing — this is a *visual* project and the output is the point.

That said: the seams between the four zones are load-bearing, because four people are building behind them at once. Change a contract in `core/` by asking, not by editing.

## Quickstart

```bash
uv sync
uv run playwright install chromium   # first time only
uv run pytest                        # must stay green
uv run infographic "a panda" -o out.png
```

## Working together — we all push to main

No branches, no PRs. Everyone commits and pushes straight to `main`. That works fine here because the ownership zones below mean we're almost never editing the same file — but it puts the burden on you not to clobber anyone.

**Before every push:**

```bash
git pull --rebase origin main   # replay your work on top of theirs
uv run pytest                   # rebasing can break things that merged cleanly
git push
```

**Never:**

- `git push --force` or `--force-with-lease` to `main`. If your push is rejected, someone pushed first — rebase and try again. Forcing deletes their commit.
- `git reset --hard` / `git rebase` on commits already pushed. Rewriting shared history breaks everyone else's clone.
- `git checkout .` or `git restore` across the whole tree to "clean up" — you'll take out someone else's uncommitted work if you're sharing a machine.
- Resolve a conflict in `core/` or `pyproject.toml` by picking your side. Those are shared contracts — ask first.

**Do:** commit small and push often. A ten-minute-old commit rebases cleanly; a three-day branch does not. Pull before you start working, not just before you push.

**Every pull that brings in new commits gets a review before you continue.** Each of us is driving an agent, so the remote moves constantly — and a clean rebase is not the same thing as a correct integration. Dispatch a sub-agent (`[Critic]` or `[Scout]`) to read the incoming commits and answer:

- What changed, and does any of it touch `core/` or a port that in-flight work is being built against?
- Does anything that just landed duplicate, contradict, or invalidate what's currently being built?
- Do `uv run pytest` and `uv run --with mypy mypy --strict src` pass with their work *combined* with yours — not just individually?
- Did someone widen a zone boundary, edit `pyproject.toml`, or change a shared file the rules above say to escalate on?

Green tests after a rebase only prove the merge was textually clean. They don't prove two agents didn't just build the same thing two different ways, or that the contract you're coding against is still the contract. If in-flight work is building against something that just changed, stop it, re-brief it with the new reality, and restart — don't let it finish against a stale contract. This review is cheap; finding out three commits later is not.

## Pipeline

```
Brief ──▶ Researcher ──▶ ResearchContent ──▶ ImageSourcer ──▶ [ImageAsset] ──▶ Composer ──▶ Composition ──▶ Renderer ──▶ PNG
                                                                                            (HTML)          (Playwright)
```

Everything is `async`, and **strictly sequential** — image sourcing reads the researched content, and the composer takes brief, content *and* images, so no stage overlaps another. Data flows through frozen dataclasses in `core/models.py`; each stage is a `Protocol` in `core/ports.py`.

The AI-backed stages are written — `LlmResearcher` (`research/agent.py`), `WikimediaImageSourcer` (`imagery/wikimedia.py`), `AgentComposer` (`composition/agent_composer.py`) — but `build_pipeline` in `cli.py` still wires the panda stubs and no CLI flag switches them. Construct the agent yourself to run one.

## The three key areas

Each owner works in one directory and does not touch the others.

### 1. Text — research & fact collection → `src/infographic_generator/research/`
`async def research(self, brief: Brief) -> ResearchContent`

Turns a prompt into a title, subtitle, facts, and narrative sections. Every attributable fact carries a `Source` — **never invent a URL**. Honour `brief.max_facts`.
Stub data: `assets/panda/facts.json`. Tests: `tests/test_research.py`, `test_research_agent.py`.

### 2. Images — search & selection → `src/infographic_generator/imagery/`
`async def source_images(self, brief: Brief, content: ResearchContent) -> Sequence[ImageAsset]`

Finds images that match the facts. Returns 0–6 display-ready assets, significance-first, resized to ≤2000px **and roughly ≤1 MB encoded** — both bounds are in the `ImageSourcer` docstring in `core/ports.py`, and the second is the one people forget. This stage owns resizing: the composer inlines whatever it gets, so oversized images blow up render time. `ImageCredit.license` is mandatory.
Only `WikimediaImageSourcer` actually enforces the byte bound (via `imagery/prepare.py`); the `PandaImageSourcer` stub *drops* an over-2000px asset with a warning rather than resampling it.
Stub data: `assets/panda/*.jpg` + `credits.json`. Tests: `tests/test_imagery.py`, `test_imagery_wikimedia.py`.

### 3. Composition & rendering → `src/infographic_generator/composition/` and `render/`
`async def compose(self, brief, content, images) -> Composition`
`async def render(self, composition: Composition, output_path: Path) -> RenderResult`

Composer builds **self-contained HTML** — inline `<style>`, images as data URIs via `core.encoding.to_data_uri`, zero external requests (the renderer aborts them). Renderer screenshots it with Playwright/chromium.
Tests: `tests/test_composition.py`, `test_render.py` — plus nine more this zone owns, of which two are the non-obvious ones: `test_css_injection.py` (autoescape does **not** protect a `<style>` element — read it before interpolating anything into CSS) and `test_palette_fence.py` (proves the design tokens survive into the PNG — read it before editing a colour token). Also `test_selection.py`, `test_template_bodies.py`, `test_chrome_split.py`, `test_display_geometry.py`, `test_visual_order.py`, `test_source_host.py`, `test_agent_composer.py`.

`render/`, `pipeline.py`, and `cli.py` are the shared zone — no single owner. Coordinate before changing them. Their tests are `tests/test_contracts.py` and `test_pipeline_cli.py`.

The per-zone lists above are a map, not an index: whatever you touch, run the whole suite (`uv run pytest`) before you push.

## Libraries

- **`uv` for everything.** `uv add`, `uv run`, `uv sync`. Never `pip`.
- **`jinja2`** for templating. **Always `Environment(autoescape=True)`** — it is *not* the default, and research text and image credits are untrusted scraped web content flowing straight into markup.
- **`httpx`** for HTTP, not `requests`. Use the async client.
- **`playwright`** (chromium) — async API, matching the async ports.
- **`pillow`** for decoding, downscaling and re-encoding image bytes — the imagery stage's resize path lives in `imagery/prepare.py`.
- **`pytest` + `pytest-asyncio`** with `asyncio_mode = "auto"`, so `async def test_...` just works. Prefer property-based tests and real side effects over mocks.
- **frozen dataclasses**, not pydantic, for the core domain — the spine is `@dataclass(frozen=True, slots=True)` and stays that way.

For the AI-backed stages (already written — see the Pipeline section for where):

- **`anthropic`** SDK. Default to `claude-opus-5`. Thinking is on by default on Opus 5; tune depth with `output_config={"effort": "high"}` rather than a token budget. `temperature`/`top_p`/`top_k` are rejected — steer with prompting.
- **Structured output** is how research and image-selection results should come back: `client.messages.parse(..., output_format=SomeModel)` → `response.parsed_output`. This is the one place pydantic earns its place; it is already a dep.
  - **But `.parse` is a trap on a call you cannot afford to lose.** `parse_text` in `anthropic/lib/_parse/_response.py` runs `TypeAdapter(...).validate_json` over *every* text block with no `try/except`, and `resources/messages/messages.py` registers that as the request post-parser — so it runs inside the `await`. A narration preamble, a prose refusal or a `max_tokens` truncation raises `ValidationError` **before the response object is bound**, which makes the `stop_reason` check below unreachable exactly when you need it, and throws away the `usage` of a call that may have burned six figures of input tokens. Prefer `messages.create` with a hand-built `output_config` and parse the text yourself, or wrap `.parse` so a non-JSON block is recoverable. `research/agent.py` does the former and explains why in `_output_config` — copy that. `composition/agent_composer.py` does not, and pays for it by degrading to its rule table whenever the model narrates.
- **Server-side web search and fetch** (`web_search_20260209`, `web_fetch_20260209`) do the actual web work — no scraping stack needed. Don't also declare `code_execution`; these versions run it internally. **Necessary but not sufficient: also pass `allowed_callers=["direct"]`.** With the field omitted it defaults server-side to `["code_execution_20260120"]` and the response comes back with `code_execution` blocks and **not ZDR-eligible** — measured live; see the `_DIRECT_CALLER` note in `research/agent.py`.
- **Vision** (image content blocks) is the natural way to have a model *look* at candidate images and pick the ones that fit the facts.
- Handle `stop_reason == "refusal"` before reading `response.content`.

## Conventions

- Types everywhere, no `Any`. Short functions. Readability over cleverness.
- Models are frozen — build new ones, never mutate.
- `uv run pytest` is green. Keep it that way.
- Need a new dep or a new field on a core model? Ask — don't edit `pyproject.toml` or `core/` unilaterally.

## Gotchas

- **Image licences are real.** Four of the five panda images carry attribution obligations — two `CC-BY-SA-4.0`, two `CC-BY-2.0`, plus one `CC0-1.0` that does not. Attribution must be *rendered visibly in the output*, not just stored in JSON. `ImageCredit.modified` exists because CC BY-SA requires stating adaptation, and it is `true` on all five: the originals are 2048–6000 px and the fixtures are 1600 px. Trust `credits.json` over this paragraph — the data is authoritative, prose drifts.
- **Settled:** whether a PNG composed from the two `CC-BY-SA-4.0` images inherits ShareAlike. It no longer matters — Wikimedia is test-fixture-only and the image sources are being replaced wholesale, so no CC-BY-SA image is headed for distribution. The attribution machinery stays regardless; don't reopen this.
- **A poisoned image is waiting on Wikimedia Commons.** `File:Panda velká.jpg` is tagged CC BY-SA 4.0 "own work" but its EXIF credits `naturepl.com / LYNN M. STONE / WWF`. It's the best-looking forest shot in the pool and will tempt anyone who goes looking. Don't use it. Verify licences on the file description page, never from the filename.
- **Daily bamboo intake is genuinely contested** across sources (WWF 12–38 kg, Smithsonian 70–100 lb, IUCN up to 12.5 kg — they measure different plant parts). `facts.json` uses WWF's range and says so in `detail`. A future "correction" here is probably not a correction.

## More

`docs/plan.md` — architecture, ownership zones, Mermaid diagram, and how to swap a stub for a real agent.
