# video-mcp: spec

An MCP server that lets Claude edit video by driving FFmpeg. Claude is the editor; there is no GUI. Runs on a Raspberry Pi 5 and is used from Claude Code (and later from a phone via a bot, which is out of scope here).

## Target environment

- Raspberry Pi 5, Raspberry Pi OS Bookworm, aarch64, Python 3.11
- FFmpeg 5.1 from apt (`ffmpeg`, `ffprobe`). Do not rely on features newer than 5.1.
- No hardware H.264 encoder on the Pi 5. Encode with `libx264` in software.
- Typical input: phone clips, 5 to 90 seconds, 1080p or 4K, portrait or landscape, sometimes with rotation metadata, sometimes with no audio track.

## Stack

- Python 3.11, official MCP Python SDK (`mcp` package, FastMCP)
- FFmpeg via `subprocess` with argument lists. Never `shell=True`.
- Transports: streamable HTTP (default) and stdio (flag `--stdio`)
- Dependencies managed with `pyproject.toml`; installable with `pip install -e .`
- Tests with `pytest`

## Core design: timeline, render once

Edits do not touch media files. Each project is a JSON timeline. Tools modify the timeline; `render` turns it into one FFmpeg command with a single `filter_complex`, so the video is encoded once no matter how many edits were made.

A project has:

- `preset`: `vertical` (1080x1920), `square` (1080x1080), or `landscape` (1920x1080); 30 fps
- `fit`: `crop` (fill frame, crop overflow) or `pad` (fit inside, black bars). Default `crop`.
- `clips`: ordered list. Each clip: `id`, `file`, `start`, `end` (seconds in the source), `speed` (0.25 to 4.0, default 1.0), `volume` (0.0 to 2.0, default 1.0)
- `music`: optional. `file`, `volume` (default 0.3), `offset` (seconds into the music file), `fade_in`, `fade_out`. Trimmed to the video length, never extends it.
- `texts`: list of overlays. Each: `id`, `text`, `start`, `end` (seconds on the output timeline), `position` (`top`, `center`, `bottom`), `size` (`small`, `medium`, `large`), `color`, `box` (bool, semi-transparent background)

## Workspace

All files live under one directory (`WORKSPACE_DIR`, default `~/video-mcp/workspace`):

```
workspace/
  inbox/      source clips and music the user drops in
  projects/   one <name>.json per project
  renders/    output videos
  cache/      frames and temp files
```

## Tools

Every tool returns structured JSON. Errors return a clear message that says what to fix.

| Tool | Purpose |
|---|---|
| `list_media()` | Files in `inbox/` with duration, resolution, fps, has_audio |
| `probe_media(file)` | Full details for one file |
| `get_frame(file, time)` | Returns a JPEG frame as MCP image content so Claude can see the footage. Works on inbox files and renders. Scale to max 768 px on the long side. |
| `create_project(name, preset, fit)` | New empty project |
| `list_projects()` | Names and total durations |
| `get_project(name)` | Full timeline plus computed output duration |
| `add_clip(project, file, start, end, position)` | `start`/`end` optional (default whole file); `position` optional (default end) |
| `update_clip(project, clip_id, ...)` | Change start, end, speed, volume |
| `move_clip(project, clip_id, position)` | Reorder |
| `remove_clip(project, clip_id)` | |
| `set_music(project, file, volume, offset, fade_in, fade_out)` | Pass `file=null` to remove |
| `add_text(project, text, start, end, position, size, color, box)` | |
| `update_text(project, text_id, ...)` | |
| `remove_text(project, text_id)` | |
| `render(project, quality)` | `preview` or `final`. Starts a background job, returns `job_id` immediately. |
| `get_job(job_id)` | `status` (queued, running, done, failed), `progress` 0 to 100, `output` path, `error` |

## Rendering rules

- Normalize every clip before concat: scale and crop/pad to the preset size, `setsar=1`, 30 fps, `yuv420p`; audio to 48 kHz stereo.
- A clip with no audio gets generated silence (`anullsrc`) so concat always has matching streams.
- Respect rotation metadata (FFmpeg autorotate is on by default; do not disable it).
- Speed: `setpts` for video, chained `atempo` for audio (each `atempo` stays within 0.5 to 2.0).
- Music is mixed under the clip audio with `amix`, with normalization off so clip volume does not drop.
- Text uses `drawtext` with `textfile=` pointing at a temp file. Do not inline user text into the filter string. Font: DejaVu Sans Bold (`/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`), overridable with `FONT_FILE`. Keep text inside a safe margin of 8% from the frame edges.
- `final`: `libx264 -preset veryfast -crf 20`, AAC 160k, `+faststart`.
- `preview`: half resolution, `-preset ultrafast -crf 30`, AAC 96k.
- Progress comes from FFmpeg's `-progress pipe:1` output against the computed duration.
- One render runs at a time; others queue. Jobs are kept in memory; a restart losing job history is acceptable.
- On failure, `get_job` returns the last 20 lines of FFmpeg stderr.

## Safety

- Every file argument is resolved and must stay inside `WORKSPACE_DIR`. Reject `..`, absolute paths outside it, and symlinks that escape it.
- Project names: letters, digits, `-`, `_` only.
- HTTP binds to `127.0.0.1:8765` by default. `HOST` and `PORT` env vars override. If `HOST` is not loopback, refuse to start unless `AUTH_TOKEN` is set, and then require it as a bearer token.
- Validate all numeric ranges; reject `end <= start`, times past the end of a file, and overlays past the end of the timeline.

## Tests

- Generate fixtures at test time with FFmpeg `lavfi` sources (`testsrc`, `sine`): a landscape clip with audio, a portrait clip with audio, a clip with no audio, a music file. No binary files in the repo.
- Verify outputs with `ffprobe`: resolution, duration (within 0.2 s), presence of audio.
- Cover: mixed-orientation concat, trim, speed change, silent clip in the middle, music mix and fade, text with awkward characters (`:`, `'`, `%`, `\`, emoji, multi-line), path traversal rejection, render job lifecycle, failed render reporting.
- Install `ffmpeg` and `fonts-dejavu-core` in the session with apt before running tests. If that is not possible, stop and say so. Do not skip or mock the FFmpeg tests.

## Deliverables

- `src/video_mcp/` package with a `video-mcp` console entry point
- `tests/`
- `README.md`: install on Raspberry Pi OS Bookworm, run, the `claude mcp add --transport http video-mcp http://127.0.0.1:8765/mcp` command, and one worked example of a full edit as a sequence of tool calls
- `deploy/video-mcp.service`: systemd user unit
- `.env.example`

## Done means

1. `pytest` passes with real FFmpeg.
2. Starting the server and calling the tools in order (create project, add three mixed clips, trim one, set music, add two texts, render final) produces a playable 1080x1920 MP4 with the right duration.
3. `get_frame` on that render returns an image showing the text overlay.
4. README steps work on a clean Bookworm install.

## Out of scope

Telegram bot, Android app, web UI, transitions, filters and effects, multiple video tracks, keyframes, subtitles from speech, AI generation of any media, user accounts.
