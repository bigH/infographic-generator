import argparse
from pathlib import Path

from infographic_agent.contracts.content import ContentPayload
from infographic_agent.contracts.image_manifest import ImageManifest
from infographic_agent.pipeline import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an infographic PNG from content + image JSON.")
    parser.add_argument("--content", required=True, type=Path, help="Path to a ContentPayload JSON file")
    parser.add_argument("--images", required=True, type=Path, help="Path to an ImageManifest JSON file")
    parser.add_argument("--out", required=True, type=Path, help="Output PNG path")
    args = parser.parse_args()

    content = ContentPayload.model_validate_json(args.content.read_text())
    manifest = ImageManifest.model_validate_json(args.images.read_text())

    png_bytes = run(content, manifest)
    args.out.write_bytes(png_bytes)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
