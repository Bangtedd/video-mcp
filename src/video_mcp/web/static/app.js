"use strict";

// Video maker front end. Plain DOM, no framework, no build step.

const state = {
  csrf: "",
  templates: [],
  maxUpload: 500 * 1024 * 1024,
  session: null,
  selected: null,       // template id
  texts: {},            // key -> value, kept across template switches
  pending: [],          // uploads in flight: {key, name, progress, error, kind}
  uploading: false,
  queue: [],
  busy: false,          // a render is running
};

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function fmtDuration(s) {
  if (s == null) return "";
  s = Math.round(s * 10) / 10;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s % 60)).padStart(2, "0")}`;
}

function fmtSize(b) {
  if (b > 1e9) return `${(b / 1e9).toFixed(1)} GB`;
  if (b > 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(b / 1e3))} kB`;
}

function showError(msg) {
  const b = $("banner");
  b.textContent = msg;
  b.hidden = !msg;
  if (msg) b.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function api(method, url, body) {
  const opts = { method, headers: { "X-CSRF-Token": state.csrf }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    location.href = "/login";
    throw new Error("Logged out");
  }
  let data = {};
  try { data = await resp.json(); } catch (_) { /* empty body */ }
  if (!resp.ok) {
    const err = new Error(data.error || `Request failed (${resp.status})`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

// ------------------------------------------------------------------ session

async function ensureSession() {
  let sid = null;
  try { sid = localStorage.getItem("vmw_sid"); } catch (_) { /* storage blocked */ }
  if (sid) {
    try {
      state.session = await api("GET", `/api/sessions/${encodeURIComponent(sid)}`);
    } catch (e) {
      if (e.status !== 404) throw e;
    }
  }
  if (!state.session) {
    state.session = await api("POST", "/api/sessions");
    try { localStorage.setItem("vmw_sid", state.session.id); } catch (_) { /* ignore */ }
  }
  if (state.session.template) state.selected = state.session.template;
  Object.assign(state.texts, state.session.texts || {});
}

function sid() { return state.session.id; }

// ------------------------------------------------------------------ uploads

function queueFiles(files, kind) {
  for (const file of files) {
    const item = { key: Math.random().toString(36).slice(2), name: file.name, progress: 0, error: "", kind, file };
    if (file.size > state.maxUpload) item.error = `Larger than ${Math.round(state.maxUpload / 1048576)} MB.`;
    state.pending.push(item);
    if (!item.error) state.queue.push(item);
  }
  renderUploads();
  pump();
}

function pump() {
  if (state.uploading || !state.queue.length) return;
  const item = state.queue.shift();
  state.uploading = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/sessions/${sid()}/uploads`);
  xhr.setRequestHeader("X-CSRF-Token", state.csrf);
  xhr.setRequestHeader("X-Filename", encodeURIComponent(item.name));
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      item.progress = (e.loaded / e.total) * 100;
      const bar = document.querySelector(`[data-key="${item.key}"] .bar > div`);
      if (bar) bar.style.width = `${item.progress}%`;
      const sub = document.querySelector(`[data-key="${item.key}"] .sub`);
      if (sub) sub.textContent = item.progress >= 100 ? "Checking…" : `Uploading ${Math.round(item.progress)}%`;
    }
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
    if (xhr.status === 201) {
      state.session = data;
      state.pending = state.pending.filter((p) => p !== item);
    } else if (xhr.status === 401) {
      location.href = "/login";
    } else {
      item.error = data.error || `Upload failed (${xhr.status})`;
    }
    done();
  };
  xhr.onerror = () => { item.error = "Network error during upload."; done(); };
  function done() {
    state.uploading = false;
    renderAll();
    pump();
  }
  xhr.send(item.file);
  renderUploads();
}

async function removeUpload(id) {
  try {
    state.session = await api("DELETE", `/api/sessions/${sid()}/uploads/${id}`);
    renderAll();
  } catch (e) { showError(e.message); }
}

async function move(id, delta) {
  const ids = state.session.clips.map((c) => c.id);
  const i = ids.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  try {
    state.session = await api("POST", `/api/sessions/${sid()}/order`, { order: ids });
    renderAll();
  } catch (e) { showError(e.message); }
}

function renderUploads() {
  const list = $("uploads");
  list.replaceChildren();
  const clips = state.session.clips;
  clips.forEach((c, i) => {
    list.append(el("li", { class: "upload" },
      el("img", { class: "thumb", src: c.thumb, alt: "", loading: "lazy" }),
      el("div", { class: "meta" },
        el("div", { class: "name", text: `${i + 1}. ${c.name}` }),
        el("div", { class: "sub", text: `${fmtDuration(c.duration)} · ${c.width}×${c.height}${c.has_audio ? "" : " · no sound"}` })),
      el("div", { class: "actions" },
        el("button", { type: "button", "aria-label": `Move ${c.name} up`, disabled: i === 0, onclick: () => move(c.id, -1), text: "↑" }),
        el("button", { type: "button", "aria-label": `Move ${c.name} down`, disabled: i === clips.length - 1, onclick: () => move(c.id, 1), text: "↓" }),
        el("button", { type: "button", class: "danger", "aria-label": `Remove ${c.name}`, onclick: () => removeUpload(c.id), text: "✕" }))));
  });
  for (const p of state.pending) {
    list.append(el("li", { class: "upload", "data-key": p.key },
      el("div", { class: p.kind === "music" ? "thumb music" : "thumb", text: p.kind === "music" ? "♪" : "" }),
      el("div", { class: "meta" },
        el("div", { class: "name", text: p.name }),
        p.error
          ? el("div", { class: "error-text", text: p.error })
          : el("div", { class: "sub", text: state.queue.includes(p) ? "Waiting…" : `Uploading ${Math.round(p.progress)}%` }),
        p.error ? null : (() => { const b = el("div", { class: "bar" }, el("div")); b.firstChild.style.width = `${p.progress}%`; return b; })()),
      p.error ? el("div", { class: "actions" },
        el("button", { type: "button", "aria-label": "Dismiss", onclick: () => { state.pending = state.pending.filter((x) => x !== p); renderUploads(); }, text: "✕" })) : null));
  }

  const m = state.session.music;
  const row = $("music-row");
  row.replaceChildren();
  if (m) {
    row.append(el("div", { class: "upload" },
      el("div", { class: "thumb music", text: "♪" }),
      el("div", { class: "meta" },
        el("div", { class: "name", text: m.name }),
        el("div", { class: "sub", text: `Music · ${fmtDuration(m.duration)}` })),
      el("div", { class: "actions" },
        el("button", { type: "button", class: "danger", "aria-label": "Remove music", onclick: () => removeUpload(m.id), text: "✕" }))));
  }
}

// ---------------------------------------------------------------- templates

function renderTemplates() {
  const box = $("templates");
  box.replaceChildren();
  const first = state.session.clips[0];
  $("template-hint").hidden = !!first;
  for (const t of state.templates) {
    const checked = state.selected === t.id;
    const img = first
      ? el("img", { class: "tpl-img", alt: "", loading: "lazy",
          src: `/api/sessions/${sid()}/templates/${t.id}/thumb?clip=${first.id}` })
      : el("div", { class: "tpl-img" });
    box.append(el("button", {
      type: "button", class: "tpl", role: "radio", "aria-checked": checked ? "true" : "false",
      onclick: () => { state.selected = t.id; renderAll(); },
    }, img, el("div", { class: "tpl-body" },
      el("div", { class: "tpl-name", text: t.name }),
      el("div", { class: "tpl-len", text: `about ${fmtDuration(t.nominal_duration)}` }),
      el("div", { class: "tpl-desc", text: t.description }))));
  }
}

function renderTexts() {
  const box = $("text-fields");
  box.replaceChildren();
  const t = state.templates.find((x) => x.id === state.selected);
  if (!t) { box.append(el("p", { class: "hint", text: "Pick a template first." })); return; }
  if (!t.texts.length) { box.append(el("p", { class: "hint", text: "This template has no text." })); return; }
  for (const f of t.texts) {
    const id = `text-${f.key}`;
    const input = el("input", { type: "text", id, maxlength: "200", placeholder: f.placeholder || "", autocomplete: "off" });
    input.value = state.texts[f.key] || "";
    input.addEventListener("input", () => { state.texts[f.key] = input.value; });
    box.append(el("div", { class: "field" }, el("label", { for: id, text: f.label }), input));
  }
  box.append(el("p", { class: "hint", text: "Leave a field empty to skip it." }));
}

function renderMake() {
  const ready = state.session.clips.length > 0 && state.selected && !state.busy && !state.uploading;
  $("make").disabled = !ready;
}

function renderAll() {
  renderUploads();
  renderTemplates();
  renderTexts();
  renderMake();
}

// ------------------------------------------------------------------ renders

function pollJob(job, bar, status, label) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      let j;
      try { j = await api("GET", `/api/jobs/${job.job_id}`); } catch (e) { reject(e); return; }
      bar.style.width = `${j.progress || 0}%`;
      if (j.status === "queued") status.textContent = "Waiting for another render to finish…";
      else if (j.status === "running") status.textContent = `${label} ${Math.round(j.progress)}%`;
      if (j.status === "done") resolve(j);
      else if (j.status === "failed") reject(new Error(j.error || "Render failed"));
      else setTimeout(tick, 800);
    };
    tick();
  });
}

async function makeVideo() {
  showError("");
  state.busy = true;
  renderMake();
  $("preview").hidden = true;
  $("download").hidden = true;
  $("download-hint").hidden = false;
  $("make-progress").hidden = false;
  $("make-bar").style.width = "0";
  $("make-status").textContent = "Picking the best moments…";
  const t = state.templates.find((x) => x.id === state.selected);
  const texts = {};
  for (const f of t.texts) texts[f.key] = (state.texts[f.key] || "").trim();
  try {
    const res = await api("POST", `/api/sessions/${sid()}/make`, { template: state.selected, texts });
    const job = await pollJob(res.job, $("make-bar"), $("make-status"), "Making preview");
    $("make-progress").hidden = true;
    const v = $("preview-video");
    v.poster = job.video.poster;
    v.src = job.video.url;
    $("preview").hidden = false;
    v.scrollIntoView({ behavior: "smooth", block: "center" });
    loadVideos();
  } catch (e) {
    $("make-progress").hidden = true;
    showError(e.message);
  } finally {
    state.busy = false;
    renderMake();
  }
}

async function renderFinal() {
  showError("");
  $("final").disabled = true;
  $("final-progress").hidden = false;
  $("final-bar").style.width = "0";
  $("final-status").textContent = "Starting…";
  $("download").hidden = true;
  $("step-download").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const res = await api("POST", `/api/sessions/${sid()}/final`);
    const job = await pollJob(res.job, $("final-bar"), $("final-status"), "Rendering full quality");
    $("final-progress").hidden = true;
    $("download-hint").hidden = true;
    const a = $("download");
    a.href = job.video.download_url;
    a.hidden = false;
    loadVideos();
  } catch (e) {
    $("final-progress").hidden = true;
    showError(e.message);
  } finally {
    $("final").disabled = false;
  }
}

// ------------------------------------------------------------------- videos

async function loadVideos() {
  let data;
  try { data = await api("GET", "/api/videos"); } catch (e) { return; }
  const list = $("video-list");
  list.replaceChildren();
  if (!data.videos.length) {
    list.append(el("li", { class: "hint", text: "Nothing yet. Your renders will appear here." }));
    return;
  }
  for (const v of data.videos) {
    const when = new Date(v.created * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    list.append(el("li", { class: "video-item" },
      el("img", { class: "thumb", src: v.poster, alt: "", loading: "lazy" }),
      el("div", { class: "meta" },
        el("div", { class: "name" }, v.template || "Video",
          el("span", { class: v.quality === "final" ? "badge final" : "badge", text: v.quality || "video" })),
        el("div", { class: "sub", text: `${when} · ${fmtDuration(v.duration)} · ${fmtSize(v.size)}` })),
      el("div", { class: "actions" },
        el("button", { type: "button", "aria-label": "Play", text: "▶", onclick: () => play(v) }),
        el("a", { class: "button icon", href: v.download_url, download: v.name, "aria-label": "Download", text: "⬇" }),
        el("button", { type: "button", class: "danger", "aria-label": "Delete", text: "🗑", onclick: () => removeVideo(v) }))));
  }
}

function play(v) {
  const p = $("player");
  p.poster = v.poster;
  p.src = v.url;
  p.hidden = false;
  p.scrollIntoView({ behavior: "smooth", block: "center" });
  p.play().catch(() => {});
}

async function removeVideo(v) {
  if (!confirm("Delete this video? This cannot be undone.")) return;
  try {
    await api("DELETE", `/api/videos/${encodeURIComponent(v.name)}`);
    const p = $("player");
    if (p.src.endsWith(v.url)) { p.pause(); p.removeAttribute("src"); p.hidden = true; }
    loadVideos();
  } catch (e) { showError(e.message); }
}

// --------------------------------------------------------------------- boot

async function startOver() {
  if (!confirm("Start over? Your uploaded clips will be removed (finished videos are kept).")) return;
  try { await api("DELETE", `/api/sessions/${sid()}`); } catch (_) { /* already gone */ }
  try { localStorage.removeItem("vmw_sid"); } catch (_) { /* ignore */ }
  state.session = null;
  state.selected = null;
  state.texts = {};
  $("preview").hidden = true;
  $("download").hidden = true;
  await ensureSession();
  renderAll();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function boot() {
  try {
    const st = await api("GET", "/api/state");
    state.csrf = st.csrf;
    state.templates = st.templates;
    state.maxUpload = st.max_upload_bytes;
    await ensureSession();
  } catch (e) {
    showError(e.message);
    return;
  }
  $("pick-clips").addEventListener("change", (e) => { queueFiles(e.target.files, "clip"); e.target.value = ""; });
  $("pick-music").addEventListener("change", (e) => { queueFiles(e.target.files, "music"); e.target.value = ""; });
  $("make").addEventListener("click", makeVideo);
  $("final").addEventListener("click", renderFinal);
  $("another").addEventListener("click", () => {
    $("preview").hidden = true;
    $("preview-video").pause();
    $("step-template").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("start-over").addEventListener("click", startOver);
  $("logout").addEventListener("click", async () => {
    try { await api("POST", "/logout"); } catch (_) { /* ignore */ }
    location.href = "/login";
  });
  renderAll();
  loadVideos();
}

document.addEventListener("DOMContentLoaded", boot);
