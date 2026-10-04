import { paintPage, loadImage } from "./draw.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const LABELS = { age_fit: "Right for ages 5-10", comfort: "Cozy & safe", arc: "Story shape", fidelity: "Your ideas kept",
  heart: "Lovable characters", read_aloud: "Fun to read aloud", moral: "Lesson shown, not preached" };

// ------------------------------------------------------------------ state
const state = {
  refs: [],            // data URLs of uploaded photos (never stored server-side)
  story: null,
  pages: [],           // per page: {image, audio, check}
  moralAudio: null,
  page: 0,
  reading: false,
  painter: null,
  audio: new Audio(),
  report: null,
};

// ------------------------------------------------------------------ passcode
let passcode = "";
try { passcode = localStorage.getItem("pie-pass") || ""; } catch {}
function askPasscode() {
  return new Promise((resolve) => {
    const m = $("#pass-modal");
    m.hidden = false;
    $("#pass-input").focus();
    $("#pass-form").onsubmit = (e) => {
      e.preventDefault();
      passcode = $("#pass-input").value.trim();
      try { localStorage.setItem("pie-pass", passcode); } catch {}
      m.hidden = true;
      resolve();
    };
  });
}

async function api(path, body, { stream = false, raw = false } = {}) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Passcode": passcode },
      body: JSON.stringify(body),
    });
    if (res.status === 401) { await askPasscode(); continue; }
    if (!res.ok) {
      let msg = `Something went wrong (${res.status}).`;
      try { msg = (await res.json()).detail || msg; } catch {}
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    if (stream) return res;
    return raw ? res.blob() : res.json();
  }
  throw new Error("Passcode was not accepted.");
}

async function* ndjson(res) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buf.trim()) yield JSON.parse(buf);
}

// ------------------------------------------------------------------ views
function show(view) {
  if (view === "shelf") renderShelf();
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  window.scrollTo({ top: 0 });
  if (view !== "book") stopReading();
  if (view === "create") setTimeout(() => $("#idea").focus(), 50);
}
$$("[data-go]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); show(b.dataset.go); }));

// Other pages link back with index.html#create, #shelf or #how, so every page can reach every other.
function routeFromHash() {
  const target = location.hash.slice(1);
  if (target === "create" || target === "shelf") show(target);
  else if (target === "how") { show("home"); setTimeout(() => window.scrollTo({ top: $("#how").getBoundingClientRect().top + window.scrollY - 70, behavior: "instant" }), 50); }
}
$$('a[href="#how"]').forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); history.replaceState(null, "", "#how"); routeFromHash(); }));
window.addEventListener("hashchange", routeFromHash);

// ------------------------------------------------------------------ create: text, chips
const idea = $("#idea");
idea.addEventListener("input", () => ($("#count").textContent = `${idea.value.length} / 2000`));
$$(".chip").forEach((c) => c.addEventListener("click", () => { idea.value = c.textContent; idea.dispatchEvent(new Event("input")); }));

// ------------------------------------------------------------------ create: photos
$("#photos").addEventListener("change", async (e) => {
  for (const f of [...e.target.files].slice(0, 3 - state.refs.length)) {
    state.refs.push(await shrink(f));
  }
  e.target.value = "";
  drawThumbs();
});
function shrink(file, max = 768) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const s = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      resolve(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
  });
}
function drawThumbs() {
  $("#thumbs").innerHTML = state.refs.map((r, i) =>
    `<div class="thumb"><img src="${r}" alt="Reference photo ${i + 1}"><button aria-label="Remove photo" data-i="${i}">&times;</button></div>`).join("");
  $$("#thumbs button").forEach((b) => b.onclick = () => { state.refs.splice(+b.dataset.i, 1); drawThumbs(); });
}

// ------------------------------------------------------------------ create: voice
let rec = null, chunks = [], recTimer = null;
$("#mic").addEventListener("click", async () => {
  const btn = $("#mic");
  if (rec && rec.state === "recording") { rec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ["audio/webm", "audio/mp4", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    rec = new MediaRecorder(stream, type ? { mimeType: type } : {});
    chunks = [];
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      clearInterval(recTimer);
      stream.getTracks().forEach((t) => t.stop());
      btn.setAttribute("aria-pressed", "false");
      $("#mic-label").textContent = "Pitman is writing it down...";
      try {
        const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        const audio = await new Promise((r) => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(blob); });
        const { text } = await api("/api/transcribe", { audio });
        idea.value = (idea.value.trim() + " " + text).trim();
        idea.dispatchEvent(new Event("input"));
      } catch (err) { $("#create-error").textContent = err.message; }
      $("#mic-label").textContent = "Say it";
    };
    rec.start();
    btn.setAttribute("aria-pressed", "true");
    let s = 0;
    $("#mic-label").textContent = "Tap to stop · 0s";
    recTimer = setInterval(() => { s++; $("#mic-label").textContent = `Tap to stop · ${s}s`; if (s >= 90) rec.stop(); }, 1000);
  } catch {
    $("#create-error").textContent = "Microphone is not available. You can type instead.";
  }
});

// ------------------------------------------------------------------ workshop log
const log = $("#log");
function say(agent, html, cls = "") {
  $$(".msg.working", log).forEach((m) => m.classList.remove("working"));
  const who = agent === "Vishnu" ? "vishnu" : "pie";
  const li = document.createElement("li");
  li.className = `msg ${who} ${cls}`;
  li.innerHTML = `<div class="avatar">${agent === "Vishnu" ? "V" : "UP"}</div><div class="bubble"><div class="who">${esc(agent)}</div>${html}</div>`;
  log.appendChild(li);
  li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return li;
}
function handoffCard(h) {
  return `<div class="handoff"><span class="route">${esc(h.from)} &rarr; ${esc(h.to)}</span>
    <details><summary>Prompt handed over</summary><pre>${esc(h.prompt)}</pre></details></div>`;
}
function scoreBlock(ev) {
  const rows = Object.entries(ev.scores).map(([k, v]) =>
    `<span>${LABELS[k] || k}</span><span class="bar"><i class="${v < 4 ? "low" : ""}" style="width:${v * 20}%"></i></span><b>${v}/5</b>`).join("");
  const ideas = (ev.checklist || []).map((c) => `<li>${c.present ? "✅" : "❌"} ${esc(c.item)}${c.present ? ` <em>(page ${c.page})</em>` : ""}</li>`).join("");
  const logic = ev.logic || [];
  return `<p>Draft ${ev.round}: ${esc(ev.summary)}</p>
    ${ideas ? `<p class="mini-h">Ida checked your ideas against quotes from the story:</p><ul>${ideas}</ul>` : ""}
    <p class="mini-h">Story logic: ${logic.length ? "" : "✅ no repeats or contradictions found"}</p>
    ${logic.length ? `<ul>${logic.map((x) => `<li>❌ Page ${x.page}: ${esc(x.problem)}</li>`).join("")}</ul>` : ""}
    <div class="scores">${rows}</div>
    <span class="verdict ${ev.passed ? "pass" : "fail"}">${ev.passed ? "Ready to print" : "Back to Uncle Pie"}</span>
    ${!ev.passed && ev.fixes?.length ? `<ul>${ev.fixes.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}`;
}

function handleStoryEvent(ev) {
  switch (ev.type) {
    case "stage": say(ev.agent, `<p>${esc(ev.message)}</p>`, "working"); break;
    case "references": say("Uncle Pie", `<p>Homai looked at your photos and sees:</p><ul>${ev.subjects.map((s) => `<li><b>${esc(s.label)}</b>: ${esc(s.look)}</li>`).join("")}</ul>`); break;
    case "brief": {
      const safety = ev.safety && ev.safety.ok === false ? `<p>I'll tell a gentler version: ${esc(ev.safety.reframe)}</p>` : "";
      const must = ev.must_include.length ? `<p class="mini-h">I'll make sure these happen:</p><ul>${ev.must_include.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : "";
      say("Uncle Pie", `<p>A <b>${esc(ev.category)}</b> story in ${ev.pages} pages. The lesson: <em>${esc(ev.moral)}</em></p>${must}${safety}`);
      break;
    }
    case "plan":
      if (ev.characters && !state.castJob) startCast(ev.characters);
      say("Uncle Pie", `<p>My plan for <b>${esc(ev.title)}</b>${ev.refrain ? `, with a line to say together: <em>"${esc(ev.refrain)}"</em>` : ""}</p><ul>${ev.beats.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`);
      break;
    case "handoff":
      if (!/Page \d/.test(ev.to)) say(ev.from.startsWith("Vishnu") ? "Vishnu" : "Uncle Pie", handoffCard(ev), "handoff-msg");
      break;
    case "draft": say("Uncle Pie", `<p>Draft ${ev.round} is ready: <b>${esc(ev.title)}</b>. Vishnu, your turn.</p>`); break;
    case "review": say("Vishnu", scoreBlock(ev)); break;
    case "error": throw new Error(ev.message);
  }
}

// ------------------------------------------------------------------ media lanes (after the text)
// The text is what the family reads, so the book opens the moment Vishnu passes it. Narration and
// pictures then run as two independent lanes: neither waits for the other.
function workerBoard(story) {
  const li = say("Uncle Pie", `<p>The story is ready, and the book is open. Meanwhile, two teams work at the same time:
    Ameen records every page, and Caldecott paints them from one shared cast sheet while Ursula checks each picture.</p>
    <div class="cast" id="cast"><div class="cast-img"><span class="brush"></span></div>
      <div><b>Cast sheet</b><p class="hint" id="cast-status">Painting the characters...</p>
      <ul>${story.characters.map((c) => `<li><b>${esc(c.name)}</b>: ${esc(c.look)}</li>`).join("")}</ul></div></div>
    <div class="workers">${story.pages.map((p, i) => `
      <div class="worker" id="w${i + 1}">
        <div class="w-head"><b>Page ${i + 1}</b><span class="w-step">waiting</span></div>
        <p class="voice-dir" id="v${i + 1}">🔈 voice: recording...</p>
        ${p.moment ? `<p class="voice-dir">Must show: ${esc(p.moment)}</p>` : ""}
        <details><summary>Shot spec from Uncle Pie</summary><pre class="spec">being written...</pre></details>
        <div class="w-checks"></div>
      </div>`).join("")}</div>
    <div class="board-actions"><button class="btn btn-primary" data-go-book>Back to the book</button></div>`);
  $("[data-go-book]", li).onclick = () => show("book");
}
function workerUpdate(ev) {
  const w = $(`#w${ev.page}`);
  if (!w) return;
  const steps = { directing: "Uncle Pie planning the shot", painting: "Caldecott painting", judging: "Ursula checking", redrawing: "Caldecott redrawing" };
  if (ev.type === "page") {
    $(".w-step", w).textContent = steps[ev.step] || ev.step;
    w.dataset.step = ev.step;
    if (ev.prompt) $(".spec", w).textContent = ev.prompt;
    if (ev.step === "redrawing") $(".w-checks", w).insertAdjacentHTML("beforeend", `<p class="redo">Ursula: ${esc(ev.message.replace(/^Redrawing: /, ""))}</p>`);
  } else if (ev.type === "picture_result") {
    w.dataset.step = "done";
    $(".w-step", w).textContent = "painted";
    const ic = ev.image_check;
    $(".w-checks", w).innerHTML += `<p>${ic.passed ? "✅" : "⚠️"} picture${ic.redrawn ? " (redrawn once)" : ""}</p>`
      + (ic.what_i_see ? `<p class="voice-dir">Ursula saw: ${esc(ic.what_i_see)}</p>` : "");
  }
}

const slot = (i) => (state.pages[i] = state.pages[i] || { image: null, audio: null, check: { image_check: {}, audio_check: {} } });

// Narration lane: one request per page plus the moral. Needs only the text.
function narrationLane(s) {
  const jobs = [...s.pages, { text: `And here is what our story teaches us. ${s.moral}`, feeling: "warm, slow and sleepy" }]
    .map((p, i) => fetch("/api/narrate", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Passcode": passcode },
      body: JSON.stringify({ text: p.text, feeling: p.feeling || "" }),
    }).then(async (res) => {
      if (!res.ok) throw new Error(`voice failed (${res.status})`);
      let check = {};
      try { check = JSON.parse(res.headers.get("X-Audio-Check") || "{}"); } catch {}
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (i === s.pages.length) { state.moralAudio = url; state.moralBlob = blob; }
      else { slot(i).audio = url; slot(i).audioBlob = blob; slot(i).check.audio_check = check; const v = $(`#v${i + 1}`); if (v) v.textContent = `🔈 voice: ${check.passed ? "✅ ready" : "⚠️ ready"}`; }
      onMediaReady(i);
    }).catch((err) => { const v = $(`#v${i + 1}`); if (v) v.textContent = `🔈 voice: ${err.message}`; }));
  return Promise.all(jobs);
}

// Picture lane: cast sheet (often already started during writing), then one worker per page.
async function pictureLane(s) {
  let cast = null;
  try {
    cast = (await (state.castJob || startCast(s.characters))).image;
    s.cast_image = cast;
    $("#cast .cast-img").innerHTML = `<img src="${cast}" alt="Character sheet">`;
    $("#cast-status").textContent = "Done. Every picture worker gets this as its reference.";
  } catch (err) {
    $("#cast-status").textContent = `Skipped (${err.message}); pages will use the written descriptions only.`;
  }
  const refs = [cast, ...state.refs].filter(Boolean);
  const story = { title: s.title, moral: s.moral, characters: s.characters, pages: s.pages };
  return Promise.all(s.pages.map(async (_, i) => {
    try {
      const res = await api("/api/picture", { index: i, story, references: refs }, { stream: true });
      for await (const ev of ndjson(res)) {
        if (ev.type === "error") throw new Error(ev.message);
        if (ev.type === "picture_result") {
          slot(i).image = `data:image/jpeg;base64,${ev.image_b64}`;
          slot(i).check.image_check = ev.image_check;
          slot(i).check.prompt_trail = ev.prompt_trail;
          workerUpdate(ev);
          onMediaReady(i);
        } else if (ev.type === "page") workerUpdate(ev);
      }
    } catch (err) {
      const w = $(`#w${i + 1}`);
      if (w) { w.dataset.step = "failed"; $(".w-step", w).textContent = "failed"; $(".w-checks", w).innerHTML += `<p class="redo">${esc(err.message)}</p>`; }
      onMediaReady(i);
    }
  }));
}

function studioStatus() {
  const s = state.story, n = s?.pages.length || 0;
  const voices = state.pages.filter((p) => p?.audio).length, pics = state.pages.filter((p) => p?.image).length;
  const el = $("#studio");
  if (!el || !state.live) { if (el) el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<span>🔈 Voice ${voices}/${n}</span><span>🎨 Pictures ${pics}/${n}</span>
    ${voices + pics < 2 * n ? `<button class="linkish" data-go="workshop">Watch the studio</button>`
      : `<span>Saved to your <button class="linkish" data-go="shelf">bookshelf</button></span>`}`;
  $$("#studio [data-go=shelf]").forEach((b) => b.onclick = () => show("shelf"));
  $$("#studio [data-go=workshop]").forEach((b) => b.onclick = () => show("workshop"));
}

async function fanOut() {
  const s = state.story;
  state.pages = s.pages.map(() => null);
  state.moralAudio = null;
  state.moralBlob = null;
  state.live = true;
  s.id = s.id || `book-${Date.now()}`;
  workerBoard(s);
  openBook(0);                     // the family can start reading right away
  saveBook();                      // the text goes on the shelf at once; pictures and voice are added when ready
  const t0 = performance.now();
  const voices = narrationLane(s).then(() => (s.timings = { ...(s.timings || {}), voice_s: Math.round((performance.now() - t0) / 1000) }));
  const pictures = pictureLane(s).then(() => (s.timings = { ...(s.timings || {}), pictures_s: Math.round((performance.now() - t0) / 1000) }));
  await Promise.all([voices, pictures]);
  try {
    state.report = await api("/api/report", { story: s, pages: state.pages.map((p) => p?.check || {}) });
    s.report = state.report;
    renderNotes();
  } catch {}
  await saveBook();
  studioStatus();
}

// The cast sheet only needs the characters, which exist once the plan is made. Starting it then
// overlaps ~45 s of painting with the writing and reviewing.
function startCast(characters) {
  state.castJob = api("/api/cast", { characters, references: state.refs });
  state.castJob.catch(() => {});
  return state.castJob;
}

function onMediaReady(i) {
  studioStatus();
  if (!$("#view-book").classList.contains("active")) return;
  const last = state.story.pages.length;
  if (state.page === i || (i === last && state.page === last)) renderPage(state.page, { keepAudio: true });
}

// ------------------------------------------------------------------ create: go
$("#go").addEventListener("click", async () => {
  const text = idea.value.trim();
  $("#create-error").textContent = "";
  if (!text) { $("#create-error").textContent = "Tell Uncle Pie a little about the story first."; return; }
  $("#go").disabled = true;
  log.innerHTML = "";
  state.report = null;
  state.castJob = null;
  show("workshop");
  try {
    const res = await api("/api/story", { request: text, references: state.refs }, { stream: true });
    let story = null;
    for await (const ev of ndjson(res)) {
      if (ev.type === "done") story = ev.story; else handleStoryEvent(ev);
    }
    if (!story) throw new Error("Uncle Pie lost his place. Please try again.");
    state.story = story;
    await fanOut();
  } catch (err) {
    say("Uncle Pie", `<p class="error">${esc(err.message)}</p><button class="btn btn-ghost" data-go="create">Try again</button>`);
    $$("#log [data-go]").forEach((b) => b.onclick = () => show("create"));
  } finally {
    $("#go").disabled = false;
  }
});

// ------------------------------------------------------------------ book
const art = $("#art");
function openBook(startAt = 0) {
  show("book");
  $("#book-title").textContent = state.story.title;
  $("#after").hidden = true;
  $("#dots").innerHTML = [...state.story.pages, "moral"].map((_, i) => `<button aria-label="Page ${i + 1}"></button>`).join("");
  $$("#dots button").forEach((b, i) => b.onclick = () => goTo(i));
  renderNotes();
  goTo(startAt);
}
const total = () => state.story.pages.length + 1; // + moral page

function goTo(i) {
  state.page = Math.max(0, Math.min(total() - 1, i));
  renderPage(state.page);
}

function stopReading() {
  state.audio.pause();
  state.reading = false;
  $("#play").textContent = "Read to me";
  $("#play").setAttribute("aria-pressed", "false");
}

async function renderPage(i, { keepAudio = false } = {}) {
  const s = state.story, last = i === s.pages.length;
  $$("#dots button").forEach((b, k) => b.classList.toggle("on", k === i));
  $("#prev").disabled = i === 0;
  $("#next").disabled = i === total() - 1;
  $("#after").hidden = !last;
  const text = $("#story-text");
  text.classList.toggle("moral", last);
  if (last) {
    $("#beat").textContent = "The moral of the story";
    text.textContent = s.moral;
    $("#page-num").textContent = "The End";
  } else {
    $("#beat").textContent = s.pages[i].beat || "";
    text.textContent = s.pages[i].text;
    $("#page-num").textContent = `${i + 1}`;
  }
  studioStatus();

  // Voice and picture are independent: play the voice as soon as it exists, paint when the picture exists.
  const pg = last ? state.pages[s.pages.length - 1] : state.pages[i];
  const audioSrc = last ? state.moralAudio : pg?.audio;
  if (!keepAudio) state.audio.pause();
  const startAudio = () => {
    const alreadyPlaying = !state.audio.paused && state.audioPage === i;
    if (state.reading && audioSrc && state.page === i && !alreadyPlaying) { state.audioPage = i; playAudio(audioSrc); }
  };

  const ctx = art.getContext("2d");
  if (!pg || !pg.image) {
    state.painter?.cancel();
    state.paintedKey = null;
    ctx.fillStyle = "#fbf5e8"; ctx.fillRect(0, 0, art.width, art.height);
    $("#painting").hidden = false;
    startAudio();
    return;
  }
  $("#painting").hidden = true;
  const key = `${i}:${pg.image.length}`;
  if (keepAudio && state.paintedKey === key) { startAudio(); return; }  // already painted; only the voice arrived
  state.painter?.cancel();
  state.paintedKey = key;
  if (last) {
    const img = await loadImage(pg.image);
    ctx.drawImage(img, 0, 0, art.width, art.height);
    startAudio();
    return;
  }
  // If the voice is already playing, keep it going and paint alongside; otherwise start it as the colour washes in.
  state.painter = paintPage(art, pg.image, { seed: i * 31 + 7, onWash: startAudio });
}

function playAudio(src) {
  state.audio.src = src;
  state.audio.currentTime = 0;
  state.audio.play().catch(() => stopReading());
}
state.audio.addEventListener("ended", () => {
  if (!state.reading) return;
  if (state.page < total() - 1) setTimeout(() => state.reading && goTo(state.page + 1), 1200);
  else stopReading();
});

$("#prev").onclick = () => goTo(state.page - 1);
$("#next").onclick = () => goTo(state.page + 1);
$("#play").onclick = () => {
  if (state.reading) { stopReading(); return; }
  state.reading = true;
  $("#play").textContent = "Pause";
  $("#play").setAttribute("aria-pressed", "true");
  renderPage(state.page);
};
document.addEventListener("keydown", (e) => {
  if (!$("#view-book").classList.contains("active") || e.target.matches("input, textarea")) return;
  if (e.key === "ArrowRight") goTo(state.page + 1);
  if (e.key === "ArrowLeft") goTo(state.page - 1);
});

function renderNotes() {
  const s = state.story;
  if (!s) return;
  const r = s.report || state.report;
  const card = r ? `<div class="round report"><h4>Report card: ${r.overall}/100</h4>
    <p>Story ${r.text.score}/100 (draft ${r.text.chosen_draft} of ${r.text.drafts}) · your ideas kept ${r.fidelity.found}/${r.fidelity.asked}
    · reading grade ${r.reading_grade} · pictures approved ${r.pictures.passed}/${r.pictures.total}${r.pictures.redrawn ? ` (${r.pictures.redrawn} redrawn)` : ""}
    · narration ${r.narration.passed}/${r.narration.total}</p><p class="hint">Scored by code, not by a model, so the same book always gets the same grade.</p></div>` : "";
  const rounds = (s.reviews || []).map((rv) => `<div class="round"><h4>Draft ${rv.round} ${rv.passed ? "✅" : ""}</h4>${scoreBlock(rv)}</div>`).join("");
  const trail = (s.handoffs || []).map((h) => handoffCard(h)).join("");
  $("#notes").innerHTML = card + rounds + (trail ? `<div class="round"><h4>Prompts the agents handed each other</h4>${trail}</div>` : "");
}

$("#revise").addEventListener("click", async () => {
  const fb = $("#feedback").value.trim();
  if (!fb || !state.story) return;
  $("#revise").disabled = true;
  log.innerHTML = "";
  show("workshop");
  try {
    const res = await api("/api/revise", { story: state.story, feedback: fb }, { stream: true });
    let story = null;
    for await (const ev of ndjson(res)) { if (ev.type === "done") story = ev.story; else handleStoryEvent(ev); }
    if (!story) throw new Error("Uncle Pie lost his place. Please try again.");
    state.story = story;
    $("#feedback").value = "";
    await fanOut();
  } catch (err) {
    say("Uncle Pie", `<p class="error">${esc(err.message)}</p>`);
  } finally {
    $("#revise").disabled = false;
  }
});

// ------------------------------------------------------------------ demo
let demo = null;
async function loadDemo() {
  try {
    const story = await (await fetch("demo/story.json", { cache: "no-cache" })).json();
    demo = {
      story,
      pages: story.pages.map((p) => ({ image: `demo/${p.image}`, audio: `demo/${p.audio}`, check: { image_check: p.image_check, audio_check: p.audio_check } })),
      moralAudio: story.moral_audio ? `demo/${story.moral_audio}` : null,
    };
    $("#demo-title").textContent = story.title;
    if ($("#view-shelf").classList.contains("active")) renderShelf();  // add the demo book once it has loaded
    heroLoop();
  } catch { $(".hero-book").hidden = true; $("#open-demo").hidden = true; }
}
async function heroLoop() {
  const canvas = $("#demo-canvas");
  let i = 0;
  for (;;) {
    if (!$("#view-home").classList.contains("active")) { await new Promise((r) => setTimeout(r, 800)); continue; }
    $("#demo-page").textContent = i + 1;
    $("#demo-caption").textContent = demo.story.pages[i].text;
    await paintPage(canvas, demo.pages[i].image, { inkMs: 2600, washMs: 2600, seed: i * 31 + 7 }).done;
    await new Promise((r) => setTimeout(r, 3500));
    i = (i + 1) % demo.pages.length;
  }
}
$("#open-demo").addEventListener("click", () => {
  if (!demo) return;
  state.story = demo.story;
  state.pages = demo.pages;
  state.moralAudio = demo.moralAudio;
  state.report = demo.story.report || null;
  state.live = false;
  openBook(0);
});

// ------------------------------------------------------------------ bookshelf
// Finished books are kept in this browser (IndexedDB: room for pictures and audio). Nothing goes to a
// server, which keeps family stories and photo-inspired pictures private. Every call is guarded so the
// app still works where storage is blocked (private windows, previews).
const shelfDB = () => new Promise((resolve, reject) => {
  const req = indexedDB.open("uncle-pie-shelf", 1);
  req.onupgradeneeded = () => req.result.createObjectStore("books", { keyPath: "id" });
  req.onsuccess = () => resolve(req.result);
  req.onerror = () => reject(req.error);
});
async function shelfDo(mode, fn) {
  const db = await shelfDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("books", mode);
    const out = fn(tx.objectStore("books"));
    tx.oncomplete = () => resolve(out?.result);
    tx.onerror = () => reject(tx.error);
  });
}
async function saveBook() {
  const s = state.story;
  if (!s || !state.live) return;
  try {
    const story = JSON.parse(JSON.stringify(s));
    await shelfDo("readwrite", (st) => st.put({
      id: s.id, title: s.title, created: s.created || (s.created = Date.now()), story,
      pages: state.pages.map((p) => ({ image: p?.image || null, audio: p?.audioBlob || null, check: p?.check || {} })),
      moral: state.moralBlob || null,
    }));
    studioStatus();
  } catch { /* storage unavailable: the book still works, it just isn't kept */ }
}
async function renderShelf() {
  const el = $("#shelf");
  let books = [];
  try { books = (await shelfDo("readonly", (st) => st.getAll())) || []; } catch {}
  books.sort((a, b) => b.created - a.created);
  const card = (id, title, cover, sub, removable) => `
    <div style="position:relative"><button class="spine" data-open="${id}">
      ${cover ? `<img class="cover" src="${cover}" alt="">` : `<span class="cover blank">being painted...</span>`}
      <span class="meta"><b>${esc(title)}</b><span>${esc(sub)}</span></span></button>
      ${removable ? `<button class="remove" data-remove="${id}" aria-label="Remove ${esc(title)} from the shelf">&times;</button>` : ""}</div>`;
  el.innerHTML = (demo ? card("demo", demo.story.title, demo.pages[0].image, "Demo book", false) : "")
    + books.map((b) => card(b.id, b.title, b.pages.find((p) => p.image)?.image,
        new Date(b.created).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }), true)).join("")
    + (books.length ? "" : `<p class="shelf-empty">Books you make will appear here.</p>`);
  $$("[data-open]", el).forEach((b) => b.onclick = () => openFromShelf(b.dataset.open, books));
  $$("[data-remove]", el).forEach((b) => b.onclick = async () => {
    if (!confirm("Remove this book from your shelf?")) return;
    try { await shelfDo("readwrite", (st) => st.delete(b.dataset.remove)); } catch {}
    renderShelf();
  });
}
function openFromShelf(id, books) {
  if (id === "demo") { $("#open-demo").click(); return; }
  const b = books.find((x) => x.id === id);
  if (!b) return;
  state.story = b.story;
  state.pages = b.pages.map((p) => ({ image: p.image, audio: p.audio ? URL.createObjectURL(p.audio) : null, check: p.check }));
  state.moralAudio = b.moral ? URL.createObjectURL(b.moral) : null;
  state.report = b.story.report || null;
  state.live = false;
  openBook(0);
}

loadDemo();
routeFromHash();
