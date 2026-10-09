#!/usr/bin/env python3
"""Create a reproducible, image-only scan surrogate from a local PDF page.

This is a parser/OCR stress fixture, not a genuine scanner capture. Requires
Poppler's pdftoppm and Pillow; it makes no network requests.
"""
from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps


def make_synthetic_scan(source: Path, output: Path, page: int, dpi: int) -> None:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise SystemExit("pdftoppm is required (install Poppler locally)")
    if page < 1 or dpi < 72:
        raise SystemExit("page must be >= 1 and dpi must be >= 72")

    with tempfile.TemporaryDirectory(prefix="hybond-synthetic-scan-") as temp:
        prefix = Path(temp) / "page"
        subprocess.run(
            [pdftoppm, "-f", str(page), "-l", str(page), "-r", str(dpi),
             "-gray", "-png", "-singlefile", str(source), str(prefix)],
            check=True,
            capture_output=True,
            text=True,
        )
        image_path = prefix.with_suffix(".png")
        if not image_path.exists():
            raise SystemExit(f"pdftoppm produced no page image for page {page}")
        image = Image.open(image_path).convert("L")

    # Mild skew, softened print, uneven contrast, and deterministic dust mimic
    # common office-scanner defects without changing the document's content.
    image = image.rotate(0.35, resample=Image.Resampling.BICUBIC,
                         expand=True, fillcolor=255)
    image = ImageEnhance.Contrast(image).enhance(0.88)
    image = ImageEnhance.Brightness(image).enhance(0.98)
    image = image.filter(ImageFilter.GaussianBlur(radius=0.35))
    image = ImageOps.expand(image, border=24, fill=255)

    rng = random.Random(20261009)
    draw = ImageDraw.Draw(image)
    speckles = max(40, image.width * image.height // 40_000)
    for _ in range(speckles):
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        shade = rng.choice((90, 120, 150, 205))
        draw.point((x, y), fill=shade)

    # A JPEG pass adds realistic, deterministic compression artifacts.
    import io

    compressed = io.BytesIO()
    image.save(compressed, format="JPEG", quality=78, optimize=True)
    compressed.seek(0)
    image = Image.open(compressed).convert("L")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        output,
        format="PDF",
        resolution=dpi,
        title="Synthetic scanned-page OCR fixture (not a genuine scan)",
        subject=f"Derived from {source.name}, page {page}; skew/blur/JPEG/dust simulation",
        creator="scripts/make_rag_synthetic_scan.py",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Local source PDF")
    parser.add_argument("output", type=Path, help="Output image-only PDF")
    parser.add_argument("--page", type=int, default=1, help="One-based source page")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()
    make_synthetic_scan(args.source.resolve(), args.output.resolve(), args.page, args.dpi)


if __name__ == "__main__":
    main()
