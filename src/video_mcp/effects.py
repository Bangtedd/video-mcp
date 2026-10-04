"""Looks (colour grades) and readability gradients, built from FFmpeg 5.1 filters."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import VideoMCPError

# Each look is a list of standard filters applied once, after concat and before text.
LOOK_FILTERS: dict[str, list[str]] = {
    "none": [],
    # Warm / cool tints use per-channel curves rather than colorbalance: FFmpeg's
    # colorbalance weights its ranges by pixel lightness, so on many mid-tone
    # colours its midtone shift does nothing. Curves act the same on every colour.
    "warm": [
        "curves=r=0/0.03 0.5/0.56 1/1:g=0/0.01 0.5/0.51 1/0.99:b=0/0 0.5/0.44 1/0.92",
        "eq=saturation=1.1",
    ],
    "cool": [
        "curves=r=0/0 0.5/0.45 1/0.94:g=0/0 0.5/0.5 1/0.99:b=0/0.04 0.5/0.56 1/1",
        "eq=saturation=0.95",
    ],
    "vivid": ["eq=contrast=1.10:saturation=1.50"],
    # Faded matte: lifted blacks, softened whites, less saturation, a hint of warmth.
    "film": [
        "curves=m=0/0.07 0.25/0.26 0.75/0.73 1/0.92:r=0/0.02 0.5/0.52 1/1:b=0/0 0.5/0.48 1/0.97",
        "eq=saturation=0.78",
    ],
    "bw": ["eq=saturation=0:contrast=1.08"],
    # Darker, more contrast, slight vignette.
    "moody": [
        "eq=brightness=-0.04:contrast=1.2:saturation=0.8",
        "vignette=angle=PI/7",
    ],
}

# Gradient strip height as a fraction of the frame height, and its darkest alpha.
GRADIENT_FRACTION = 0.35
GRADIENT_MAX_ALPHA = 0.72


def look_filters(look: str) -> list[str]:
    try:
        return LOOK_FILTERS[look]
    except KeyError:
        raise VideoMCPError(f"look must be one of {', '.join(LOOK_FILTERS)}; got {look!r}.")


def gradient_edges(gradient: str) -> list[str]:
    return {"none": [], "bottom": ["bottom"], "top": ["top"], "both": ["top", "bottom"]}[gradient]


def gradient_png(cache: Path, edge: str, width: int, height: int) -> Path:
    """A black-to-transparent RGBA strip covering 35% of a `height`-tall frame,
    darkest at `edge`. Generated once per size with Pillow and kept in cache/."""
    from PIL import Image

    strip_h = max(2, int(round(height * GRADIENT_FRACTION)))
    out_dir = cache / "gradients"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{edge}-{width}x{strip_h}-v1.png"
    if out.is_file():
        return out
    alpha = Image.new("L", (1, strip_h))
    for y in range(strip_h):
        # 0 at the inner edge, 1 at the frame edge; eased so it fades out softly.
        pos = (y + 0.5) / strip_h
        if edge == "top":
            pos = 1 - pos
        alpha.putpixel((0, y), int(round(255 * GRADIENT_MAX_ALPHA * pos ** 1.6)))
    img = Image.new("RGBA", (width, strip_h), (0, 0, 0, 0))
    img.putalpha(alpha.resize((width, strip_h)))
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".png")
    os.close(fd)
    img.save(tmp, format="PNG")
    os.replace(tmp, out)
    return out
