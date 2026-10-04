# video-mcp v2: templates and web UI

Builds on the existing video-mcp in this repo. Read `SPEC.md`, the README and the current code first. Keep all existing tools and tests working.

Goal: the user opens a web page on their phone, uploads clips, taps a template, types a caption, and downloads a finished video. No terminal, no Claude needed for this flow.

Same target as before: Raspberry Pi 5, Bookworm, Python 3.11, FFmpeg 5.1, software `libx264`. Do not use FFmpeg features newer than 5.1.

## Part A: engine additions

Add to the project timeline and to the single-pass render.

**Transitions.** Project field `transition`: `{type, duration}`. Types: `cut` (default), `fade`, `slide_left`, `slide_up`, `zoom`. Implement with `xfade` for video and `acrossfade` for audio. Duration 0.2 to 1.0 s. A transition overlaps two clips, so total duration shrinks; `get_project` must report the correct output duration. If a clip is shorter than twice the transition duration, fall back to `cut` for that join.

**Looks.** Project field `look`: `none`, `warm`, `cool`, `vivid`, `film` (faded, slight grain-free matte), `bw`, `moody` (darker, more contrast, slight vignette). Built from standard filters (`eq`, `colorbalance`, `curves`, `vignette`). Applied once after concat, before text.

**Gradient.** Project field `gradient`: `none`, `bottom`, `top`, `both`. A dark-to-transparent gradient covering 35% of the frame height, drawn under the text so captions stay readable. Generate the gradient as a PNG with alpha (Pillow) into `cache/` and overlay it.

**Intro and outro fade.** Project fields `fade_in`, `fade_out` in seconds (0 to 2), video and audio.

**Text fade.** Text overlays gain `fade` (bool): 0.3 s fade in and out using a `drawtext` alpha expression.

**Segment picking.** New function and tool `suggest_segments(file, length, count)`. Returns the best `count` non-overlapping windows of `length` seconds. Score windows by motion (analyze at low resolution and 2 fps, for example with scene-change scores), skip the first and last 0.5 s of the file, and prefer windows that are not near-static. Must be deterministic. If analysis fails or the file is short, fall back to evenly spaced windows. Cache results per file in `cache/`.

**New MCP tools:** `set_transition`, `set_look`, `set_gradient`, `set_fades`, `suggest_segments`, `list_templates`, `apply_template`.

## Part B: templates

A template is a JSON file in `templates/` at the repo root. Fields:

- `id`, `name`, `description`
- `preset`, `fit`, `look`, `gradient`, `transition`, `fade_in`, `fade_out`
- `slots`: list of clip lengths in seconds, for example `[2.5, 1.5, 1.5, 3]`
- `music`: `{volume, fade_in, fade_out}`, used only if the user uploaded music
- `clip_volume`: volume for original clip audio (0 mutes it)
- `texts`: list of named fields, each with `key`, `label`, `position`, `size`, `color`, `box`, `fade`, and timing as `from_start` or `from_end` plus `duration`

`apply_template(project, template_id, files, texts)`:

- Assign files to slots in the order given. Fewer files than slots: cycle through the files, using a different segment each time. More files than slots: repeat the slot pattern.
- Pick each segment with `suggest_segments`.
- A clip shorter than its slot uses the whole clip.
- Text fields the user left empty are skipped.
- The result is a normal project, so it can still be adjusted with the existing tools.

Ship these seven templates:

| id | Feel |
|---|---|
| `clean_cuts` | Hard cuts, 2 s slots, no look, one caption at the bottom |
| `warm_promo` | Warm look, fade transitions, bottom gradient, title at start and call to action at the end |
| `fast_hype` | 0.8 to 1.2 s slots, vivid look, hard cuts, big centered title in the first 2 s |
| `cinematic` | Film look, slow fades, 3 to 4 s slots, fade in and out, small bottom caption |
| `bw_mood` | Black and white, slide transitions, both gradients, top title |
| `product_showcase` | 2.5 s slots, moody look, bottom gradient, product name and price lines, call to action at the end |
| `story_vlog` | Longer 4 to 5 s slots, cool look, zoom transitions, clip audio kept loud, music low |

## Part C: web app

New entry point `video-mcp-web`. Starlette or FastAPI, plain HTML, CSS and vanilla JavaScript. No Node, no build step, no CDN (the Pi may be offline). Runs as its own process on `WEB_HOST` (default `0.0.0.0`) and `WEB_PORT` (default `8780`), sharing the same workspace.

Because the web app and the MCP server are separate processes, serialize renders across both with a lock file in the workspace so only one FFmpeg render runs at a time.

**Flow, one page, mobile first (designed for a 380 px wide screen):**

1. **Upload.** Pick several videos at once, plus one optional music file. Per-file progress bars. Show a thumbnail for each uploaded clip. Clips can be removed and reordered.
2. **Template.** Cards showing name, description, and length. Tapping one selects it. Each card shows a thumbnail of the user's first clip with that template's look and gradient applied, so the choice is visual.
3. **Text.** Inputs for the selected template's text fields.
4. **Make video.** Renders a preview with a progress bar, then plays it in the page. Buttons: "Render full quality" and "Try another template" (keeps the uploads).
5. **Download.** Download button for the final file.

Also a "My videos" list of past renders with play, download and delete.

**Backend rules:**

- Each upload session stores files under `inbox/<session_id>/` with generated names. Never use the client's filename on disk.
- Accept `.mp4`, `.mov`, `.m4v`, `.webm`, `.mp3`, `.m4a`, `.wav`. Verify every upload with `ffprobe` and delete it if it is not real media. Limit 500 MB per file, streamed to disk, not held in memory.
- Downloads support HTTP range requests so video plays and seeks in mobile browsers.
- Sessions with no activity for 7 days are deleted on startup, along with their uploads. Renders are kept until the user deletes them.

**Security:**

- Login is always required. Password from `WEB_PASSWORD`; the app refuses to start if it is not set. Signed, HTTP-only session cookie. One-second delay on a failed login.
- All state-changing requests require a CSRF token.
- Every path stays inside the workspace, using the existing containment checks.
- README must say: this is for the home network only, do not port-forward it to the internet.

## Tests

Keep generating all media with `lavfi`. Add:

- Transitions: output duration equals the sum of clips minus overlaps; short-clip fallback to cut
- Each look and each gradient renders and changes the picture compared to `none`
- `suggest_segments`: deterministic, in bounds, non-overlapping, fallback path
- `apply_template` for all seven templates with 1, 3 and 8 input clips, with and without music; each result renders and has the expected duration
- Web: pages require login; wrong password rejected; upload then apply template then render then download works end to end; a fake `.mp4` is rejected; path traversal in any id is rejected; CSRF enforced; range requests work
- Cross-process render lock

Run everything with real FFmpeg. Do not skip or mock.

## Deliverables

- Code, `templates/*.json`, tests
- `deploy/video-mcp-web.service` (systemd user unit)
- README section: start the web app, set the password, open it from a phone at `http://<pi-ip>:8780`, how to write a new template
- `.env.example` updated

## Done means

1. All old and new tests pass with real FFmpeg.
2. Through the web app's HTTP API: upload three mixed clips and a music file, apply `warm_promo` with two text fields, render final, download a playable 1080x1920 MP4 with transitions, look, gradient and text visible in extracted frames.
3. The page is usable at 380 px wide with no horizontal scrolling. Include screenshots of each step in the PR.
4. Open a PR with a summary and honest caveats.

## Out of scope

Claude choosing the cuts, beat-synced cuts, stickers, animated text beyond fades, a template editor in the UI, multiple users, speech captions, anything that needs an internet connection at runtime.
