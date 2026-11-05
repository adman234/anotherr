/* Anotherr web UI - vanilla JS, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sev = (s) => `<span class="sev s-${esc(s)}">${esc(s)}</span>`;
const ago = (t) => { if (!t) return "never"; const d = Date.now() / 1000 - t; if (d < 0) return "soon"; if (d < 60) return `${d | 0}s ago`; if (d < 3600) return `${(d / 60) | 0}m ago`; if (d < 86400) return `${(d / 3600) | 0}h ago`; return `${(d / 86400) | 0}d ago`; };
const fmtTs = (t) => { t = Math.floor(t); return `${Math.floor(t / 3600)}:${String(Math.floor(t % 3600 / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; };
const gb = (b) => b ? (b / 1e9).toFixed(2) + " GB" : "";
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

let KEY = localStorage.getItem("anotherr_key") || "";

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (KEY) headers["X-Api-Key"] = KEY;
  const r = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401) {
    const k = prompt("API key required");
    if (k) { KEY = k; localStorage.setItem("anotherr_key", k); return api(path, opts); }
    throw new Error("unauthorized");
  }
  if (!r.ok) { const t = await r.text(); throw new Error(t.slice(0, 300)); }
  return r.json();
}
const imgUrl = (fid, t, w = 480) => `/api/files/${fid}/frame?t=${t}&w=${w}` + (KEY ? `&apikey=${encodeURIComponent(KEY)}` : "");

function toast(msg) {
  const d = document.createElement("div"); d.textContent = msg; $("#toast").appendChild(d);
  setTimeout(() => d.remove(), 4500);
}
function openModal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-body").innerHTML = ""; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

async function act(fn, ok) { try { const r = await fn(); if (ok) toast(typeof ok === "function" ? ok(r) : ok); route(); return r; } catch (e) { toast("Error: " + e.message); } }

// ------------------------------------------------------------------ status bar
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("#n-issues").textContent = s.open_issues || "";
    $("#n-reviews").textContent = s.pending_reviews || "";
    $("#n-actions").textContent = s.proposed_actions || "";
    const cur = s.current && s.current.file_id ? `Scanning <b>${esc(s.current.title)} ${esc(s.current.label || "")}</b> · ${esc(s.current.step)}` : esc(s.scheduler.mode || "idle");
    const pause = s.scheduler.pause_reasons && s.scheduler.pause_reasons.length ? ` · <span style="color:var(--warning)">${esc(s.scheduler.pause_reasons.join(", "))}</span>` : "";
    $("#statusbar").innerHTML = `${cur}${pause}<br>queue ${s.queue} · whisper ${esc(s.whisper.device || (s.whisper.error ? "error" : "not loaded"))} · sync ${s.sync.running ? "running" : ago(s.sync.last)}`;
    window.STATUS = s;
  } catch (e) { $("#statusbar").textContent = "offline: " + e.message; }
}
setInterval(refreshStatus, 5000);

// ------------------------------------------------------------------ router
const routes = { "": dashboard, library, item: itemPage, file: filePage, issues, reviews, actions: actionsPage, queue, settings: settingsPage };
async function route() {
  // stop images still loading on the page we're leaving, so they don't hold up the next page
  document.querySelectorAll("#app img").forEach((i) => { i.removeAttribute("src"); });
  const [name, arg] = location.hash.replace(/^#\/?/, "").split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#/" + (name || "")));
  try { await (routes[name] || dashboard)(arg); } catch (e) { $("#app").innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", () => { closeModal(); route(); });

// ------------------------------------------------------------------ dashboard
async function dashboard() {
  const [s, c] = await Promise.all([api("/api/status"), api("/api/coverage")]);
  const files = s.files || {};
  const bad = (files.error || 0) + (files.critical || 0) + (files.failed || 0);
  const scanned = (files.total || 0) - (files.unscanned || 0);
  const unconfigured = Object.entries(s.configured).filter(([k, v]) => !v && (k === "sonarr" || k === "radarr")).map(([k]) => k);
  const covRow = (r) => {
    const t = r.total || 1;
    return `<tr><td>${esc(r.name)}${c.disk_state[r.name] ? (c.disk_state[r.name].spundown ? ' <span class="muted small">(spun down)</span>' : ' <span class="small" style="color:var(--ok)">(spinning)</span>') : ""}</td><td>${r.total}</td><td>${r.scanned || 0} (${Math.round(100 * (r.scanned || 0) / t)}%)</td>
      <td style="min-width:160px"><div class="bar"><span style="width:${100 * (r.ok || 0) / t}%;background:var(--ok)"></span><span style="width:${100 * (r.warn || 0) / t}%;background:var(--warning)"></span><span style="width:${100 * (r.bad || 0) / t}%;background:var(--error)"></span></div></td>
      <td>${r.bad || 0}</td><td><button class="sm" onclick="${r.name.startsWith("Anime") || r.name === "TV" || r.name === "Movies" ? `quickScan('library','${r.name.startsWith("Anime") ? "anime" : r.name === "TV" ? "tv" : "movies"}')` : `quickScan('disk','${esc(r.name === "(unknown)" ? "" : r.name)}')`}">Sample 10</button></td></tr>`;
  };
  $("#app").innerHTML = `
    ${s.gpu_decode.available === false && s.gpu_decode.mode !== "off" ? `<div class="card" style="border-color:var(--warning);margin-bottom:12px"><b>GPU video decoding isn't available, so scans decode on the CPU (much slower).</b> In Unraid, edit the container and set <code>NVIDIA_DRIVER_CAPABILITIES</code> to <code>compute,utility,video</code>. <span class="small muted">${esc(s.gpu_decode.reason)}</span></div>` : ""}
    ${s.files_not_found ? `<div class="card" style="border-color:var(--error);margin-bottom:12px"><b>${s.files_not_found} file(s) couldn't be found on disk.</b> Your path mappings probably don't match how Sonarr/Radarr see your folders. Go to <a href="#/settings">Settings → Path mappings</a> and click <b>Check paths</b> - it suggests the right mapping.</div>` : ""}
    ${unconfigured.length ? `<div class="card" style="border-color:var(--accent);margin-bottom:12px">Set up ${unconfigured.join(" and ")} in <a href="#/settings">Settings</a>, then run a sync.</div>` : ""}
    <div class="grid">
      <div class="card"><div class="muted">Files</div><div class="stat">${files.total || 0}</div><div class="small muted">${scanned} checked · ${files.unscanned || 0} never checked</div></div>
      <div class="card"><div class="muted">Problems</div><div class="stat" style="color:var(--error)">${bad}</div><div class="small muted">${files.warning || 0} warnings · ${s.open_issues} open issues</div></div>
      <div class="card"><div class="muted">Waiting on you</div><div class="stat">${s.pending_reviews + s.proposed_actions}</div><div class="small muted"><a href="#/reviews">${s.pending_reviews} visual reviews</a> · <a href="#/actions">${s.proposed_actions} proposed actions</a></div></div>
      <div class="card"><div class="muted">Scanner</div><div style="font-weight:600;margin:4px 0">${s.current.file_id ? `${esc(s.current.title)} ${esc(s.current.label || "")}` : esc(s.scheduler.mode)}</div>
        <div class="small muted">Next: ${esc(s.scheduler.next_run ? s.scheduler.next_run.why : "")}${s.scheduler.windows && !s.scheduler.windows.length ? " · no scan window set (scans anytime)" : ""}</div>
        ${(s.scheduler.streams || []).length ? `<div class="small muted">Plex: ${s.scheduler.streams.map((x) => `${esc(x.title)}${x.disk ? ` (${esc(x.disk)})` : ""}`).join(", ")}${s.scheduler.stream_pause === "same_disk" ? " - skipping only those disks" : ""}</div>` : ""}
        ${(s.scheduler.slots || []).map((x) => `<div class="small"><span class="pill">${esc(x.slot)}</span> ${esc(x.disk || "?")} · ${esc(x.title || "")} ${esc(x.label || "")} <span class="muted">· ${esc(x.why || "")} · ${esc(x.step || "")}</span></div>`).join("")}
        <div class="toolbar" style="margin:8px 0 0">
          <button onclick="act(()=>api('/api/control/pause',{method:'POST',body:{paused:${!s.paused}}}),'${s.paused ? "Resumed" : "Paused"}')">${s.paused ? "Resume" : "Pause"}</button>
          ${s.current.file_id ? `<button class="danger" onclick="act(()=>api('/api/scan/stop',{method:'POST'}),'Stopping')">Stop scan</button>` : ""}
          ${s.queue && !s.forcing_queue ? `<button class="primary" onclick="act(()=>api('/api/queue/process',{method:'POST'}),r=>'Processing '+r.queued+' file(s)')">Process queue (${s.queue})</button>` : ""}
          <button onclick="act(()=>api('/api/sync',{method:'POST'}),'Sync started')">Sync now</button>
        </div></div>
      <div class="card"><div class="muted">Automation</div><div style="font-weight:600;margin:4px 0">${s.automation.enabled ? (s.automation.active_now ? '<span style="color:var(--ok)">Active now</span>' : "Enabled (outside its schedule)") : "Disabled"}</div>
        <div class="small muted">${s.automation.today}/${s.automation.max_per_day} auto actions today · <a href="#/settings">configure</a></div></div>
      <div class="card"><div class="muted">Whisper</div><div style="font-weight:600;margin:4px 0">${esc(s.whisper.model)} ${s.whisper.device ? "on " + esc(s.whisper.device) : "(loads on demand)"}</div>
        <div class="small" style="color:var(--error)">${esc(s.whisper.error || "")}</div><div class="small muted">Video decode: ${s.gpu_decode.mode === "off" ? "CPU (GPU off in settings)" : s.gpu_decode.available === false ? '<span style="color:var(--warning)">CPU (GPU unavailable)</span>' : s.gpu_decode.available ? '<span style="color:var(--ok)">GPU (NVDEC)</span>' : "GPU (not used yet)"}</div><div class="small muted">${s.unraid.detected ? "Unraid detected" : "Unraid not detected"}${s.unraid.parity_running ? " · parity check running" : ""}</div></div>
    </div>
    <h3>Coverage by library</h3>
    <table><tr><th>Library</th><th>Files</th><th>Checked</th><th>Health</th><th>Bad</th><th></th></tr>${c.libraries.map(covRow).join("") || '<tr><td colspan=6 class="muted">No data yet - sync first.</td></tr>'}</table>
    <h3>Coverage by disk</h3>
    <table><tr><th>Disk</th><th>Files</th><th>Checked</th><th>Health</th><th>Bad</th><th></th></tr>${c.disks.map(covRow).join("")}</table>
    <div class="grid" style="margin-top:12px">
      <div><h3>Next up</h3><table>${c.next_up.map((r) => `<tr><td><a href="#/file/${r.id}">${esc(r.title)} ${esc(r.label || "")}</a></td><td class="muted">${esc(r.disk)}</td><td class="muted">${r.score}</td></tr>`).join("") || '<tr><td class="muted">Nothing due</td></tr>'}</table></div>
      <div><h3>Activity</h3><div id="activity" class="card small" style="max-height:340px;overflow:auto"></div></div>
    </div>`;
  const a = await api("/api/activity?limit=60");
  $("#activity").innerHTML = a.map((r) => `<div><span class="muted">${new Date(r.ts * 1000).toLocaleString()}</span> <span style="color:var(--${r.level === "info" ? "text" : r.level})">${esc(r.message)}</span></div>`).join("");
}

function profileSelect(id = "profile") {
  return `<select id="${id}"><option value="quick">quick</option><option value="standard" selected>standard</option><option value="deep">deep</option></select>`;
}
async function quickScan(scope, val) {
  const body = { scope, count: 10, profile: "standard" };
  if (scope === "library") body.library = val; else body.disk = val;
  await act(() => api("/api/scan", { method: "POST", body }), (r) => `Queued ${r.queued} file(s)`);
}

// ------------------------------------------------------------------ library
let LIB = { search: "", source: "", status: "", page: 1 };
async function library() {
  const p = new URLSearchParams({ search: LIB.search, source: LIB.source, status: LIB.status, page: LIB.page, per_page: 50 });
  const res = await api("/api/items?" + p);
  $("#app").innerHTML = `<h2>Library</h2>
    <div class="toolbar">
      <input id="q" placeholder="Search titles" value="${esc(LIB.search)}">
      <select id="src"><option value="">All</option><option value="sonarr">TV (Sonarr)</option><option value="radarr">Movies (Radarr)</option></select>
      <select id="st"><option value="">Any status</option><option value="problems">Has problems</option><option value="unscanned">Not fully checked</option></select>
      <span class="muted">${res.total} items</span>
      <span style="margin-left:auto"></span>${profileSelect()}
      <button onclick="scanSelected()">Scan selected</button>
      <button onclick="scanRandom()">Random sample…</button>
    </div>
    <table><tr><th><input type="checkbox" onchange="document.querySelectorAll('.sel').forEach(c=>c.checked=this.checked)"></th><th>Title</th><th>Type</th><th>Language</th><th>Files</th><th>Checked</th><th>Problems</th></tr>
    ${res.rows.map((r) => `<tr><td><input type="checkbox" class="sel" value="${r.id}"></td><td><a href="#/item/${r.id}">${esc(r.title)}</a> <span class="muted">${r.year || ""}</span></td>
      <td>${r.source === "sonarr" ? "TV" : "Movie"}${r.is_anime ? ' <span class="pill">anime</span>' : ""}${r.excluded ? ' <span class="pill">excluded</span>' : ""}</td>
      <td>${esc(r.original_language || "?")}${r.language_override ? ` → <b>${esc(r.language_override)}</b>` : ""}</td>
      <td>${r.files}</td><td>${r.scanned || 0}</td><td>${r.bad ? sev("error") + " " + r.bad : ""} ${r.warn ? sev("warning") + " " + r.warn : ""}</td></tr>`).join("")}</table>
    ${pager(res, "LIB")}`;
  $("#src").value = LIB.source; $("#st").value = LIB.status;
  $("#q").onchange = (e) => { LIB.search = e.target.value; LIB.page = 1; library(); };
  $("#src").onchange = (e) => { LIB.source = e.target.value; LIB.page = 1; library(); };
  $("#st").onchange = (e) => { LIB.status = e.target.value; LIB.page = 1; library(); };
}
function pager(res, state) {
  const pages = Math.max(1, Math.ceil(res.total / res.per_page));
  return `<div class="pager"><button class="sm" ${res.page <= 1 ? "disabled" : ""} onclick="${state}.page--;route()">‹ Prev</button><span class="muted">Page ${res.page} / ${pages}</span><button class="sm" ${res.page >= pages ? "disabled" : ""} onclick="${state}.page++;route()">Next ›</button></div>`;
}
async function scanSelected() {
  const ids = [...document.querySelectorAll(".sel:checked")].map((c) => +c.value);
  if (!ids.length) return toast("Select some items first");
  await act(() => api("/api/scan", { method: "POST", body: { scope: "item", ids, profile: $("#profile").value } }), (r) => `Queued ${r.queued} file(s)`);
}
async function scanRandom() {
  const disks = (await api("/api/settings")).disks;
  openModal(`<h2>Random sample scan</h2><div class="field"><label>How many files</label><input id="rn" type="number" value="10"></div>
    <div class="field"><label>Library</label><select id="rl"><option value="">Any</option><option value="tv">TV</option><option value="movies">Movies</option><option value="anime">Anime</option></select></div>
    <div class="field"><label>Disk</label><select id="rd"><option value="">Any</option>${disks.map((d) => `<option>${esc(d)}</option>`).join("")}</select></div>
    <div class="field"><label>Profile</label>${profileSelect("rp")}</div><button class="primary" onclick="doRandom()">Queue</button>`);
}
async function doRandom() {
  const lib = $("#rl").value, body = { scope: lib ? "library" : "random", library: lib, disk: $("#rd").value, count: +$("#rn").value, profile: $("#rp").value };
  closeModal(); await act(() => api("/api/scan", { method: "POST", body }), (r) => `Queued ${r.queued} file(s)`);
}

async function itemPage(id) {
  const it = await api("/api/items/" + id);
  $("#app").innerHTML = `<h2>${esc(it.title)} <span class="muted">${it.year || ""}</span></h2>
    <div class="card" style="margin-bottom:12px"><div class="kv">
      <div>Source</div><div>${esc(it.source)} #${it.arr_id} ${it.is_anime ? '<span class="pill">anime</span>' : ""}</div>
      <div>Original language</div><div>${esc(it.original_language_name || "?")} (${esc(it.original_language || "?")})</div>
      <div>Preferred audio override</div><div><input id="ovr" placeholder="e.g. eng (blank = original)" value="${esc(it.language_override)}" style="width:220px"> <button class="sm" onclick="saveItem(${it.id})">Save</button>
        <span class="small muted">Original language is always preferred unless you set this.</span></div>
      <div>Anime rules</div><div><input type="checkbox" id="anime" ${it.is_anime ? "checked" : ""} onchange="saveItem(${it.id})"></div>
      <div>Multi-segment show</div><div><input type="checkbox" id="mseg" ${it.multi_segment ? "checked" : ""} onchange="saveItem(${it.id})"> <span class="small muted">e.g. SpongeBob - files usually hold 2 segments</span></div>
      <div>Exclude from scheduled scans</div><div><input type="checkbox" id="excl" ${it.excluded ? "checked" : ""} onchange="saveItem(${it.id})"></div>
      <div>Path</div><div class="small">${esc(it.path)}</div></div>
      <div class="toolbar" style="margin:10px 0 0">${profileSelect()}<button onclick="act(()=>api('/api/scan',{method:'POST',body:{scope:'item',ids:[${it.id}],profile:$('#profile').value}}),r=>'Queued '+r.queued)">Scan all files</button></div></div>
    <table><tr><th>File</th><th>Status</th><th>Disk</th><th>Quality</th><th>Group</th><th>Last check</th><th></th></tr>
    ${it.files.map((f) => `<tr><td><a href="#/file/${f.id}">${esc(f.label || f.arr_path.split("/").pop())}</a>${f.episodes.length ? `<div class="small muted">${f.episodes.map((e) => esc(e.title)).join(" / ")}</div>` : ""}</td>
      <td>${sev(f.status)} ${f.open_issues ? f.open_issues : ""}</td><td>${esc(f.disk)}</td><td>${esc(f.quality)}</td><td>${esc(f.release_group)}</td><td class="muted">${ago(f.last_scan_at)} ${esc(f.last_profile || "")}</td>
      <td><button class="sm" onclick="act(()=>api('/api/scan',{method:'POST',body:{scope:'file',ids:[${f.id}],profile:$('#profile').value}}),'Queued')">Scan</button></td></tr>`).join("")}</table>`;
}
async function saveItem(id) {
  await act(() => api("/api/items/" + id, { method: "PATCH", body: { language_override: $("#ovr").value.trim(), is_anime: $("#anime").checked ? 1 : 0, multi_segment: $("#mseg").checked ? 1 : 0, excluded: $("#excl").checked ? 1 : 0 } }), "Saved");
}

async function filePage(id) {
  const f = await api("/api/files/" + id);
  const r = (f.last_scan && f.last_scan.result) || {};
  const st = r.streams || {};
  const audio = (r.audio_tracks || []).map((t) => `<tr><td>#${t.a_index + 1}${t.default ? " (default)" : ""}</td><td>${esc(t.tag || "und")}</td><td>${t.detected ? `${esc(t.detected)} ${Math.round(t.prob * 100)}%` : '<span class="muted">-</span>'}</td><td>${t.speech_s}s</td><td>${t.level_db} dB</td><td>${esc(t.codec)} ${t.channels || ""}ch ${esc(t.title || "")}</td></tr>`).join("");
  const subs = (r.subtitle_tracks || []).map((t) => `<tr><td>${esc(t.title || t.codec)}${t.external ? ' <span class="pill">external</span>' : ""}${t.default ? ' <span class="pill">default</span>' : ""}${t.forced ? ' <span class="pill">forced</span>' : ""}</td><td>${esc(t.lang)}</td><td>${esc(t.kind)}</td><td>${t.cues} (${t.per_min}/min)</td></tr>`).join("");
  const sync = (r.subtitle_sync || []).map((v) => `<tr><td>${esc(v.track)}</td><td>${esc(v.verdict)}</td><td>${v.offset ?? ""}s</td><td>${v.score ?? ""} (at 0: ${v.score_at_zero ?? ""})</td><td>${v.windows}</td></tr>`).join("");
  $("#app").innerHTML = `<h2><a href="#/item/${f.item.id}">${esc(f.item.title)}</a> ${esc(f.label || "")} ${sev(f.status)}</h2>
    <div class="toolbar">${profileSelect()}<button onclick="act(()=>api('/api/scan',{method:'POST',body:{scope:'file',ids:[${f.id}],profile:$('#profile').value}}),'Queued')">Scan now</button>
      <button onclick="act(()=>api('/api/actions',{method:'POST',body:{file_id:${f.id},kind:'resub',reason:'manual'}}),'Proposed')">Propose resub</button>
      <button onclick="act(()=>api('/api/actions',{method:'POST',body:{file_id:${f.id},kind:'redownload',reason:'manual'}}),'Proposed')">Propose redownload</button>
      <button onclick="framePicker(${f.id}, ${st.duration || 0})">View frames</button></div>
    <div class="grid">
      <div class="card"><div class="kv"><div>Path</div><div class="small">${esc(f.arr_path)}</div><div>Read from</div><div class="small">${esc(r.path || f.local_path)}</div>
        <div>Disk</div><div>${esc(f.disk)}</div><div>Size</div><div>${gb(f.size)}</div><div>Quality</div><div>${esc(f.quality || "?")}${f.release_group ? ` [${esc(f.release_group)}]` : ""}</div>
        <div>Duration</div><div>${st.duration ? fmtTs(st.duration) : "?"} ${r.expected_runtime_s ? `(expected ~${Math.round(r.expected_runtime_s / 60)} min, ratio ${r.runtime_ratio})` : ""}</div>
        <div>Episodes</div><div>${f.episodes.map((e) => `S${String(e.season).padStart(2, "0")}E${String(e.episode).padStart(2, "0")} ${esc(e.title)}`).join("<br>") || "-"}</div>
        <div>Expected audio</div><div>${esc(r.expected_language || "?")}${r.dual_audio ? " · dual audio ✓" : ""}</div>
        <div>Last scan</div><div>${f.last_scan ? `${ago(f.last_scan.finished_at)} · ${esc(f.last_scan.profile)} · seed ${f.last_scan.seed} · ${r.elapsed_s || "?"}s` : "never"}</div></div></div>
      <div class="card"><b>Issues</b>${f.issues.map((i) => `<div style="margin:6px 0">${sev(i.severity)} <b>${esc(i.code)}</b> ${i.status === "ignored" ? '<span class="pill">ignored</span>' : ""}<br>${esc(i.message)}
        <div><button class="sm" onclick="showEvidence(${i.id})">Evidence</button> ${i.status === "open" ? `<button class="sm" onclick="act(()=>api('/api/issues/${i.id}',{method:'PATCH',body:{status:'ignored'}}),'Ignored')">Ignore</button>` : `<button class="sm" onclick="act(()=>api('/api/issues/${i.id}',{method:'PATCH',body:{status:'open'}}),'Reopened')">Un-ignore</button>`}</div></div>`).join("") || '<div class="muted">None</div>'}</div>
    </div>
    <h3>Audio tracks</h3><table><tr><th>Track</th><th>Tag</th><th>Detected</th><th>Speech</th><th>Peak level</th><th>Info</th></tr>${audio || '<tr><td colspan=6 class="muted">Not analysed</td></tr>'}</table>
    <h3>Subtitles</h3><table><tr><th>Track</th><th>Lang</th><th>Kind</th><th>Dialogue cues</th></tr>${subs || `<tr><td colspan=4 class="muted">${(st.subs || []).length} track(s); content not analysed in this profile</td></tr>`}</table>
    ${sync ? `<h3>Subtitle sync</h3><table><tr><th>Track</th><th>Verdict</th><th>Offset</th><th>Match score</th><th>Windows</th></tr>${sync}</table>` : ""}
    ${r.timings ? `<h3>Where the time went</h3><div class="card small">${Object.entries(r.timings).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span class="pill">${esc(k)}: ${v}s</span>`).join(" ")} <span class="muted">· total ${r.elapsed_s}s · video decode on ${r.gpu_decode ? "GPU" : "CPU"}</span></div>` : ""}
    ${r.transcript_sample ? `<h3>Transcript sample</h3><div class="card small">${esc(r.transcript_sample)}</div>` : ""}
    <h3>Reviews & actions</h3><table>${f.reviews.map((v) => `<tr><td>review</td><td>${esc(v.kind)}</td><td>${esc(v.status)} ${esc(v.verdict)}</td><td>${esc(v.notes)}</td></tr>`).join("")}${f.actions.map((a) => `<tr><td>action</td><td>${esc(a.kind)}</td><td><span class="st s-${a.status}">${a.status}</span></td><td>${esc(a.result || a.reason)}</td></tr>`).join("") || ""}</table>
    <h3>Raw result</h3><details><summary class="muted">show JSON</summary><pre>${esc(JSON.stringify(r, null, 1))}</pre></details>`;
}
async function showEvidence(id) {
  const e = await api("/api/issues/" + id);
  openModal(`<h2>${sev(e.severity)} ${esc(e.code)}</h2><p>${esc(e.message)}</p>
    ${e.evidence_ts && e.evidence_ts.length ? `<div class="frames">${e.evidence_ts.map((t) => `<figure><img loading="lazy" src="${imgUrl(e.file_id, t)}"><figcaption>${fmtTs(t)}</figcaption></figure>`).join("")}</div>` : ""}
    <h3>Details</h3><pre>${esc(JSON.stringify(e.details, null, 1))}</pre>
    <h3>Measurements from the scan</h3><pre>${esc(JSON.stringify(e.evidence, null, 1))}</pre>
    ${e.suppressed_by_this_scan && e.suppressed_by_this_scan.length ? `<h3>Skipped because of a root cause</h3><pre>${esc(JSON.stringify(e.suppressed_by_this_scan, null, 1))}</pre>` : ""}`);
}
function framePicker(fid, dur) {
  const ts = [0.05, 0.2, 0.4, 0.6, 0.8, 0.95].map((p) => Math.round(dur * p));
  openModal(`<h2>Frames</h2><div class="toolbar"><input id="fts" value="${ts.join(", ")}" style="width:60%"><button onclick="showFrames(${fid})">Show</button></div><div id="fr" class="frames"></div>`);
  showFrames(fid);
}
function showFrames(fid) {
  const ts = $("#fts").value.split(",").map((x) => parseFloat(x)).filter((x) => !isNaN(x));
  $("#fr").innerHTML = ts.map((t) => `<figure><img loading="lazy" src="${imgUrl(fid, t)}"><figcaption>${fmtTs(t)}</figcaption></figure>`).join("");
}

// ------------------------------------------------------------------ issues
let ISS = { status: "open", severity: "", code: "", search: "", page: 1 };
async function issues() {
  const p = new URLSearchParams({ ...ISS, per_page: 100 });
  const res = await api("/api/issues?" + p);
  $("#app").innerHTML = `<h2>Issues</h2>
    <div class="toolbar"><select id="is"><option value="open">Open</option><option value="ignored">Ignored</option><option value="resolved">Resolved</option></select>
      <select id="isev"><option value="">Any severity</option><option value="critical,error">Error + critical</option><option value="warning">Warning</option><option value="info">Info</option></select>
      <select id="icode"><option value="">Any type</option>${res.codes.map((c) => `<option value="${esc(c.code)}">${esc(c.code)} (${c.n})</option>`).join("")}</select>
      <input id="iq" placeholder="Search" value="${esc(ISS.search)}"><span class="muted">${res.total}</span>
      <button style="margin-left:auto" onclick="act(()=>api('/api/scan',{method:'POST',body:{scope:'problems',profile:'standard'}}),r=>'Queued '+r.queued)">Rescan all problem files</button></div>
    <table><tr><th>Severity</th><th>File</th><th>Issue</th><th>Disk</th><th></th></tr>
    ${res.rows.map((i) => `<tr><td>${sev(i.severity)}</td><td><a href="#/file/${i.file_id}">${esc(i.title)} ${esc(i.label || "")}</a></td><td><b>${esc(i.code)}</b><br><span class="small">${esc(i.message)}</span></td><td class="muted">${esc(i.disk)}</td>
      <td>${i.status === "open" ? `<button class="sm" onclick="act(()=>api('/api/issues/${i.id}',{method:'PATCH',body:{status:'ignored'}}),'Ignored')">Ignore</button>` : ""}</td></tr>`).join("")}</table>${pager(res, "ISS")}`;
  $("#is").value = ISS.status; $("#isev").value = ISS.severity; $("#icode").value = ISS.code;
  for (const [id, k] of [["is", "status"], ["isev", "severity"], ["icode", "code"], ["iq", "search"]])
    $("#" + id).onchange = (e) => { ISS[k] = e.target.value; ISS.page = 1; issues(); };
}

// ------------------------------------------------------------------ reviews
let REV = { page: 1 };
const REV_PER = 10;
async function reviews() {
  const rows = await api(`/api/reviews?status=pending&limit=${REV_PER + 1}&offset=${(REV.page - 1) * REV_PER}`);
  const more = rows.length > REV_PER;
  rows.length = Math.min(rows.length, REV_PER);
  const total = (window.STATUS && window.STATUS.pending_reviews) || rows.length;
  const nav = `<div class="pager"><button class="sm" ${REV.page <= 1 ? "disabled" : ""} onclick="REV.page--;route()">‹ Prev</button><span class="muted">Page ${REV.page} of ${Math.max(1, Math.ceil(total / REV_PER))} · ${total} pending</span><button class="sm" ${more ? "" : "disabled"} onclick="REV.page++;route()">Next ›</button></div>`;
  $("#app").innerHTML = `<h2>Visual reviews</h2>
    <div class="card small muted" style="margin-bottom:12px">These need eyes. Review them here, or in Claude Desktop with the Anotherr MCP server ("review pending items"). <a href="/mcp/anotherr_mcp.py">Download MCP server</a></div>${nav}
    ${rows.map((r) => `<div class="card" style="margin-bottom:12px"><div><b>${esc(r.title)} ${esc(r.label || "")}</b> <span class="pill">${esc(r.kind)}</span> <a class="small" href="#/file/${r.file_id}">file</a></div>
      <p>${esc(r.question)}</p><div class="frames">${r.timestamps.map((t) => `<figure><a href="${imgUrl(r.file_id, t, 1280)}" target="_blank"><img loading="lazy" src="${imgUrl(r.file_id, t)}"></a><figcaption>${fmtTs(t)}</figcaption></figure>`).join("")}</div>
      <div class="toolbar" style="margin-top:8px"><input id="rn${r.id}" placeholder="Notes" style="flex:1">
        <button onclick="verdict(${r.id},'ok')">Looks right</button><button class="danger" onclick="verdict(${r.id},'problem')">Problem</button>
        <button class="danger" onclick="verdict(${r.id},'problem',true)">Problem + propose redownload</button><button onclick="verdict(${r.id},'dismiss')">Dismiss</button></div></div>`).join("") || '<div class="muted">Nothing to review.</div>'}${rows.length ? nav : ""}`;
}
async function verdict(id, v, redl = false) {
  await act(() => api(`/api/reviews/${id}/verdict`, { method: "POST", body: { verdict: v, notes: $("#rn" + id).value, request_redownload: redl } }), "Saved");
}

// ------------------------------------------------------------------ actions
let ACT = { status: "proposed" };
async function actionsPage() {
  const rows = await api("/api/actions?status=" + ACT.status);
  const s = window.STATUS || (await api("/api/status"));
  $("#app").innerHTML = `<h2>Actions</h2>
    <div class="card small" style="margin-bottom:12px">Automation is <b>${s.automation.enabled ? "enabled" : "disabled"}</b>${s.automation.enabled ? (s.automation.active_now ? " and active now" : " (outside its schedule)") : ""}. Proposals wait here for approval unless automation picks them up. <a href="#/settings">Configure automation</a></div>
    <div class="toolbar"><select id="as"><option value="proposed">Proposed</option><option value="done,failed">Finished</option><option value="rejected">Rejected</option><option value="withdrawn">Withdrawn</option><option value="">All</option></select>
    ${ACT.status === "proposed" && rows.length ? `<button onclick="bulk('approve','resub')">Approve all resubs</button><button onclick="if(confirm('Reject all proposals?'))bulk('reject')">Reject all</button>` : ""}</div>
    <table><tr><th>Status</th><th>Kind</th><th>File</th><th>Reason</th><th>Result</th><th></th></tr>
    ${rows.map((a) => `<tr><td><span class="st s-${a.status}">${a.status}</span>${a.auto ? ' <span class="pill">auto</span>' : ""}</td><td>${a.kind === "resub" ? "Bazarr resub" : "Blocklist + redownload"}</td>
      <td><a href="#/file/${a.file_id}">${esc(a.title)} ${esc(a.label || "")}</a></td><td class="small">${esc(a.reason)}</td><td class="small">${esc(a.result)}</td>
      <td>${a.status === "proposed" || a.status === "failed" ? `<button class="sm primary" onclick="if(${a.kind === "redownload"} && !confirm('Blocklist and redownload this file?'))return; act(()=>api('/api/actions/${a.id}/approve',{method:'POST'}),r=>r.ok?'Done: '+r.result:'Failed: '+(r.result||r.error))">Approve</button>` : ""}
        ${a.status === "proposed" ? `<button class="sm" onclick="act(()=>api('/api/actions/${a.id}/reject',{method:'POST'}),'Rejected')">Reject</button>` : ""}</td></tr>`).join("") || '<tr><td colspan=6 class="muted">None</td></tr>'}</table>`;
  $("#as").value = ACT.status; $("#as").onchange = (e) => { ACT.status = e.target.value; actionsPage(); };
  window._actRows = rows;
}
async function bulk(op, kind) {
  for (const a of window._actRows.filter((a) => a.status === "proposed" && (!kind || a.kind === kind))) {
    try { await api(`/api/actions/${a.id}/${op}`, { method: "POST" }); } catch (e) { toast(e.message); }
  }
  route();
}

// ------------------------------------------------------------------ queue
async function queue() {
  const [rows, s] = await Promise.all([api("/api/queue"), api("/api/status")]);
  const reasons = s.scheduler.pause_reasons || [];
  const waiting = rows.length && !s.current.file_id && !s.forcing_queue && reasons.length;
  $("#app").innerHTML = `<h2>Queue <span class="muted">(${rows.length})</span></h2>
    ${waiting ? `<div class="card" style="border-color:var(--warning);margin-bottom:12px">Queue is waiting: <b>${esc(reasons.join(", "))}</b>. Use <b>Process queue now</b> to run it anyway.</div>` : ""}
    ${s.forcing_queue ? `<div class="card" style="border-color:var(--accent);margin-bottom:12px">Processing the queue now, ignoring pauses. ${s.current.file_id ? `Scanning <b>${esc(s.current.title)} ${esc(s.current.label || "")}</b> · ${esc(s.current.step)}` : ""}</div>` : ""}
    <div class="toolbar">
      ${s.forcing_queue ? `<button onclick="act(()=>api('/api/queue/process',{method:'POST',body:{stop:true}}),'Stopped forcing')">Stop forcing</button>`
        : `<button class="primary" ${rows.length ? "" : "disabled"} onclick="act(()=>api('/api/queue/process',{method:'POST'}),r=>'Processing '+r.queued+' file(s)')">Process queue now</button>`}
      <button class="danger" onclick="act(()=>api('/api/queue',{method:'DELETE'}),'Cleared')">Clear queue</button>
      <span class="muted small">Queued scans run automatically at any time (scan windows don't apply). They wait during parity checks, Plex streams or a manual pause - "Process queue now" ignores those until the queue is empty.</span></div>
    <table><tr><th>File</th><th>Profile</th><th>Source</th><th>Queued</th><th></th></tr>${rows.map((q) => `<tr><td><a href="#/file/${q.file_id}">${esc(q.title)} ${esc(q.label || "")}</a></td><td>${esc(q.profile)}</td><td>${esc(q.source)}</td><td class="muted">${ago(q.created_at)}</td>
    <td><button class="sm" onclick="act(()=>api('/api/queue/${q.id}',{method:'DELETE'}),'Removed')">Remove</button></td></tr>`).join("") || '<tr><td colspan=5 class="muted">Empty</td></tr>'}</table>`;
}

// ------------------------------------------------------------------ settings
const LABELS = {
  sonarr: "Sonarr", radarr: "Radarr", bazarr: "Bazarr", tautulli: "Tautulli (Plex activity)", path_mappings: "Path mappings",
  languages: "Languages", anime: "Anime", scan: "Scanning", review: "Visual reviews (Claude / manual)", schedule: "Scan schedule",
  imports: "New imports (webhook)", sync: "Library sync", whisper: "Whisper (speech recognition)", automation: "Automation",
};
const HELP = {
  "path_mappings": "How Sonarr/Radarr paths map into this container. Example: Sonarr sees /tv → here it's /mnt/user/media/tv.",
  "languages.subtitle_language": "The language you read subtitles in (ISO code, e.g. eng).",
  "languages.require_subs_for_native_audio": "Also require subtitles when the audio is already in your language.",
  "languages.min_full_sub_cues_per_min": "Tracks with fewer dialogue lines per minute are treated as signs/songs only.",
  "anime.prefer_dual_audio": "Flag anime without a dub track (info only - original audio is always preferred).",
  "anime.detect_dubtitles": "Flag subtitle tracks that just transcribe the English dub.",
  "scan.hw_decode": "auto = decode video on the NVIDIA GPU (NVDEC), falling back to CPU. Needs NVIDIA_DRIVER_CAPABILITIES to include 'video'.",
  "scan.ffmpeg_threads": "CPU threads for CPU decoding (0 = automatic).",
  "scan.max_audio_tracks": "Max audio tracks analysed per file (commentary tracks are skipped).",
  "review.title_cards": "When to queue title-card checks: never | multi_episode | flagged | always.",
  "review.visual_quality_on_deep": "Deep scans queue a picture-quality check (hardcoded subs, watermarks, cam rips).",
  "schedule.max_disks": "Hard drives scanned at the same time (each slot works on a different disk).",
  "schedule.cache_slot": "One extra slot for cache/SSD pools - no spin-up and no playback contention, so new downloads get checked right away.",
  "schedule.spinup": "Waking idle disks for scheduled scans: anytime · windows (only inside the spin-up windows below) · never. Queued scans you asked for may always wake a disk.",
  "schedule.quiet_windows": "No new scans start during these (e.g. while scheduled updates or backups run). Running scans finish first.",
  "schedule.pause_on_mover": "Don't start scans while Unraid's mover is running.",
  "schedule.disks": "Only scan these disks with scheduled scans (none ticked = all disks).",
  "schedule.rescan_after_days": "Files are re-checked after this many days.",
  "schedule.max_streams": "Only for stream pause 'any': pause when Plex has more than this many active streams.",
  "schedule.stream_pause": "same_disk = while Plex streams, skip files on the disk it is reading (others keep scanning) · any = pause all scanning during streams · off. Needs Tautulli.",
  "schedule.pause_on_streams": "Master switch for the stream pause below.",
  "schedule.windows": "Spin-up windows: only used when Spin up = windows. Cache and already-spinning disks are scanned around the clock.",
  "automation.windows": "Automation only acts inside these windows. Remove every window to allow it at any time.",
  "review.ocr_title_cards": "Read title cards automatically (OCR). A match passes the review; another episode's title raises 'wrong content'; no readable text leaves it for you or Claude.",
  "schedule.manual_respects_pause": "Manual/import scans also wait during parity checks and active streams.",
  "imports.enabled": "Scan new downloads as soon as Sonarr/Radarr import them. Add a Webhook connection in Sonarr/Radarr (On Import/Upgrade) pointing to http://<unraid-ip>:9797/api/webhook/sonarr (or /radarr).",
  "sync.interval_minutes": "Full library resync interval (0 = only manual / webhooks).",
  "whisper.model": "faster-whisper model, e.g. large-v3-turbo (GPU), large-v3, medium, small.",
  "whisper.device": "auto tries CUDA first, then falls back to CPU.",
  "whisper.cpu_fallback_model": "Smaller model used if the GPU is unavailable.",
  "whisper.idle_unload_minutes": "Unload the model after this long idle so Plex gets the VRAM back.",
  "automation.enabled": "Off by default. When on, proposals are executed automatically inside the windows below.",
  "automation.auto_resub": "Automatically ask Bazarr for new subtitles.",
  "automation.auto_redownload": "Automatically blocklist + redownload files with the issue types below.",
  "automation.redownload_codes": "Issue types that may trigger an automatic redownload.",
  "automation.redownload_delete_file": "Delete the bad file before searching (needed for a same-quality replacement).",
};
let SET = null;

async function settingsPage() {
  SET = await api("/api/settings");
  const s = SET.settings;
  const html = Object.keys(LABELS).map((k) => {
    if (["sonarr", "radarr", "bazarr", "tautulli"].includes(k)) {
      return `<section><h4>${LABELS[k]}</h4>${field(k + ".url", "URL", `<input data-k="${k}.url" value="${esc(s[k].url)}" placeholder="http://192.168.1.10:${{ sonarr: 8989, radarr: 7878, bazarr: 6767, tautulli: 8181 }[k]}" style="width:100%">`)}
        ${field(k + ".api_key", "API key", `<input data-k="${k}.api_key" type="password" value="${esc(s[k].api_key)}" style="width:100%">`)}
        <div class="field"><span></span><div><button class="sm" onclick="testSvc('${k}')">Test</button> <span id="t-${k}" class="small"></span></div></div></section>`;
    }
    if (k === "path_mappings") {
      return `<section><h4>${LABELS[k]}</h4><div class="small muted" style="margin-bottom:6px">${HELP.path_mappings}</div><div id="pm">${s.path_mappings.map(pmRow).join("")}</div>
        <button class="sm" onclick="$('#pm').insertAdjacentHTML('beforeend', pmRow({from:'',to:''}))">+ Add mapping</button>
        <button class="sm primary" onclick="checkPaths()">Check paths</button><div id="pathcheck"></div></section>`;
    }
    return `<section><h4>${LABELS[k]}</h4>${render(k, s[k])}${k === "whisper" ? `<div class="field"><span></span><div><button class="sm" onclick="testWhisper()">Load & test model</button> <span id="t-whisper" class="small"></span></div></div>` : ""}</section>`;
  }).join("");
  $("#app").innerHTML = `<h2>Settings</h2><div class="settings">${html}</div><div class="toolbar" style="position:sticky;bottom:0;background:var(--bg);padding:10px 0"><button class="primary" onclick="saveSettings()">Save settings</button><span class="muted small">Webhook URLs: /api/webhook/sonarr and /api/webhook/radarr${window.STATUS && window.STATUS.version ? " · v" + esc(window.STATUS.version) : ""}</span></div>`;
}
const pmRow = (m) => `<div class="win pmrow"><input class="pm-from" placeholder="/tv (as Sonarr sees it)" value="${esc(m.from)}"> → <input class="pm-to" placeholder="/mnt/user/media/tv" value="${esc(m.to)}" style="flex:1"><button class="sm" onclick="this.parentElement.remove()">✕</button></div>`;
function field(path, label, input) {
  return `<div class="field"><label>${esc(label)}</label><div>${input}</div>${HELP[path] ? `<div class="help">${esc(HELP[path])}</div>` : ""}</div>`;
}
const pretty = (k) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
function render(path, val) {
  return Object.entries(val).map(([k, v]) => {
    const p = `${path}.${k}`;
    if (k.endsWith("windows")) { const dflt = k === "quiet_windows" ? ["02:30", "04:30"] : ["01:00", "07:00"]; return field(p, pretty(k), `<div data-win="${p}">${v.map(winRow).join("")}</div><button class="sm" onclick="this.previousElementSibling.insertAdjacentHTML('beforeend', winRow({days:[0,1,2,3,4,5,6],start:'${dflt[0]}',end:'${dflt[1]}'}))">+ Add window</button>`); }
    if (p === "schedule.spinup") return field(p, "Spin up idle disks", `<select data-k="${p}">${["anytime", "windows", "never"].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`);
    if (p === "schedule.stream_pause") return field(p, pretty(k), `<select data-k="${p}">${["same_disk", "any", "off"].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`);
    if (p === "review.title_cards") return field(p, pretty(k), `<select data-k="${p}">${["never", "multi_episode", "flagged", "always"].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`);
    if (p === "schedule.profile" || p === "scan.default_profile" || p === "imports.profile") return field(p, pretty(k), `<select data-k="${p}">${["quick", "standard", "deep"].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`);
    if (p === "scan.hw_decode") return field(p, pretty(k), `<select data-k="${p}">${["auto", "off"].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`);
    if (p === "whisper.device") return field(p, pretty(k), `<select data-k="${p}">${["auto", "cuda", "cpu"].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`);
    if (p === "schedule.disks") return field(p, pretty(k), `<div data-list="${p}">${SET.disks.filter((d) => d).map((d) => `<label style="margin-right:10px"><input type="checkbox" value="${esc(d)}" ${v.includes(d) ? "checked" : ""}> ${esc(d)}</label>`).join("") || '<span class="muted">No disks detected yet (sync first)</span>'}</div>`);
    if (p === "automation.redownload_codes") return field(p, pretty(k), `<div data-list="${p}">${SET.redownload_codes.map((d) => `<label style="margin-right:10px;display:inline-block"><input type="checkbox" value="${d}" ${v.includes(d) ? "checked" : ""}> ${d}</label>`).join("")}</div>`);
    if (typeof v === "boolean") return field(p, pretty(k), `<input type="checkbox" data-k="${p}" data-type="bool" ${v ? "checked" : ""}>`);
    if (typeof v === "number") return field(p, pretty(k), `<input type="number" step="any" data-k="${p}" data-type="num" value="${v}" style="width:120px">`);
    if (typeof v === "string") return field(p, pretty(k), `<input data-k="${p}" value="${esc(v)}" style="width:260px">`);
    if (v && typeof v === "object" && !Array.isArray(v)) return `<div class="sub"><div class="muted" style="margin:6px 0 2px">${esc(pretty(k))}</div>${render(p, v)}</div>`;
    return field(p, pretty(k), `<input data-k="${p}" data-type="json" value='${esc(JSON.stringify(v))}' style="width:100%">`);
  }).join("");
}
function winRow(w) {
  return `<div class="win">${DAYS.map((d, i) => `<label><input type="checkbox" value="${i}" ${w.days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}
    <input type="time" class="w-start" value="${esc(w.start)}"> – <input type="time" class="w-end" value="${esc(w.end)}"><button class="sm" onclick="this.parentElement.remove()">✕</button></div>`;
}
function setPath(obj, path, val) { const ks = path.split("."); let o = obj; ks.slice(0, -1).forEach((k) => { o[k] = o[k] || {}; o = o[k]; }); o[ks[ks.length - 1]] = val; }
function collect() {
  const out = {};
  document.querySelectorAll("[data-k]").forEach((el) => {
    let v = el.value;
    if (el.dataset.type === "bool") v = el.checked; else if (el.dataset.type === "num") v = parseFloat(v); else if (el.dataset.type === "json") { try { v = JSON.parse(v); } catch { return; } }
    setPath(out, el.dataset.k, v);
  });
  document.querySelectorAll("[data-win]").forEach((el) => {
    setPath(out, el.dataset.win, [...el.querySelectorAll(".win")].map((w) => ({ days: [...w.querySelectorAll("input[type=checkbox]:checked")].map((c) => +c.value), start: $(".w-start", w).value, end: $(".w-end", w).value })));
  });
  document.querySelectorAll("[data-list]").forEach((el) => setPath(out, el.dataset.list, [...el.querySelectorAll("input:checked")].map((c) => c.value)));
  out.path_mappings = [...document.querySelectorAll(".pmrow")].map((r) => ({ from: $(".pm-from", r).value.trim(), to: $(".pm-to", r).value.trim() })).filter((m) => m.from && m.to);
  return out;
}
async function checkPaths() {
  $("#pathcheck").innerHTML = '<div class="muted small" style="margin-top:8px">Checking a sample of files…</div>';
  const r = await api("/api/paths/check");
  const sug = r.suggestions.map((g, i) => `<div class="win"><b>${esc(g.from)}</b> → <b>${esc(g.to)}</b> <span class="muted small">fixes ${g.files} of the sampled files</span>
      <button class="sm primary" onclick="applyMapping(${i})">Use this</button></div>`).join("");
  window._sugg = r.suggestions;
  $("#pathcheck").innerHTML = `<div style="margin-top:10px">
    ${!r.mnt_visible ? '<div class="card" style="border-color:var(--error)">/mnt/user is not visible inside the container - check the "Unraid /mnt" path in the Docker template.</div>' : ""}
    <div><b>${r.found} of ${r.checked}</b> sampled files found with the current mappings.</div>
    ${sug ? `<h3>Suggested mappings</h3>${sug}<div class="small muted">"Use this" adds it to the list and saves; affected files are re-checked automatically.</div>` : (r.found < r.checked ? '<div class="small muted">No suggestion found - the files may be outside /mnt/user.</div>' : "")}
    <details style="margin-top:8px"><summary class="muted small">Show sampled files</summary><table class="small"><tr><th></th><th>Sonarr/Radarr path</th><th>Looked for</th></tr>
    ${r.samples.map((x) => `<tr><td>${x.found ? '<span class="sev s-ok">ok</span>' : '<span class="sev s-error">missing</span>'}</td><td>${esc(x.arr_path)}</td><td>${esc(x.read_path)}</td></tr>`).join("")}</table></details></div>`;
}
async function applyMapping(i) {
  const g = window._sugg[i];
  // replace a mapping with the same "from", otherwise add it
  const rows = [...document.querySelectorAll(".pmrow")];
  const same = rows.find((row) => $(".pm-from", row).value.trim() === g.from);
  if (same) $(".pm-to", same).value = g.to; else $("#pm").insertAdjacentHTML("beforeend", pmRow({ from: g.from, to: g.to }));
  await saveSettings();
  setTimeout(checkPaths, 1500);
}

async function saveSettings() { try { await api("/api/settings", { method: "PUT", body: collect() }); toast("Settings saved"); settingsPage(); } catch (e) { toast("Error: " + e.message); } }
async function testSvc(k) {
  $("#t-" + k).textContent = "testing…";
  const r = await api("/api/settings/test/" + k, { method: "POST", body: { url: $(`[data-k="${k}.url"]`).value, api_key: $(`[data-k="${k}.api_key"]`).value } });
  $("#t-" + k).innerHTML = r.ok ? `<span style="color:var(--ok)">OK - ${esc(r.version)}</span>` : `<span style="color:var(--error)">${esc(r.error)}</span>`;
}
async function testWhisper() {
  $("#t-whisper").textContent = "loading (first run downloads the model, can take a few minutes)…";
  const r = await api("/api/whisper/test", { method: "POST" });
  $("#t-whisper").innerHTML = r.ok ? `<span style="color:var(--ok)">${esc(r.model)} on ${esc(r.device)} (${r.seconds}s)</span>` : `<span style="color:var(--error)">${esc(r.error)}</span>`;
}

refreshStatus().then(route);
