"use strict";
const $ = (id) => document.getElementById(id);
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
  $("analyze").disabled = !current || dirty || !current.confirmed_transcript;
  $("confirm").disabled = !current || dirty || !current.draft_transcript;
}
function loggedOut() {
  editorGeneration++;
  clearTimeout(timer); current = null;
  $("workspace").hidden = true; $("auth").hidden = false;
  $("editor").hidden = $("results").hidden = true;
  $("transcript").value = ""; $("feedback").replaceChildren(); $("history-list").replaceChildren();
  $("email").focus();
}
function showPractice(value, focus = true) {
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
  $("report-state").textContent = report.report_complete ? "Current FAKE results available. Not an assessment." : "Report incomplete: confirm your current words and queue grammar; see requested stages below.";
  $("job-list").replaceChildren(); $("feedback").replaceChildren();
  for (const job of report.jobs) {
    const p = document.createElement("p"); p.className = "job";
    p.textContent = `${job.kind}: ${job.status}${job.error_code ? " / " + job.error_code : ""} · ${Object.entries(job.stages).map(([name,state]) => `${name}: ${state}`).join("; ")}`;
    $("job-list").append(p);
  }
  for (const result of report.grammar) {
    const box = document.createElement("div"), title = document.createElement("h3"), original = document.createElement("p"), corrected = document.createElement("p"), warning = document.createElement("p");
    box.className = "result"; title.textContent = `FAKE grammar · revision ${result.revision}${result.is_current ? " (current)" : " (not current)"}`;
    original.textContent = `Confirmed original: ${result.source_text}`;
    corrected.textContent = `Canned suggestion: ${result.corrected_text}`;
    warning.textContent = result.warnings.join(" "); warning.className = "hint";
    box.append(title, original, corrected, warning); $("feedback").append(box);
  }
  for (const result of report.pronunciation) {
    const p = document.createElement("p"); p.className = "result";
    p.textContent = "FAKE pronunciation: no score; no acoustic assessment or human calibration. WER is not a pronunciation score.";
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
  status(dirty ? "Saved revision confirmed; newer edits still need saving and confirmation." : "Saved revision confirmed. You may now queue FAKE grammar.");
});
$("analyze").onclick = guard(async () => {
  await api(`/api/sessions/${current.id}/jobs/grammar`, "POST", {expected_revision: current.transcript_revision, idempotency_key: crypto.randomUUID()});
  status("FAKE grammar queued. The separate worker processes it."); await refresh();
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
  if (file.size > 10485760) throw new Error("Maximum upload size is 10 MiB.");
  await api(`/api/sessions/${current.id}/audio`, "POST", file, {"Content-Type": "audio/wav", "Idempotency-Key": crypto.randomUUID()});
  status("Audio accepted. FAKE ASR queued; check the canned draft before confirming."); await refresh();
});
$("refresh").onclick = guard(refresh); $("history-refresh").onclick = guard(history);
$("export").onclick = guard(async () => {
  const data = await api(`/api/sessions/${current.id}/export`);
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:"application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = "fake-technical-summary.json"; a.click(); URL.revokeObjectURL(url);
});
$("delete-session").onclick = guard(async () => {
  if (!window.confirm("Delete this practice, its audio, text and results? This cannot be undone.")) return;
  const result = await api(`/api/sessions/${current.id}`, "DELETE"); clearTimeout(timer); current = null;
  $("editor").hidden = $("results").hidden = true; await history(); status(result.status === "deleted" ? "Practice deleted." : "Deletion pending. Cleanup will retry after the worker stops.");
});
$("logout").onclick = guard(async () => { await api("/api/auth/logout", "POST"); loggedOut(); csrf = (await api("/api/auth/csrf")).csrf_token; status("Logged out."); });
$("delete-account").onclick = guard(async () => {
  if (!window.confirm("Permanently delete this account and all its practices?")) return;
  const result = await api("/api/account", "DELETE"); loggedOut(); csrf = (await api("/api/auth/csrf")).csrf_token; status(`Account ${result.status}. Any operator-created backups expire separately.`);
});
(async () => { csrf = (await api("/api/auth/csrf")).csrf_token; try { await api("/api/me"); await signedIn(); } catch { status("Log in or create a local account. No real models are connected."); } })().catch((error) => status(error.message, true));
