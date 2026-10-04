# video-mcp

An MCP server that lets Claude edit video by driving FFmpeg. Claude is the editor. You drop phone clips into an inbox, Claude builds a timeline with tool calls, and `render` encodes it once with a single FFmpeg `filter_complex`.

There's also a small web app, `video-mcp-web`, for when you don't want Claude in the loop: open it on your phone, upload clips, tap a template, type a caption and download the finished video. See [Web app](#web-app).

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
| `TEMPLATES_DIR` | `templates/` in the repo | Template JSON files |
| `WEB_PASSWORD` | unset | Web app login password. Required; the web app refuses to start without it |
| `WEB_HOST` | `0.0.0.0` | Web app bind address |
| `WEB_PORT` | `8780` | Web app port |
| `WEB_MAX_UPLOAD_MB` | `500` | Per-file upload limit for the web app |

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
- `texts`: overlays with `start`/`end` on the output timeline, plus `position`, `size`, `color`, `box` and `fade` (0.3 s fade in and out)
- `transition`: `{type, duration}` between every pair of clips. `cut` (default), `fade`, `slide_left`, `slide_up` or `zoom`, 0.2 to 1 s. A transition overlaps the two clips, so the video gets shorter by its duration at every join, and `get_project` reports the shorter length. A join where either clip is shorter than twice the duration falls back to a cut.
- `look`: colour grade for the whole video. `none`, `warm`, `cool`, `vivid`, `film` (faded matte), `bw` or `moody` (darker, contrasty, slight vignette)
- `gradient`: `none`, `bottom`, `top` or `both`. A dark-to-transparent strip over 35% of the frame, drawn under the text so captions stay readable
- `fade_in`, `fade_out`: 0 to 2 s fade from and to black, picture and sound

`render(project, "preview" | "final")` builds one FFmpeg command. It normalises every clip (scale, crop or pad, `setsar=1`, 30 fps, yuv420p, 48 kHz stereo), generates silence for clips with no audio, applies speed with `setpts` and chained `atempo`, joins clips with `xfade` and `acrossfade` (or concat for cuts), applies the look (`eq`, `curves`, `vignette`), overlays the gradient (a PNG made with Pillow and cached in `cache/gradients/`), draws text with `drawtext` from temp text files, and mixes music with `amix` (with normalisation off). Intro and outro fades go on last. `final` uses `libx264 -preset veryfast -crf 20` with AAC 160k and `+faststart`. `preview` is half resolution, `ultrafast`, CRF 30, AAC 96k. One render runs at a time and the rest queue. The MCP server and the web app are separate processes, so renders also take an exclusive lock on `workspace/.render.lock`: only one FFmpeg render runs on the Pi at a time, whichever process started it. Job history lives in memory.

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
| `add_text(project, text, start, end, position, size, color, box, fade)` | |
| `update_text(project, text_id, ...)` | |
| `remove_text(project, text_id)` | |
| `set_transition(project, type, duration)` | `cut`, `fade`, `slide_left`, `slide_up`, `zoom`; 0.2 to 1 s |
| `set_look(project, look)` | `none`, `warm`, `cool`, `vivid`, `film`, `bw`, `moody` |
| `set_gradient(project, gradient)` | `none`, `bottom`, `top`, `both` |
| `set_fades(project, fade_in, fade_out)` | 0 to 2 s each |
| `suggest_segments(file, length, count)` | The `count` liveliest non-overlapping windows of `length` seconds |
| `list_templates()` | Templates with their text fields and nominal length |
| `apply_template(project, template_id, files, texts, music?)` | Build a whole project from a template |
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

The same footage with a template and the v2 finishing tools:

```jsonc
list_templates()                             // -> warm_promo: texts title, cta; about 9.5 s
apply_template(project="promo", template_id="warm_promo",
               files=["beach.mp4", "selfie.mov", "drone.mp4", "song.mp3"],
               texts={"title": "Weekend at the coast", "cta": "See you next time"})
// -> clips picked with suggest_segments, warm look, fades, bottom gradient, music at 0.35
set_transition(project="promo", type="slide_left", duration=0.4)   // still editable
set_fades(project="promo", fade_in=0.5, fade_out=1.5)
render(project="promo", quality="final")
```

## Segment picking

`suggest_segments(file, length, count)` decodes the clip once at 2 fps and 160 px wide and uses FFmpeg's scene-change score between samples as a motion measure. Windows are scored by average motion, with a penalty for near-static stretches, never start in the first 0.5 s or end in the last 0.5 s, and are chosen so that as many as possible fit without overlapping. The result is deterministic, and the per-file analysis is cached in `cache/segments/`. If the file is too short, completely static, or can't be analysed, it falls back to evenly spaced windows (`method: "even"`).

## Templates

A template turns a list of clips into a finished project. `apply_template(project, template_id, files, texts)` assigns the files to the template's slots in order. With fewer files than slots it cycles through them and uses a different segment of the file each time; with more files than slots the slot pattern repeats. Each segment comes from `suggest_segments`, and a clip shorter than its slot is used whole. An audio-only file in `files` (or the `music` argument) becomes the music bed. Text fields left empty are skipped. The result is an ordinary project, so you (or Claude) can still adjust it with the other tools. Applying a template to an existing project replaces its timeline.

Seven templates ship in `templates/`:

| id | Feel |
|---|---|
| `clean_cuts` | Hard cuts, 2 s slots, no look, one caption at the bottom |
| `warm_promo` | Warm look, fade transitions, bottom gradient, title at the start and a call to action at the end |
| `fast_hype` | 0.8 to 1.2 s slots, vivid look, hard cuts, big centred title for the first 2 s |
| `cinematic` | Film look, slow 1 s fades, 3 to 4 s slots, fade in and out, small bottom caption |
| `bw_mood` | Black and white, slide transitions, both gradients, title at the top |
| `product_showcase` | 2.5 s slots, moody look, bottom gradient, product name and price, call to action at the end |
| `story_vlog` | 4 to 5 s slots, cool look, zoom transitions, clip audio kept loud and music low |

### Writing a new template

Add `templates/<id>.json`. The file name must match the `id` (lowercase letters, digits and `_`). The web app and `list_templates` pick it up on the next request; there's nothing to restart.

```json
{
  "id": "beach_day",
  "name": "Beach day",
  "description": "Bright colours, quick fades and a caption at the bottom.",
  "preset": "vertical",
  "fit": "crop",
  "look": "vivid",
  "gradient": "bottom",
  "transition": {"type": "fade", "duration": 0.4},
  "fade_in": 0.5,
  "fade_out": 1.0,
  "slots": [2.5, 2, 2, 3],
  "music": {"volume": 0.4, "fade_in": 1, "fade_out": 2},
  "clip_volume": 0.5,
  "texts": [
    {"key": "caption", "label": "Caption", "placeholder": "Sun's out",
     "position": "bottom", "size": "medium", "color": "white", "box": true, "fade": true,
     "from_start": 0.5, "duration": 3},
    {"key": "outro", "label": "Last words", "position": "center", "size": "large",
     "color": "#ffd166", "fade": true, "from_end": 0.3, "duration": 2}
  ]
}
```

- `preset`, `fit`, `look`, `gradient`, `transition`, `fade_in` and `fade_out` take the same values as the project fields above.
- `slots` are clip lengths in seconds, in order. The template's length is the sum of the slots minus one transition overlap per join.
- `music` applies only when the user supplies a music file. `clip_volume` is the volume of the clips' own sound (0 mutes it).
- Each text field needs a `key` (what `apply_template`'s `texts` uses), a `label` for the web form, and timing: `from_start` (seconds after the start) or `from_end` (seconds before the end, measured to the text's end), plus `duration`. Timing is clipped to the video. `position` is `top`, `center` or `bottom`; `size` is `small`, `medium` or `large`; `color` is a name or `#rrggbb`; `box` adds a dark box behind the text; `fade` fades it in and out over 0.3 s.

A template with a mistake (unknown look, missing `slots`, invalid JSON and so on) is left out, and `list_templates` reports it under `errors` with the file and the field, so one broken file doesn't take the others down.

## Web app

`video-mcp-web` serves a single page, designed for a phone, on your home network. You upload clips (and optionally one music file), tap a template, fill in its text fields and tap **Make video**. It renders a half-resolution preview you can watch in the page, then **Render full quality** gives you a 1080x1920 MP4 to download. **Try another template** keeps your uploads. **My videos** lists past renders with play, download and delete.

It's plain HTML, CSS and JavaScript served by the app itself: no Node, no build step and no CDN, so it works with the Pi offline.

> **Home network only.** Don't port-forward port 8780 (or expose the web app in any other way) to the internet. It has a single shared password, plain HTTP, and it runs FFmpeg on whatever is uploaded.

### Start it

Set a password in `~/video-mcp/.env`:

```bash
echo "WEB_PASSWORD=$(openssl rand -base64 18)" >> ~/video-mcp/.env
grep WEB_PASSWORD ~/video-mcp/.env      # note it down for your phone
```

Run it by hand:

```bash
set -a; . ~/video-mcp/.env; set +a
~/video-mcp/.venv/bin/video-mcp-web         # listens on 0.0.0.0:8780
```

or as a service, next to the MCP server:

```bash
cp ~/video-mcp/deploy/video-mcp-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now video-mcp-web
journalctl --user -u video-mcp-web -f
```

To change the password, edit `WEB_PASSWORD` and restart the service. Changing it logs everyone out.

### Open it from your phone

Find the Pi's address with `hostname -I` (for example `192.168.1.42`), then open `http://<pi-ip>:8780` on a phone on the same Wi-Fi and log in. If it doesn't load, check that a firewall on the Pi allows port 8780 from your LAN.

### How it stores things

- Each browser upload session gets `workspace/inbox/<session_id>/`. Files are saved under generated names (the name your phone sent is only shown in the page) and are streamed to disk, up to 500 MB each. Accepted: `.mp4`, `.mov`, `.m4v`, `.webm`, `.mp3`, `.m4a`, `.wav`. Every upload is checked with `ffprobe` and deleted if it isn't real media.
- The session's project is `projects/web-<session_id>.json`, a normal project you can also open over MCP.
- Renders go to `renders/` and stay until you delete them in **My videos**.
- Upload sessions with no activity for 7 days are deleted (with their uploads and project) when the web app starts.
- Login uses a signed, HTTP-only cookie that also carries a CSRF token, which every state-changing request must send. A failed login waits one second. Every id in a URL is checked against a strict pattern and every path is checked to stay inside the workspace.

## Development

```bash
.venv/bin/pytest -q
```

The tests generate all their media at run time with FFmpeg `lavfi` sources (`testsrc`, `testsrc2`, `sine`), so no binary files are checked in. They render for real and check the results with `ffprobe`: resolution, duration within 0.2 s, and audio levels. They also start the real server and drive it over MCP via both HTTP and stdio, and start the real web app and drive it over HTTP (login, upload, template, render, download with range requests).
