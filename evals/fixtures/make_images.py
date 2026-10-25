"""Draws the synthetic photos used by the vision eval cases (no real photos, nobody's personal data).

Regenerate with:  python -m evals.fixtures.make_images   (needs Pillow, from requirements-dev.txt)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent


def poster(path: Path, lines: list[tuple[str, int]], background: str, ink: str) -> None:
    """A phone-photo-sized image with centered lines of text: (text, font size)."""
    image = Image.new("RGB", (900, 1200), background)
    draw = ImageDraw.Draw(image)
    y = 150
    for text, size in lines:
        font = ImageFont.load_default(size=size)
        width = draw.textlength(text, font=font)
        draw.text(((900 - width) / 2, y), text, font=font, fill=ink)
        y += int(size * 1.6)
    image.save(path, optimize=True)


if __name__ == "__main__":
    poster(HERE / "yoga_flyer.png", [
        ("Community Yoga", 84), ("in the Park", 84), ("", 30),
        ("Saturday, November 7", 56), ("10:00 AM - 11:30 AM", 56), ("", 30),
        ("Balboa Park, San Diego", 44), ("Free - bring a mat", 44),
    ], background="#fdf6e3", ink="#073642")
    poster(HERE / "pharmacy_hours.png", [
        ("MAIN STREET PHARMACY", 60), ("", 40), ("Store hours", 50), ("", 20),
        ("Monday - Friday   9 AM - 7 PM", 44), ("Saturday   10 AM - 4 PM", 44), ("Sunday   Closed", 44),
    ], background="#ffffff", ink="#222222")
    print("Wrote", ", ".join(p.name for p in HERE.glob("*.png")))
