"""Screenshot the built page: one full-height PNG at 2x plus one PNG per section.

    python3 demo/shoot.py [--light]     # -> demo/shots/

Section boundaries are not hardcoded — they are found by looking for full-width runs of
page background, which is exactly where one section ends and the next begins. Edit the
page, re-run, and the cuts follow; a hardcoded offset table would quietly start slicing
through the middle of a card.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
SCALE = 2
MIN_GAP = 55 * SCALE            # a section break is ~56px of padding; a card gap is less


def capture(page: Path, out: Path) -> Image.Image:
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                    f"--force-device-scale-factor={SCALE}", "--window-size=1440,14000",
                    f"--screenshot={out}", "--virtual-time-budget=5000",
                    page.as_uri()], check=True, capture_output=True)
    return Image.open(out).convert("RGB")


def main() -> None:
    light = "--light" in sys.argv
    SHOTS.mkdir(exist_ok=True)
    page = HERE / "index.html"
    if light:
        page = SHOTS / "_light.html"
        page.write_text((HERE / "index.html").read_text(encoding="utf-8")
                        .replace('data-theme="dark"', 'data-theme="light"'), encoding="utf-8")
    suffix = "-light" if light else ""
    raw = SHOTS / f"page{suffix}-2x.png"
    im = capture(page, raw)

    a = np.asarray(im)
    bg = a[-1, 5].astype(int)                        # the page plane, read off the render
    ink = np.abs(a.astype(int) - bg).sum(2) > 12
    rows = np.where(ink.any(1))[0]
    height = int(rows.max()) + 34 * SCALE
    im = im.crop((0, 0, im.width, height))
    im.save(raw)

    band = ink[:height, 40 * SCALE:im.width - 40 * SCALE].any(1)
    cuts, start = [0], None
    for y, filled in enumerate(band):
        if not filled and start is None:
            start = y
        elif filled and start is not None:
            if y - start >= MIN_GAP and start > 0:
                cuts.append(y - MIN_GAP // 2)
            start = None
    cuts.append(height)

    for i, (top, bottom) in enumerate(zip(cuts, cuts[1:]), start=1):
        if bottom - top < 120 * SCALE:               # a stray gap, not a section
            continue
        path = SHOTS / f"{i:02d}{suffix}.png"
        im.crop((0, top, im.width, bottom)).save(path)
        print(f"{path.name}  {im.width}x{bottom - top}")
    print(f"{raw.name}  {im.width}x{height}  (весь лист)")
    if light:
        page.unlink()


if __name__ == "__main__":
    main()
