"use strict";
const $ = (id) => document.getElementById(id);
let backend = "fake"; // set from /api/auth/csrf at startup
const real = () => backend === "real";
let csrf = "", current = null, dirty = false, timer = null, editorGeneration = 0;
function status(message, error = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", error);
}
async function api(path, method = "GET", payload = undefined, headers = {}) {
  const options = {method, credentials: "same-origin", headers: {...headers}};
  if (method !== "GET") options.headers["X-CSRF-Token"] = csrf;
  if (payload !== undefined) {
    if (payload instanceof Blob) options.body = payload;
    else { options.body = JSON.stringify(payload); options.headers["Content-Type"] = "application/json"; }
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) loggedOut();
    throw new Error(`${response.status}: ${typeof data.detail === "string" ? data.detail : "Invalid input; check the fields."}`);
  }
  return data;
}
function guard(fn) {
  return async (event) => {
    if (event) event.preventDefault();
    const button = event?.submitter || (event?.currentTarget?.tagName === "BUTTON" ? event.currentTarget : null);
    if (button) button.disabled = true;
    try { await fn(event); } catch (error) { status(error.message, true); }
    finally { if (button) button.disabled = false; updateButtons(); }
  };
}
function updateButtons() {
  $("record").disabled = !current || !!rec || starting;
  $("analyze").disabled = !current || dirty || !current.confirmed_transcript;
  $("confirm").disabled = !current || dirty || !current.draft_transcript;
}
function loggedOut() {
  abortRecording();
  editorGeneration++;
  clearTimeout(timer); current = null;
  $("workspace").hidden = true; $("auth").hidden = false;
  $("editor").hidden = $("results").hidden = true;
  $("transcript").value = ""; $("feedback").replaceChildren(); $("history-list").replaceChildren();
  $("email").focus();
}
function showPractice(value, focus = true) {
  if (rec && value.id !== rec.sid) abortRecording();
  editorGeneration++;
  current = value; dirty = false;
  $("editor").hidden = $("results").hidden = false;
  $("transcript").value = value.draft_transcript || "";
  $("revision").textContent = `${value.mode} · revision ${value.transcript_revision} · ${value.confirmed_transcript ? "confirmed" : "not confirmed"}`;
  updateButtons();
  if (focus) $("editor-title").focus();
}
async function history() {
  const rows = await api("/api/sessions");
  $("history-list").replaceChildren();
  for (const row of rows) {
    const li = document.createElement("li"), button = document.createElement("button");
    button.className = "secondary";
    button.textContent = `${row.mode} · ${new Date(row.created_at).toLocaleString()}`;
    button.onclick = guard(async () => {
      if (dirty && !window.confirm("Discard unsaved draft changes?")) return;
      showPractice(await api(`/api/sessions/${row.id}`)); await refresh();
    });
    li.append(button); $("history-list").append(li);
  }
  if (!rows.length) { const li = document.createElement("li"); li.textContent = "No practices yet."; $("history-list").append(li); }
}
function applyEditorResponse(value, sid, generation) {
  if (!current || current.id !== sid || value.transcript_revision < current.transcript_revision) return;
  if (editorGeneration === generation) showPractice(value, false);
  else {
    // Keep text typed after submission, but use the saved revision for the next save.
    current = value;
    dirty = true;
    $("revision").textContent = `${value.mode} · saved revision ${value.transcript_revision} · unsaved changes`;
    updateButtons();
  }
}
async function refresh() {
  clearTimeout(timer);
  if (!current) return;
  const sid = current.id;
  const report = await api(`/api/sessions/${sid}/report`);
  if (!current || current.id !== sid) return;
  $("report-state").textContent = report.report_complete ? (report.fake ? "Current FAKE results available. Not an assessment." : "Current results available. Not an official assessment.") : "Report incomplete: confirm your current words and queue grammar; see requested stages below.";
  $("job-list").replaceChildren(); $("feedback").replaceChildren();
  const retried = new Set(report.jobs.map((j) => j.retry_of_job_id).filter(Boolean));
  for (const job of report.jobs) {
    const p = document.createElement("p"); p.className = "job";
    p.textContent = `${job.kind}${job.attempt_no > 1 ? " (attempt " + job.attempt_no + ")" : ""}: ${job.status}${job.error_code ? " / " + job.error_code : ""} · ${Object.entries(job.stages).map(([name,state]) => `${name}: ${state}`).join("; ")}`;
    if (job.status === "failed" && job.attempt_no < job.max_attempts && !retried.has(job.id)) {
      const button = document.createElement("button"); button.className = "secondary"; button.textContent = "Retry";
      button.onclick = guard(async () => {
        await api(`/api/jobs/${job.id}/retry`, "POST", {idempotency_key: crypto.randomUUID()});
        status("Retry queued as a new job."); await refresh();
      });
      p.append(" ", button);
    }
    $("job-list").append(p);
  }
  for (const result of report.grammar) {
    const box = document.createElement("div"), title = document.createElement("h3"), original = document.createElement("p"), corrected = document.createElement("p"), warning = document.createElement("p");
    box.className = "result"; title.textContent = `${result.fake ? "FAKE grammar" : "Grammar"} · revision ${result.revision}${result.is_current ? " (current)" : " (not current)"}`;
    original.textContent = `Confirmed original: ${result.source_text}`;
    corrected.textContent = `${result.fake ? "Canned suggestion" : "Suggestion"}: ${result.corrected_text}`;
    warning.textContent = result.warnings.join(" "); warning.className = "hint";
    box.append(title, original, corrected, warning); $("feedback").append(box);
  }
  for (const result of report.pronunciation) {
    const p = document.createElement("p"); p.className = "result";
    p.textContent = `${result.fake ? "FAKE pronunciation" : "Pronunciation"}: no score; no acoustic assessment or human calibration. WER is not a pronunciation score.`;
    $("feedback").append(p);
  }
  if (report.jobs.some((job) => ["queued", "running"].includes(job.status))) {
    timer = setTimeout(() => refresh().catch((error) => status(error.message, true)), 2000);
  } else if (!dirty) {
    const generation = editorGeneration;
    const value = await api(`/api/sessions/${sid}`);
    if (current?.id === sid && !dirty && editorGeneration === generation) showPractice(value, false);
  }
}
async function signedIn() {
  $("auth").hidden = true; $("workspace").hidden = false; $("password").value = "";
  await history(); status("Signed in. Start a practice after reading the local policy.");
}
$("auth-form").onsubmit = guard(async (event) => {
  const action = event.submitter.value;
  const payload = {email: $("email").value, password: $("password").value};
  if (action === "register") { await api("/api/auth/register", "POST", payload); status("Account created. Log in to continue."); return; }
  csrf = (await api("/api/auth/login", "POST", payload)).csrf_token;
  await signedIn();
});
$("mode").onchange = () => { const reference = $("mode").value === "reference_reading"; $("reference-area").hidden = !reference; $("reference").required = reference; };
$("practice-form").onsubmit = guard(async () => {
  const consent = await api("/api/consents", "POST", {policy_version: "local-v1", allow_server_processing: $("consent").checked, allow_anonymized_retention: $("retain").checked});
  const payload = {consent_id: consent.id, mode: $("mode").value};
  if (payload.mode === "reference_reading") payload.reference_text = $("reference").value;
  showPractice(await api("/api/sessions", "POST", payload)); $("audio").value = "";
  await history(); await refresh(); status("Practice created. Type your own text or upload a supported WAV.");
});
$("transcript").oninput = () => { editorGeneration++; dirty = true; updateButtons(); };
$("save").onclick = guard(async () => {
  const sid = current.id, generation = editorGeneration;
  const value = await api(`/api/sessions/${sid}/transcript`, "PUT", {text: $("transcript").value, expected_revision: current.transcript_revision});
  applyEditorResponse(value, sid, generation);
  status(dirty ? "Submitted draft saved. Newer edits remain unsaved." : "Draft saved. Confirm this revision before grammar analysis."); await refresh();
});
$("confirm").onclick = guard(async () => {
  const sid = current.id, generation = editorGeneration;
  const value = await api(`/api/sessions/${sid}/confirm`, "POST", {expected_revision: current.transcript_revision});
  applyEditorResponse(value, sid, generation);
  status(dirty ? "Saved revision confirmed; newer edits still need saving and confirmation." : `Saved revision confirmed. You may now queue ${real() ? "" : "FAKE "}grammar.`);
});
$("analyze").onclick = guard(async () => {
  await api(`/api/sessions/${current.id}/jobs/grammar`, "POST", {expected_revision: current.transcript_revision, idempotency_key: crypto.randomUUID()});
  status(`${real() ? "Grammar (real model, unevaluated)" : "FAKE grammar"} queued. The separate worker processes it.`); await refresh();
});
$("reload").onclick = guard(async () => {
  if (dirty && !window.confirm("Discard unsaved changes?")) return;
  const sid = current.id, generation = editorGeneration;
  applyEditorResponse(await api(`/api/sessions/${sid}`), sid, generation);
  status(dirty ? "Newer edits preserved; saved revision loaded." : "Saved draft reloaded.");
});
$("upload-form").onsubmit = guard(async () => {
  const file = $("audio").files[0];
  if (!file) throw new Error("Choose a WAV file first.");
  await uploadAudio(file);
});
async function uploadAudio(file, sid = current.id) {
  if (file.size > 10485760) throw new Error("Maximum upload size is 10 MiB.");
  await api(`/api/sessions/${sid}/audio`, "POST", file, {"Content-Type": "audio/wav", "Idempotency-Key": crypto.randomUUID()});
  status(real() ? "Audio accepted. ASR (real model, unevaluated) queued; check the draft before confirming." : "Audio accepted. FAKE ASR queued; check the canned draft before confirming."); await refresh();
}
// Web Audio capture: MediaRecorder would yield WebM/Ogg, but the server accepts only WAV.
const WAV_RATE = 16000, WAV_MAX_FRAMES = 480000;
function encodeWav(samples) {
  const n = Math.min(samples.length, WAV_MAX_FRAMES), out = new Uint8Array(44 + 2 * n), v = new DataView(out.buffer);
  const tag = (at, text) => { for (let i = 0; i < 4; i++) out[at + i] = text.charCodeAt(i); };
  tag(0, "RIFF"); v.setUint32(4, 36 + 2 * n, true); tag(8, "WAVE"); tag(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, WAV_RATE, true); v.setUint32(28, WAV_RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  tag(36, "data"); v.setUint32(40, 2 * n, true);
  for (let i = 0; i < n; i++) { const s = Math.max(-1, Math.min(1, samples[i])); v.setInt16(44 + 2 * i, s < 0 ? s * 32768 : s * 32767, true); }
  return out;
}
let rec = null, starting = false;
function recordingUi(on) { $("record").disabled = on; $("stop-record").hidden = !on; }
function abortRecording() {
  const r = rec; if (!r) return;
  rec = null; r.node.onaudioprocess = null; r.stream.getTracks().forEach((t) => t.stop());
  r.context.close().catch(() => {}); recordingUi(false);
}
$("record").onclick = guard(async () => {
  if (rec || starting) return;
  if (!current) throw new Error("Create a practice first.");
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone unavailable: use localhost or HTTPS.");
  starting = true; updateButtons();
  try {
    const sid = current.id;
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
    catch { throw new Error("Microphone permission denied or no microphone found."); }
    const chunks = [];
    let context, node;
    try {
      // the practice may be gone (logout, delete, 401) while awaiting: release the mic instead of recording
      if (!current || current.id !== sid) throw new Error("Recording cancelled.");
      context = new AudioContext();
      await context.resume();
      if (!current || current.id !== sid) throw new Error("Recording cancelled.");
      // shortcut: ScriptProcessorNode is deprecated; move to AudioWorklet (static file, CSP-safe) if a browser drops it
      node = context.createScriptProcessor(4096, 1, 1);
    } catch (error) {
      stream.getTracks().forEach((t) => t.stop()); if (context) context.close().catch(() => {}); throw error;
    }
    const mine = rec = {stream, context, node, chunks, frames: 0, sid};
    node.onaudioprocess = (event) => {
      chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
      mine.frames += 4096;
      if (mine.frames >= 30 * context.sampleRate) $("stop-record").click();
    };
    context.createMediaStreamSource(stream).connect(node); node.connect(context.destination);
    recordingUi(true); status("Recording… press Stop (max 30 s).");
  } finally { starting = false; updateButtons(); }
});
$("stop-record").onclick = guard(async () => {
  const r = rec; rec = null;
  if (!r) return;
  r.node.onaudioprocess = null; r.stream.getTracks().forEach((t) => t.stop());
  const rate = r.context.sampleRate; await r.context.close(); recordingUi(false);
  const length = Math.min(r.chunks.reduce((a, c) => a + c.length, 0), 30 * rate);
  if (!length) throw new Error("Nothing was recorded.");
  const input = new Float32Array(length); let at = 0;
  for (const c of r.chunks) { if (at >= length) break; input.set(c.subarray(0, length - at), at); at += c.length; }
  const frames = Math.ceil(length * WAV_RATE / rate), offline = new OfflineAudioContext(1, frames, WAV_RATE);
  const buffer = offline.createBuffer(1, length, rate); buffer.copyToChannel(input, 0);
  const source = offline.createBufferSource(); source.buffer = buffer; source.connect(offline.destination); source.start();
  const rendered = await offline.startRendering();
  await uploadAudio(new Blob([encodeWav(rendered.getChannelData(0))], {type: "audio/wav"}), r.sid);
});
$("refresh").onclick = guard(refresh); $("history-refresh").onclick = guard(history);
$("export").onclick = guard(async () => {
  const data = await api(`/api/sessions/${current.id}/export`);
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:"application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = "fake-technical-summary.json"; a.click(); URL.revokeObjectURL(url);
});
$("delete-session").onclick = guard(async () => {
  if (!window.confirm("Delete this practice, its audio, text and results? This cannot be undone.")) return;
  const result = await api(`/api/sessions/${current.id}`, "DELETE"); clearTimeout(timer); current = null; abortRecording();
  $("editor").hidden = $("results").hidden = true; await history(); status(result.status === "deleted" ? "Practice deleted." : "Deletion pending. Cleanup will retry after the worker stops.");
});
$("withdraw-consent").onclick = guard(async () => {
  if (!window.confirm("Withdraw consent and delete every practice that used it? This cannot be undone.")) return;
  const result = await api(`/api/consents/${current.consent_id}/withdraw`, "POST"); clearTimeout(timer); current = null; abortRecording();
  $("editor").hidden = $("results").hidden = true; await history(); status(result.status === "withdrawn" ? "Consent withdrawn; practices deleted." : "Consent withdrawn. Deletion pending; cleanup will retry after the worker stops.");
});
$("logout").onclick = guard(async () => { await api("/api/auth/logout", "POST"); loggedOut(); csrf = (await api("/api/auth/csrf")).csrf_token; status("Logged out."); });
$("delete-account").onclick = guard(async () => {
  if (!window.confirm("Permanently delete this account and all its practices?")) return;
  const result = await api("/api/account", "DELETE"); loggedOut(); csrf = (await api("/api/auth/csrf")).csrf_token; status(`Account ${result.status}. Any operator-created backups expire separately.`);
});
(async () => {
  const boot = await api("/api/auth/csrf"); csrf = boot.csrf_token; backend = boot.backend || "fake";
  if (real()) {
    $("banner-title").textContent = "REAL ASR/GRAMMAR (UNEVALUATED); PRONUNCIATION FAKE";
    $("banner-text").textContent = "ASR and grammar come from local models and may be wrong; quality is not evaluated. Pronunciation is a FAKE test double: not assessed, no score.";
  }
  try { await api("/api/me"); await signedIn(); } catch { status(real() ? "Log in or create a local account. Real ASR/grammar models (unevaluated); pronunciation is FAKE, no score." : "Log in or create a local account. No real models are connected."); }
})().catch((error) => status(error.message, true));
