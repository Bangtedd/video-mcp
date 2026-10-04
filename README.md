# video-mcp

An MCP server that lets Claude edit video by driving FFmpeg. Claude is the editor and there is no GUI. You drop phone clips into an inbox, Claude builds a timeline with tool calls, and `render` encodes it once with a single FFmpeg `filter_complex`.

It's built for a Raspberry Pi 5 running Raspberry Pi OS Bookworm (64-bit), with FFmpeg 5.1 from apt and software `libx264` encoding.

## Install (Raspberry Pi OS Bookworm)

```bash
sudo apt update
sudo apt install -y git ffmpeg fonts-dejavu-core python3-venv

git clone https://github.com/bangtedd/video-mcp.git ~/video-mcp
cd ~/video-mcp
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[test]'

cp .env.example .env        # optional; the defaults work as they are
```

Bookworm's system Python is "externally managed", so install into a virtualenv (as above), not with `sudo pip`.

Check the install (this generates test media with FFmpeg and renders it for real):

```bash
.venv/bin/pytest
```

## Run

```bash
~/video-mcp/.venv/bin/video-mcp            # streamable HTTP on http://127.0.0.1:8765/mcp
~/video-mcp/.venv/bin/video-mcp --stdio    # stdio transport
```

On the first start the server creates the workspace at `~/video-mcp/workspace`:

```
workspace/
  inbox/      put source clips and music here
  projects/   one <name>.json timeline per project
  renders/    output videos
  cache/      frames and temp files
```

### Add it to Claude Code

```bash
claude mcp add --transport http video-mcp http://127.0.0.1:8765/mcp
```

If you'd rather have Claude Code launch the server itself over stdio:

```bash
claude mcp add video-mcp -- ~/video-mcp/.venv/bin/video-mcp --stdio
```

### Run as a service

```bash
mkdir -p ~/.config/systemd/user
cp ~/video-mcp/deploy/video-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now video-mcp
sudo loginctl enable-linger "$USER"     # keep running after logout
journalctl --user -u video-mcp -f       # logs
```

### Configuration

Settings are environment variables. The service reads them from `~/video-mcp/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `WORKSPACE_DIR` | `~/video-mcp/workspace` | Root for inbox/projects/renders/cache |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8765` | Bind port |
| `AUTH_TOKEN` | unset | Bearer token. Required if `HOST` is not loopback; the server refuses to start without it |
| `FONT_FILE` | `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` | Font for text overlays |
| `FFMPEG`, `FFPROBE` | `ffmpeg`, `ffprobe` | Binaries |

To reach the server from another machine, set `HOST=0.0.0.0` and `AUTH_TOKEN=$(openssl rand -hex 32)`, then add it in Claude Code with the token:

```bash
claude mcp add --transport http video-mcp http://<pi-address>:8765/mcp \
  --header "Authorization: Bearer <token>"
```

## How it works

Edits never touch media files. A project is a JSON timeline:

- `preset`: `vertical` 1080x1920, `square` 1080x1080 or `landscape` 1920x1080, all at 30 fps
- `fit`: `crop` (fill the frame, default) or `pad` (black bars)
- `clips`: each has `start`/`end` in source seconds, `speed` (0.25 to 4), and `volume` (0 to 2)
- `music`: optional bed mixed under the clip audio, with `volume`, `offset`, `fade_in` and `fade_out`. It's trimmed to the video length.
- `texts`: overlays with `start`/`end` on the output timeline, plus `position`, `size`, `color` and `box`

`render(project, "preview" | "final")` builds one FFmpeg command. It normalises every clip (scale, crop or pad, `setsar=1`, 30 fps, yuv420p, 48 kHz stereo), generates silence for clips with no audio, applies speed with `setpts` and chained `atempo`, concatenates, draws text with `drawtext` from temp text files, and mixes music with `amix` (with normalisation off). `final` uses `libx264 -preset veryfast -crf 20` with AAC 160k and `+faststart`. `preview` is half resolution, `ultrafast`, CRF 30, AAC 96k. One render runs at a time and the rest queue. Job history lives in memory.

File arguments are names in `inbox/` (`clip.mp4`) or workspace-relative paths (`renders/x.mp4`). Anything that resolves outside the workspace is rejected, including through symlinks.

Text notes: long lines wrap automatically inside an 8% safe margin, and newlines are kept. The default DejaVu font has no emoji glyphs, so emoji show up as boxes. Set `FONT_FILE` to a font that has them if you need emoji.

## Tools

| Tool | Purpose |
|---|---|
| `list_media()` | Files in `inbox/` with duration, resolution, fps, has_audio |
| `probe_media(file)` | Full details for one file, including rotation |
| `get_frame(file, time)` | JPEG frame (long side ≤ 768 px) as image content. Works on inbox files and renders |
| `create_project(name, preset, fit)` | New empty project |
| `list_projects()` | Names and output durations |
| `get_project(name)` | Timeline, output duration, each clip's timeline position, and any `problems` |
| `add_clip(project, file, start?, end?, position?)` | Defaults: whole file, appended at the end. `position` is a 0-based index |
| `update_clip(project, clip_id, start?, end?, speed?, volume?)` | |
| `move_clip(project, clip_id, position)` | |
| `remove_clip(project, clip_id)` | |
| `set_music(project, file, volume, offset, fade_in, fade_out)` | `file=null` removes it |
| `add_text(project, text, start, end, position, size, color, box)` | |
| `update_text(project, text_id, ...)` | |
| `remove_text(project, text_id)` | |
| `render(project, quality)` | Returns a `job_id` right away |
| `get_job(job_id)` | `status`, `progress`, `output`, `error`; on failure, the last 20 lines of FFmpeg stderr |

## Worked example

Three phone clips in `inbox/`: `beach.mp4` (landscape, 12 s), `selfie.mov` (portrait, 8 s, with rotation metadata) and `drone.mp4` (landscape, 20 s, no audio), plus `song.mp3`. The goal is a 15-second vertical reel.

```jsonc
list_media()
// -> files: beach.mp4 12.0s 1920x1080 audio, selfie.mov 8.0s 1080x1920 audio,
//           drone.mp4 20.0s 3840x2160 no audio, song.mp3 184.2s

get_frame(file="drone.mp4", time=9)          // look at the footage
create_project(name="weekend", preset="vertical", fit="crop")

add_clip(project="weekend", file="beach.mp4", start=2, end=7)     // -> clip c1, 5 s
add_clip(project="weekend", file="selfie.mov")                     // -> clip c2, 8 s
add_clip(project="weekend", file="drone.mp4", start=4, end=12)     // -> clip c3
update_clip(project="weekend", clip_id="c3", speed=2.0)            // 8 s of source -> 4 s
update_clip(project="weekend", clip_id="c2", start=1, end=7)       // trim the selfie to 6 s
move_clip(project="weekend", clip_id="c3", position=0)             // open on the drone shot

set_music(project="weekend", file="song.mp3", volume=0.3, offset=30, fade_in=1, fade_out=2)

get_project(name="weekend")                  // -> output_duration: 15.0
add_text(project="weekend", text="Weekend at the coast", start=0, end=3,
         position="top", size="large", color="white", box=true)
add_text(project="weekend", text="see you next time ☀", start=12, end=15,
         position="bottom", size="medium", color="#ffcc00")

render(project="weekend", quality="preview") // -> {job_id: "a1b2c3", status: "queued"}
get_job(job_id="a1b2c3")                     // -> running, progress 42 ... then done,
                                             //    output: "renders/weekend-preview-....mp4"
get_frame(file="renders/weekend-preview-....mp4", time=1.5)   // check the title overlay

render(project="weekend", quality="final")   // 1080x1920, 15 s, H.264 + AAC
get_job(job_id="...")                        // -> done, output: "renders/weekend-final-....mp4"
```

## Development

```bash
.venv/bin/pytest -q
```

The tests generate all their media at run time with FFmpeg `lavfi` sources (`testsrc`, `testsrc2`, `sine`), so no binary files are checked in. They render for real and check the results with `ffprobe`: resolution, duration within 0.2 s, and audio levels. They also start the real server and drive it over MCP via both HTTP and stdio.
