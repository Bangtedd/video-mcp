"""Project timeline model: constants, validation and duration math."""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import VideoMCPError

PRESETS: dict[str, tuple[int, int]] = {
    "vertical": (1080, 1920),
    "square": (1080, 1080),
    "landscape": (1920, 1080),
}
FPS = 30
FITS = ("crop", "pad")
TEXT_POSITIONS = ("top", "center", "bottom")
TEXT_SIZES = ("small", "medium", "large")
SPEED_RANGE = (0.25, 4.0)
VOLUME_RANGE = (0.0, 2.0)
MAX_TEXT_LEN = 500
TRANSITIONS = ("cut", "fade", "slide_left", "slide_up", "zoom")
TRANSITION_RANGE = (0.2, 1.0)
DEFAULT_TRANSITION = {"type": "cut", "duration": 0.5}
LOOKS = ("none", "warm", "cool", "vivid", "film", "bw", "moody")
GRADIENTS = ("none", "bottom", "top", "both")
FADE_RANGE = (0.0, 2.0)
# Fade in/out time for text overlays with fade=true.
TEXT_FADE = 0.3
# Allow tiny float noise when comparing against file / timeline ends.
EPS = 1e-3

NAMED_COLORS = {
    "white", "black", "red", "green", "blue", "yellow", "orange", "purple",
    "pink", "cyan", "magenta", "gray", "grey", "silver", "gold", "lime",
    "navy", "teal", "maroon", "olive", "brown", "violet", "indigo", "coral",
    "salmon", "turquoise", "beige", "ivory", "khaki", "lavender", "crimson",
}
HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoMCPError(f"{name} must be a number, got {value!r}.")
    value = float(value)
    if not math.isfinite(value):
        raise VideoMCPError(f"{name} must be a finite number.")
    return value


def check_range(name: str, value: Any, lo: float, hi: float) -> float:
    value = _number(name, value)
    if not lo <= value <= hi:
        raise VideoMCPError(f"{name} must be between {lo:g} and {hi:g}, got {value:g}.")
    return value


def check_nonneg(name: str, value: Any) -> float:
    value = _number(name, value)
    if value < 0:
        raise VideoMCPError(f"{name} must be >= 0, got {value:g}.")
    return value


def check_choice(name: str, value: Any, choices: tuple[str, ...] | list[str]) -> str:
    if value not in choices:
        raise VideoMCPError(f"{name} must be one of {', '.join(choices)}; got {value!r}.")
    return value


def check_color(value: Any) -> str:
    if not isinstance(value, str):
        raise VideoMCPError("color must be a string like 'white' or '#ffcc00'.")
    v = value.strip()
    if v.lower() in NAMED_COLORS:
        return v.lower()
    if HEX_COLOR_RE.match(v):
        return v.lower()
    raise VideoMCPError(
        f"Unknown color {value!r}. Use a hex value like '#ffcc00' or one of: "
        + ", ".join(sorted(NAMED_COLORS))
        + "."
    )


def check_text(value: Any) -> str:
    if not isinstance(value, str):
        raise VideoMCPError("text must be a string.")
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if any(ord(ch) < 32 and ch != "\n" for ch in text):
        raise VideoMCPError("text contains control characters; only newlines are allowed.")
    text = text.strip("\n")
    if not text.strip():
        raise VideoMCPError("text is empty.")
    if len(text) > MAX_TEXT_LEN:
        raise VideoMCPError(f"text is too long ({len(text)} chars); max is {MAX_TEXT_LEN}.")
    return text


def check_span(label: str, start: float, end: float, limit: float, limit_desc: str) -> None:
    if start < 0:
        raise VideoMCPError(f"{label}: start must be >= 0, got {start:g}.")
    if end <= start:
        raise VideoMCPError(f"{label}: end ({end:g}) must be greater than start ({start:g}).")
    if end > limit + EPS:
        raise VideoMCPError(
            f"{label}: end ({end:g}s) is past the end of {limit_desc} ({limit:.3f}s). "
            f"Use an end of at most {limit:.3f}."
        )


def clip_output_duration(clip: dict) -> float:
    return (clip["end"] - clip["start"]) / clip["speed"]


def frames_for(seconds: float) -> int:
    """Number of output frames a clip of `seconds` occupies (at least one)."""
    return max(1, int(round(seconds * FPS)))


def transition_of(project: dict) -> dict:
    """The project's transition; projects from before v2 have none (hard cuts)."""
    return project.get("transition") or dict(DEFAULT_TRANSITION)


def clip_frames(project: dict) -> list[int]:
    return [frames_for(clip_output_duration(c)) for c in project["clips"]]


def join_overlaps(project: dict) -> list[int]:
    """Overlap in frames for each join between consecutive clips.

    A transition overlaps the two clips it joins. If either clip is shorter
    than twice the transition duration, that join falls back to a hard cut (0),
    so a clip never has to take part in two overlapping transitions.
    """
    frames = clip_frames(project)
    tr = transition_of(project)
    if tr["type"] == "cut":
        return [0] * max(0, len(frames) - 1)
    d = max(1, int(round(tr["duration"] * FPS)))
    return [
        d if frames[i] >= 2 * d and frames[i + 1] >= 2 * d else 0
        for i in range(len(frames) - 1)
    ]


def timeline_frames(project: dict) -> int:
    return sum(clip_frames(project)) - sum(join_overlaps(project))


def timeline_duration(project: dict) -> float:
    """Output duration, accounting for frame quantisation at 30 fps and for
    transitions, which overlap neighbouring clips and so shorten the video."""
    return round(timeline_frames(project) / FPS, 3)


def timeline_problems(project: dict) -> list[str]:
    """Problems that block rendering but can arise after valid edits
    (e.g. a clip got shorter so an overlay now runs past the end)."""
    problems = []
    if not project["clips"]:
        problems.append("The project has no clips. Add one with add_clip().")
        return problems
    total = timeline_duration(project)
    for t in project["texts"]:
        if t["end"] > total + EPS:
            problems.append(
                f"Text {t['id']} ends at {t['end']:g}s but the timeline is only {total:.3f}s. "
                "Shorten it with update_text() or remove it."
            )
    return problems


def summarize(project: dict) -> dict:
    out = dict(project)
    out["output_duration"] = timeline_duration(project) if project["clips"] else 0.0
    out["width"], out["height"] = PRESETS[project["preset"]]
    out["fps"] = FPS
    out["transition"] = transition_of(project)
    out.setdefault("look", "none")
    out.setdefault("gradient", "none")
    out.setdefault("fade_in", 0.0)
    out.setdefault("fade_out", 0.0)
    pos = 0
    clips = []
    overlaps = join_overlaps(project)
    for i, (c, frames) in enumerate(zip(project["clips"], clip_frames(project))):
        if i > 0:
            pos -= overlaps[i - 1]
        clips.append(
            {**c, "timeline_start": round(pos / FPS, 3),
             "timeline_end": round((pos + frames) / FPS, 3)}
        )
        pos += frames
    out["clips"] = clips
    out["transitions"] = [
        {"after_clip": project["clips"][i]["id"],
         "type": transition_of(project)["type"] if ov else "cut",
         "overlap": round(ov / FPS, 3)}
        for i, ov in enumerate(overlaps)
    ]
    problems = timeline_problems(project)
    if problems:
        out["problems"] = problems
    return out
