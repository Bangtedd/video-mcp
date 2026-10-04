"""Templates: JSON recipes in templates/ that turn a list of clips into a project."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import VideoMCPError
from .timeline import (
    FADE_RANGE,
    FITS,
    GRADIENTS,
    LOOKS,
    PRESETS,
    TEXT_POSITIONS,
    TEXT_SIZES,
    TRANSITION_RANGE,
    TRANSITIONS,
    VOLUME_RANGE,
    check_choice,
    check_color,
    check_nonneg,
    check_range,
    timeline_duration,
)

TEMPLATE_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
TEXT_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
SLOT_RANGE = (0.3, 30.0)
MAX_SLOTS = 50


def check_template_id(template_id) -> str:
    if not isinstance(template_id, str) or not TEMPLATE_ID_RE.match(template_id):
        raise VideoMCPError(
            f"Invalid template id {template_id!r}. Use one of the ids from list_templates()."
        )
    return template_id


def _bool(name: str, value) -> bool:
    if not isinstance(value, bool):
        raise VideoMCPError(f"{name} must be true or false.")
    return value


def validate(data: dict, source: str = "template") -> dict:
    """Check a template dict and return it with defaults filled in."""
    if not isinstance(data, dict):
        raise VideoMCPError(f"{source}: must be a JSON object.")
    try:
        t = {
            "id": check_template_id(data["id"]),
            "name": str(data["name"]),
            "description": str(data.get("description", "")),
            "preset": check_choice("preset", data.get("preset", "vertical"), tuple(PRESETS)),
            "fit": check_choice("fit", data.get("fit", "crop"), FITS),
            "look": check_choice("look", data.get("look", "none"), LOOKS),
            "gradient": check_choice("gradient", data.get("gradient", "none"), GRADIENTS),
            "fade_in": check_range("fade_in", data.get("fade_in", 0), *FADE_RANGE),
            "fade_out": check_range("fade_out", data.get("fade_out", 0), *FADE_RANGE),
            "clip_volume": check_range("clip_volume", data.get("clip_volume", 1.0), *VOLUME_RANGE),
        }
        tr = data.get("transition") or {"type": "cut"}
        t["transition"] = {
            "type": check_choice("transition.type", tr.get("type", "cut"), TRANSITIONS),
            "duration": check_range("transition.duration", tr.get("duration", 0.5), *TRANSITION_RANGE),
        }
        slots = data["slots"]
        if not isinstance(slots, list) or not 1 <= len(slots) <= MAX_SLOTS:
            raise VideoMCPError(f"slots must be a list of 1 to {MAX_SLOTS} lengths in seconds.")
        t["slots"] = [check_range("slot", s, *SLOT_RANGE) for s in slots]
        music = data.get("music") or {}
        t["music"] = {
            "volume": check_range("music.volume", music.get("volume", 0.3), *VOLUME_RANGE),
            "fade_in": check_range("music.fade_in", music.get("fade_in", 0), 0, 60),
            "fade_out": check_range("music.fade_out", music.get("fade_out", 0), 0, 60),
        }
        texts = []
        for f in data.get("texts") or []:
            key = f["key"]
            if not isinstance(key, str) or not TEXT_KEY_RE.match(key):
                raise VideoMCPError(f"text key {key!r} must be lowercase letters, digits or '_'.")
            if ("from_start" in f) == ("from_end" in f):
                raise VideoMCPError(f"text {key!r}: give exactly one of from_start or from_end.")
            field = {
                "key": key,
                "label": str(f.get("label", key)),
                "placeholder": str(f.get("placeholder", "")),
                "position": check_choice("position", f.get("position", "bottom"), TEXT_POSITIONS),
                "size": check_choice("size", f.get("size", "medium"), TEXT_SIZES),
                "color": check_color(f.get("color", "white")),
                "box": _bool("box", f.get("box", False)),
                "fade": _bool("fade", f.get("fade", False)),
                "duration": check_range("duration", f["duration"], 0.3, 600),
            }
            if "from_start" in f:
                field["from_start"] = check_nonneg("from_start", f["from_start"])
            else:
                field["from_end"] = check_nonneg("from_end", f["from_end"])
            texts.append(field)
        if len({f["key"] for f in texts}) != len(texts):
            raise VideoMCPError("text keys must be unique.")
        t["texts"] = texts
    except KeyError as exc:
        raise VideoMCPError(f"{source}: missing field {exc.args[0]!r}.") from None
    except VideoMCPError as exc:
        raise VideoMCPError(f"{source}: {exc}") from None
    t["nominal_duration"] = nominal_duration(t)
    return t


def nominal_duration(t: dict, n_clips: int | None = None) -> float:
    """Output length when every slot is filled by a long enough clip."""
    n = n_clips or len(t["slots"])
    clips = [
        {"start": 0.0, "end": t["slots"][k % len(t["slots"])], "speed": 1.0} for k in range(n)
    ]
    return timeline_duration({"clips": clips, "transition": t["transition"]})


def load_templates(directory: Path) -> tuple[dict[str, dict], list[str]]:
    """Valid templates by id, plus one message per file that failed to load."""
    out: dict[str, dict] = {}
    errors: list[str] = []
    if not directory.is_dir():
        return out, [f"Template directory {directory} does not exist."]
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: not valid JSON ({exc}).")
            continue
        try:
            t = validate(data, source=path.name)
        except VideoMCPError as exc:
            errors.append(str(exc))
            continue
        if t["id"] != path.stem:
            errors.append(f"{path.name}: id {t['id']!r} must match the file name.")
            continue
        out[t["id"]] = t
    return out, errors


def text_timing(field: dict, total: float) -> tuple[float, float] | None:
    """(start, end) on a timeline of length `total`, or None if it doesn't fit."""
    if "from_start" in field:
        start = field["from_start"]
        end = min(total, start + field["duration"])
    else:
        end = total - field["from_end"]
        start = max(0.0, end - field["duration"])
    start, end = round(start, 3), round(end, 3)
    if end - start < 0.3 or start < 0:
        return None
    return start, end
