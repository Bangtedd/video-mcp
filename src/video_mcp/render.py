"""Turn a project timeline into one FFmpeg command with a single filter_complex."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import VideoMCPError
from .timeline import FPS, PRESETS, clip_output_duration, frames_for, timeline_problems
from .workspace import Workspace

SAMPLE_RATE = 48000
SAFE_MARGIN = 0.08
# Font size as a fraction of the frame's short side.
TEXT_SIZE_FRACTION = {"small": 0.037, "medium": 0.055, "large": 0.08}
# Average advance width of DejaVu Sans Bold glyphs relative to font size (a bit
# pessimistic so lines reliably fit inside the safe area).
AVG_CHAR_WIDTH = 0.62

QUALITY = {
    "final": {"scale": 1, "preset": "veryfast", "crf": "20", "audio_bitrate": "160k"},
    "preview": {"scale": 2, "preset": "ultrafast", "crf": "30", "audio_bitrate": "96k"},
}

AFMT = f"aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=stereo"


def escape_option(value: str) -> str:
    """Escape a value for both the filter option parser and the filtergraph parser."""
    value = re.sub(r"([\\':])", r"\\\1", value)
    return re.sub(r"([\\'\[\],;])", r"\\\1", value)


def atempo_chain(speed: float) -> list[str]:
    """atempo filters (each within 0.5..2.0) whose product equals `speed`."""
    filters = []
    s = speed
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-9:
        filters.append(f"atempo={s:.6f}")
    return filters


def wrap_text(text: str, max_chars: int) -> str:
    """Greedy word wrap that keeps explicit newlines; hard-breaks long words."""
    max_chars = max(1, max_chars)
    out_lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        line = ""
        for word in words:
            while len(word) > max_chars:
                if line:
                    out_lines.append(line)
                    line = ""
                out_lines.append(word[:max_chars])
                word = word[max_chars:]
            candidate = f"{line} {word}" if line else word
            if len(candidate) <= max_chars:
                line = candidate
            else:
                out_lines.append(line)
                line = word
        out_lines.append(line)
    return "\n".join(out_lines)


@dataclass
class RenderPlan:
    args: list[str]
    duration: float
    width: int
    height: int
    filter_complex: str


def build_render(
    project: dict,
    ws: Workspace,
    cfg: Config,
    quality: str,
    output: Path,
    work_dir: Path,
) -> RenderPlan:
    if quality not in QUALITY:
        raise VideoMCPError("quality must be 'preview' or 'final'.")
    problems = timeline_problems(project)
    if problems:
        raise VideoMCPError("Cannot render: " + " ".join(problems))
    q = QUALITY[quality]
    full_w, full_h = PRESETS[project["preset"]]
    W, H = full_w // q["scale"], full_h // q["scale"]
    fit = project["fit"]

    inputs: list[str] = []
    chains: list[str] = []
    concat_pads: list[str] = []
    total_frames = 0

    for i, clip in enumerate(project["clips"]):
        src = ws.resolve_file(clip["file"])
        speed = float(clip["speed"])
        src_len = clip["end"] - clip["start"]
        frames = frames_for(clip_output_duration(clip))
        seg = frames / FPS
        samples = int(round(seg * SAMPLE_RATE))
        total_frames += frames
        inputs += ["-ss", f"{clip['start']:.3f}", "-t", f"{src_len:.3f}", "-i", str(src)]

        if fit == "crop":
            geom = (
                f"scale={W}:{H}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                f"crop={W}:{H}"
            )
        else:
            geom = (
                f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        chains.append(
            f"[{i}:v:0]setpts=(PTS-STARTPTS)/{speed:.6f},fps={FPS},{geom},setsar=1,"
            f"format=yuv420p,tpad=stop_mode=clone:stop_duration=2,"
            f"trim=end_frame={frames},setpts=PTS-STARTPTS[v{i}]"
        )

        if clip.get("has_audio"):
            afilters = ["asetpts=PTS-STARTPTS", f"aresample={SAMPLE_RATE}", AFMT]
            afilters += atempo_chain(speed)
            afilters.append(f"volume={float(clip['volume']):.4f}")
            afilters += ["apad", f"atrim=end_sample={samples}", "asetpts=PTS-STARTPTS"]
            chains.append(f"[{i}:a:0]" + ",".join(afilters) + f"[a{i}]")
        else:
            chains.append(
                f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end_sample={samples},"
                f"{AFMT},asetpts=PTS-STARTPTS[a{i}]"
            )
        concat_pads.append(f"[v{i}][a{i}]")

    n = len(project["clips"])
    duration = total_frames / FPS
    chains.append("".join(concat_pads) + f"concat=n={n}:v=1:a=1[vcat][acat]")

    # Text overlays.
    video_label = "vcat"
    texts = project.get("texts") or []
    if texts:
        font = Path(cfg.font_file)
        if not font.is_file():
            raise VideoMCPError(
                f"Font file {cfg.font_file} not found. Install fonts-dejavu-core or set FONT_FILE."
            )
        short = min(W, H)
        draws = []
        for t in texts:
            size = max(8, int(round(short * TEXT_SIZE_FRACTION[t["size"]])))
            max_chars = int((W * (1 - 2 * SAFE_MARGIN)) / (size * AVG_CHAR_WIDTH))
            textfile = work_dir / f"{t['id']}.txt"
            textfile.write_text(wrap_text(t["text"], max_chars), encoding="utf-8")
            y = {
                "top": f"h*{SAFE_MARGIN}",
                "center": "(h-text_h)/2",
                "bottom": f"h*{1 - SAFE_MARGIN}-text_h",
            }[t["position"]]
            enable = f"between(t,{t['start']:.3f},{t['end']:.3f})"
            opts = [
                f"fontfile={escape_option(str(font))}",
                f"textfile={escape_option(str(textfile))}",
                "expansion=none",
                f"fontsize={size}",
                f"fontcolor={escape_option(t['color'])}",
                f"line_spacing={int(size * 0.2)}",
                f"x={escape_option(f'max((w-text_w)/2,w*{SAFE_MARGIN})')}",
                f"y={escape_option(y)}",
                f"enable={escape_option(enable)}",
            ]
            if t.get("box"):
                opts += [
                    "box=1",
                    f"boxcolor={escape_option('black@0.5')}",
                    f"boxborderw={max(2, int(size * 0.3))}",
                ]
            draws.append("drawtext=" + ":".join(opts))
        chains.append(f"[vcat]{','.join(draws)}[vtxt]")
        video_label = "vtxt"

    # Music bed.
    audio_label = "acat"
    music = project.get("music")
    if music:
        mpath = ws.resolve_file(music["file"])
        available = music["duration"] - music["offset"]
        if available <= 0:
            raise VideoMCPError(
                "Music offset is past the end of the music file. Lower it with set_music()."
            )
        m_len = min(duration, available)
        mi = n
        inputs += ["-ss", f"{music['offset']:.3f}", "-i", str(mpath)]
        mf = ["asetpts=PTS-STARTPTS", f"aresample={SAMPLE_RATE}", AFMT,
              f"volume={float(music['volume']):.4f}", f"atrim=duration={m_len:.6f}"]
        fade_in = min(float(music.get("fade_in") or 0), m_len)
        fade_out = min(float(music.get("fade_out") or 0), m_len)
        if fade_in > 0:
            mf.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            mf.append(f"afade=t=out:st={m_len - fade_out:.3f}:d={fade_out:.3f}")
        chains.append(f"[{mi}:a:0]" + ",".join(mf) + "[mus]")
        chains.append(
            "[acat][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amix]"
        )
        audio_label = "amix"

    filter_complex = ";\n".join(chains)
    args = [
        cfg.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "warning", "-nostats",
        "-progress", "pipe:1",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{video_label}]", "-map", f"[{audio_label}]",
        "-c:v", "libx264", "-preset", q["preset"], "-crf", q["crf"],
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", q["audio_bitrate"], "-ar", str(SAMPLE_RATE), "-ac", "2",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        "-f", "mp4", str(output),
    ]
    return RenderPlan(args=args, duration=duration, width=W, height=H, filter_complex=filter_complex)
