"""Build the versioned fictional PDF; no provider calls or user documents."""
import json
from pathlib import Path

import pymupdf


def build(source: Path, destination: Path):
    fixture = json.loads(source.read_text(encoding="utf-8"))
    with pymupdf.open() as pdf:
        for number, section in enumerate(fixture["pages"], 1):
            page = pdf.new_page()
            page.insert_text((50, 55), fixture["title"], fontsize=14)
            page.insert_text((50, 90), section["title"], fontsize=18)
            remaining = page.insert_textbox(
                pymupdf.Rect(50, 120, 545, 740), "\n\n".join(section["paragraphs"]),
                fontsize=12, lineheight=1.5,
            )
            if remaining < 0:
                raise ValueError(f"Page {number} overflowed; do not publish a truncated fixture")
            page.insert_text((50, 790), f"{fixture['version']} | Page {number} | Fictional demo data", fontsize=9)
        pdf.set_metadata({"title": fixture["title"], "author": "Inside the Agent evaluation"})
        destination.write_bytes(pdf.tobytes(garbage=4, deflate=True, no_new_id=True))


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    build(root / "handbook-v1.json", root / "harbor-handbook-v1.pdf")
