from pathlib import Path

from infographic_agent.config import settings
from infographic_agent.contracts.content_mapping import ContentMappingResult
from infographic_agent.contracts.image_manifest import ImageCandidate, ImageManifest
from infographic_agent.steps.render import render
from infographic_agent.templates.base_slots import ImageSlot, StatSlot, TimelineEventSlot
from infographic_agent.templates.comparison.schema import ComparisonRow, ComparisonSlots
from infographic_agent.templates.process_flow.schema import ProcessFlowSlots
from infographic_agent.templates.quote_spotlight.schema import QuoteSpotlightSlots
from infographic_agent.templates.ranked_list.schema import RankedListSlots
from infographic_agent.templates.stat_grid.schema import StatGridSlots
from infographic_agent.templates.timeline.schema import TimelineSlots

_ASSETS = Path(__file__).parent / "fixtures" / "images" / "assets"
_OUT = Path(__file__).parent / "fixtures" / "renders"


def _manifest_with(*paths: Path) -> ImageManifest:
    return ImageManifest(
        topic="test",
        candidates=[
            ImageCandidate(id=f"img-{i+1}", path=str(p), alt_text="placeholder")
            for i, p in enumerate(paths)
        ],
    )


def _write_png(name: str, mapping_result: ContentMappingResult, manifest: ImageManifest) -> Path:
    _OUT.mkdir(parents=True, exist_ok=True)
    png_bytes = render(mapping_result, manifest)
    out_path = _OUT / f"{name}.png"
    out_path.write_bytes(png_bytes)
    return out_path


def _assert_png(path: Path) -> None:
    assert path.exists()
    assert path.stat().st_size > 1000
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_stat_grid_renders():
    manifest = _manifest_with()
    slots = StatGridSlots(
        title="Renewables Are Outpacing Every Forecast",
        subtitle="2025 was a record year for clean energy capacity",
        stats=[
            StatSlot(label="Global solar capacity added", value="480 GW", detail="up 32% YoY"),
            StatSlot(label="Share of new capacity from renewables", value="92%"),
            StatSlot(label="Avg. cost of utility-scale solar", value="$0.031/kWh"),
        ],
    )
    out = _write_png("stat_grid", ContentMappingResult(template_id="stat_grid", slots=slots), manifest)
    _assert_png(out)


def test_timeline_renders():
    manifest = _manifest_with()
    slots = TimelineSlots(
        title="Six Years of Acme Cloud",
        events=[
            TimelineEventSlot(date="2019", label="Launches as a simple object storage service"),
            TimelineEventSlot(date="2021", label="Adds managed compute and enterprise SLA tier"),
            TimelineEventSlot(date="2023", label="Launches multi-region replication"),
        ],
    )
    out = _write_png("timeline", ContentMappingResult(template_id="timeline", slots=slots), manifest)
    _assert_png(out)


def test_comparison_renders():
    manifest = _manifest_with()
    slots = ComparisonSlots(
        title="Cloud vs. On-Prem: What Actually Changes",
        left_label="Cloud",
        right_label="On-Prem",
        rows=[
            ComparisonRow(label="Upfront cost", left="Low", right="High"),
            ComparisonRow(label="Scaling time", left="Minutes", right="Weeks"),
        ],
    )
    out = _write_png("comparison", ContentMappingResult(template_id="comparison", slots=slots), manifest)
    _assert_png(out)


def test_process_flow_renders():
    manifest = _manifest_with()
    slots = ProcessFlowSlots(
        title="Your First Perfect Shot",
        steps=[
            "Grind 18g of beans to a fine, even consistency",
            "Distribute grounds evenly in the portafilter",
            "Tamp firmly with about 30 lbs of pressure",
        ],
    )
    out = _write_png("process_flow", ContentMappingResult(template_id="process_flow", slots=slots), manifest)
    _assert_png(out)


def test_quote_spotlight_renders():
    manifest = _manifest_with()
    slots = QuoteSpotlightSlots(
        quote_text="If you want to have good ideas you must have many ideas.",
        attribution="Linus Pauling",
        supporting_stat=StatSlot(label="Patents filed", value="25,000+"),
    )
    out = _write_png("quote_spotlight", ContentMappingResult(template_id="quote_spotlight", slots=slots), manifest)
    _assert_png(out)


def test_ranked_list_renders():
    manifest = _manifest_with()
    slots = RankedListSlots(
        title="Top Risks Before We Cut Over",
        items=[
            StatSlot(label="No rollback plan tested beyond staging", value=""),
            StatSlot(label="DB migration script unverified at scale", value=""),
            StatSlot(label="On-call engineers unfamiliar with new infra", value=""),
        ],
    )
    out = _write_png("ranked_list", ContentMappingResult(template_id="ranked_list", slots=slots), manifest)
    _assert_png(out)


def test_output_canvas_size():
    slots = ProcessFlowSlots(title="Size check", steps=["a", "b", "c"])
    manifest = _manifest_with()
    png_bytes = render(ContentMappingResult(template_id="process_flow", slots=slots), manifest)

    # PNG IHDR chunk stores width/height as big-endian uint32 at fixed offsets
    width = int.from_bytes(png_bytes[16:20], "big")
    height = int.from_bytes(png_bytes[20:24], "big")
    assert width == settings.canvas_width * settings.device_scale_factor
    assert height == settings.canvas_height * settings.device_scale_factor
