"use strict";
/* Healthcare Appointment Manager — client.
 * Server-rendered shells (Jinja) enforce the role gate; this script drives the
 * dynamic flows against the JSON API using the session cookie and echoing the
 * CSRF token from the (non-httpOnly) hcv_csrf cookie in the X-CSRF-Token header.
 */

// ---------------------------------------------------------------- helpers
function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "k-" + Date.now() + "-" + Math.floor(Math.random() * 1e9);
}

async function api(method, url, opts = {}) {
  const headers = {};
  let body;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRF-Token"] = getCookie("hcv_csrf");
  }
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let res;
  try {
    res = await fetch(url, { method, headers, body, credentials: "same-origin" });
  } catch (e) {
    return { ok: false, status: 0, data: { detail: "Network error — is the server running?" } };
  }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch (_) { data = { detail: text }; } }
  return { ok: res.ok, status: res.status, data };
}

function toast(msg, kind = "") {
  const t = document.getElementById("toast");
  if (!t) { alert(msg); return; }
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 4200);
}

function errText(r) {
  const d = r.data && r.data.detail;
  if (Array.isArray(d)) return d.map(x => x.msg || JSON.stringify(x)).join("; ");
  return d || ("Request failed (" + r.status + ")");
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ---------------------------------------------------------------- Google Calendar (patient + doctor)
async function initGoogle() {
  const box = document.getElementById("google-status");
  if (!box) return;
  const r = await api("GET", "/api/integrations/google/status");
  if (!r.ok) { box.textContent = "unavailable"; return; }
  const { configured, connected } = r.data;
  if (!configured) { box.innerHTML = '<span class="muted">Not configured on this server</span>'; return; }
  if (connected) {
    box.innerHTML = '<span class="badge confirmed">Connected</span> <button class="btn-ghost" id="g-disconnect" type="button">Disconnect</button>';
    document.getElementById("g-disconnect").onclick = async () => {
      await api("POST", "/api/integrations/google/disconnect");
      toast("Google Calendar disconnected");
      initGoogle();
    };
  } else {
    box.innerHTML = '<a class="btn" href="/api/integrations/google/authorize">Connect Google Calendar</a>';
  }
}

function flashQueryStatus() {
  const p = new URLSearchParams(location.search);
  if (p.get("google") === "connected") toast("Google Calendar connected", "success");
  else if (p.get("google") === "error") toast("Could not connect Google Calendar", "error");
  if (p.has("google")) history.replaceState({}, "", location.pathname);
}

// ---------------------------------------------------------------- LOGIN
function initLogin() {
  const card = document.querySelector(".auth-card");
  const initial = (card && card.dataset.initial) || "login";
  const tabs = document.querySelectorAll(".tab");
  const panels = document.querySelectorAll(".tab-panel");
  function show(name) {
    tabs.forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    panels.forEach(p => p.classList.toggle("hidden", p.dataset.panel !== name));
  }
  tabs.forEach(t => (t.onclick = () => show(t.dataset.tab)));
  show(initial);
  document.querySelectorAll(".demo-row").forEach(r => r.onclick = () => {
    show("login");
    const form = document.querySelector('.tab-panel[data-panel="login"]');
    if(form){ form.querySelector('input[name="email"]').value = r.dataset.email; form.querySelector('input[name="password"]').value = r.dataset.pass; form.querySelector('input[name="email"]').focus(); }
    toast(`Filled ${r.dataset.email}`, "success");
  });
}

// ---------------------------------------------------------------- PATIENT dashboard
function doctorCard(d) {
  return `<div class="item">
    <div class="row space-between">
      <div><h4>${esc(d.full_name)}</h4><div class="meta">${esc(d.specialisation)}</div></div>
      <a class="btn" href="/patient/book/${d.id}">Book</a>
    </div>
    ${d.bio ? `<div class="body muted">${esc(d.bio)}</div>` : ""}
  </div>`;
}

async function searchDoctors() {
  const q = document.getElementById("doc-search").value.trim();
  const list = document.getElementById("doctor-list");
  list.innerHTML = '<p class="muted">Searching…</p>';
  const r = await api("GET", "/api/doctors" + (q ? "?specialisation=" + encodeURIComponent(q) : ""));
  if (!r.ok) { list.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  if (!r.data.length) { list.innerHTML = '<p class="muted">No doctors found.</p>'; return; }
  list.innerHTML = r.data.map(doctorCard).join("");
}

function apptCard(a) {
  const hasDetails = !!(a.previsit || a.postvisit || (a.prescriptions||[]).length || ["holding","confirmed"].includes(a.status));
  const toggleBtn = hasDetails ? `<button class="btn-ghost appt-detail-toggle" data-id="${a.id}" type="button">Show more</button>` : ``;
  const parts = [];
  parts.push(`<div class="row space-between">
      <div><h4>${esc(a.doctor_name || "Doctor")}</h4>
      <div class="meta">${esc(a.specialisation || "")} · ${fmtDateTime(a.scheduled_start)}</div></div>
      <div class="row" style="gap:6px"><span class="badge ${esc(a.status)}">${esc(a.status)}</span>${toggleBtn}</div>
    </div>`);
  // collapsed details
  parts.push(`<div class="appt-details hidden" id="appt-detail-${a.id}" style="margin-top:10px">`);
  if (a.previsit) {
    parts.push(`<div class="body"><span class="urgency ${esc(a.previsit.urgency_level)}">Urgency: ${esc(a.previsit.urgency_level)}</span>
      <div class="muted" style="margin-top:6px">${esc(a.previsit.chief_complaint || "")}</div></div>`);
  }
  if (a.postvisit) {
    const txt = a.postvisit.summary_text || "";
    const short = txt.length > 180 ? `<span class="post-short">${esc(txt.slice(0,180))}… <a href="#" class="post-more" data-id="${a.id}">Read more</a></span><span class="post-full hidden">${esc(txt)} <a href="#" class="post-less" data-id="${a.id}">Show less</a></span>` : esc(txt);
    parts.push(`<div class="body"><strong>Visit summary</strong><p>${short}</p>`);
    if ((a.postvisit.medication_schedule || []).length)
      parts.push(`<strong>Medication schedule</strong><ul class="tight">${a.postvisit.medication_schedule.map(m => `<li>${esc(m)}</li>`).join("")}</ul>`);
    if ((a.postvisit.follow_up_steps || []).length)
      parts.push(`<strong>Follow-up</strong><ul class="tight">${a.postvisit.follow_up_steps.map(m => `<li>${esc(m)}</li>`).join("")}</ul>`);
    parts.push(`</div>`);
  }
  if ((a.prescriptions || []).length) {
    parts.push(`<div class="body"><strong>Prescriptions</strong>`);
    a.prescriptions.forEach(p => {
      const rem = (p.reminders || []).length
        ? `<div class="muted" style="margin-top:4px">Reminders: ${p.reminders.slice(0,3).map(r => `${fmtDateTime(r.scheduled_at)} <span class="badge ${esc(r.status)}">${esc(r.status)}</span>`).join(", ")}${p.reminders.length>3?` <span class="muted">+${p.reminders.length-3} more</span>`:""}</div>`
        : (p.times_per_day ? `<div class="muted">Frequency: ${esc(p.frequency || "")} (${p.times_per_day}×/day, ${p.duration_days} days)</div>` : `<div class="muted">PRN — as needed (no scheduled reminders)</div>`);
      parts.push(`<div style="margin-top:8px"><strong>${esc(p.medication_name)}</strong> ${p.dosage ? "("+esc(p.dosage)+")" : ""}${rem}</div>`);
    });
    parts.push(`</div>`);
  }
  if (["holding", "confirmed"].includes(a.status)) {
    parts.push(`<div style="margin-top:10px" class="row">
      <button class="btn-ghost btn-danger cancel-appt" data-id="${a.id}" type="button">Cancel</button>
      <button class="btn-ghost reschedule-toggle" data-id="${a.id}" data-doctor="${a.doctor_id}" type="button">Reschedule</button>
    </div>
    <div class="reschedule-panel hidden" id="resched-${a.id}" style="margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:8px">
      <div class="row"><input type="date" class="resched-date" value="${new Date(Date.now()+86400000).toISOString().slice(0,10)}"><button class="btn resched-fetch" data-id="${a.id}" data-doctor="${a.doctor_id}" type="button">Fetch slots</button></div>
      <div class="resched-slots list" style="margin-top:10px"></div>
    </div>`);
  }
  parts.push(`</div>`);
  return `<div class="item">${parts.join("")}</div>`;
}

async function loadAppointments() {
  const list = document.getElementById("appt-list");
  list.innerHTML = '<p class="muted">Loading…</p>';
  const r = await api("GET", "/api/appointments/mine");
  if (!r.ok) { list.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  if (!r.data.length) { list.innerHTML = '<p class="muted">No appointments yet. Find a doctor to get started.</p>'; return; }
  list.innerHTML = r.data.map(apptCard).join("");
  list.querySelectorAll(".cancel-appt").forEach(b => (b.onclick = async () => {
    if (!confirm("Cancel this appointment?")) return;
    const rr = await api("POST", `/api/appointments/${b.dataset.id}/cancel`, { idempotencyKey: uuid() });
    if (!rr.ok) return toast(errText(rr), "error");
    toast("Appointment cancelled", "success");
    loadAppointments();
  }));
  list.querySelectorAll(".appt-detail-toggle").forEach(b => (b.onclick = () => {
    const panel = document.getElementById("appt-detail-" + b.dataset.id);
    if (!panel) return;
    const isHidden = panel.classList.toggle("hidden");
    b.textContent = isHidden ? "Show more" : "Show less";
  }));
  list.querySelectorAll(".post-more").forEach(a => (a.onclick = (e) => {
    e.preventDefault();
    const card = a.closest(".body");
    if (!card) return;
    card.querySelector(".post-short").classList.add("hidden");
    card.querySelector(".post-full").classList.remove("hidden");
  }));
  list.querySelectorAll(".post-less").forEach(a => (a.onclick = (e) => {
    e.preventDefault();
    const card = a.closest(".body");
    if (!card) return;
    card.querySelector(".post-full").classList.add("hidden");
    card.querySelector(".post-short").classList.remove("hidden");
  }));
  list.querySelectorAll(".reschedule-toggle").forEach(b => (b.onclick = () => {
    const panel = document.getElementById("resched-" + b.dataset.id);
    if (panel) panel.classList.toggle("hidden");
  }));
  list.querySelectorAll(".resched-fetch").forEach(b => (b.onclick = async () => {
    const panel = document.getElementById("resched-" + b.dataset.id);
    const dateInput = panel.querySelector(".resched-date");
    const slotsBox = panel.querySelector(".resched-slots");
    const date = dateInput.value;
    if (!date) return toast("Pick a date", "error");
    slotsBox.innerHTML = '<p class="muted">Loading slots…</p>';
    const rr = await api("GET", `/api/doctors/${b.dataset.doctor}/slots?date=${date}`);
    if (!rr.ok) { slotsBox.innerHTML = `<p class="alert error">${esc(errText(rr))}</p>`; return; }
    if (!rr.data.length) { slotsBox.innerHTML = '<p class="muted">No free slots on this date.</p>'; return; }
    slotsBox.innerHTML = "";
    rr.data.forEach(s => {
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "slot"; btn.textContent = fmtTime(s.start_time);
      btn.onclick = async () => {
        if (!confirm(`Reschedule to ${fmtDateTime(s.start_time)}?`)) return;
        btn.disabled = true;
        const r2 = await api("POST", `/api/appointments/${b.dataset.id}/reschedule`, { body: { new_slot_id: s.id }, idempotencyKey: uuid() });
        if (!r2.ok) { toast(errText(r2), "error"); btn.disabled = false; return; }
        toast("Appointment rescheduled", "success");
        loadAppointments();
      };
      slotsBox.appendChild(btn);
    });
  }));
}

function initPatient() {
  document.getElementById("doc-search-btn").onclick = searchDoctors;
  document.getElementById("doc-search").addEventListener("keydown", e => { if (e.key === "Enter") searchDoctors(); });
  document.getElementById("refresh-appts").onclick = loadAppointments;
  searchDoctors();
  loadAppointments();
  flashQueryStatus();
  initGoogle();
}

// ---------------------------------------------------------------- BOOK flow
const bookState = { doctorId: null, appointmentId: null, confirmKey: null, selectedSlot: null, holdExpiresAt: null };
let holdTimer = null;
function clearHoldTimer(){ if(holdTimer){ clearInterval(holdTimer); holdTimer=null; } }
function fmtMMSS(ms){ if(ms<=0) return "00:00"; const s=Math.floor(ms/1000); const m=Math.floor(s/60); const sec=s%60; return String(m).padStart(2,"0")+":"+String(sec).padStart(2,"0"); }
function startHoldTimer(expiresIso){
  clearHoldTimer();
  if(!expiresIso) return;
  const expires = new Date(expiresIso).getTime();
  const el1 = document.getElementById("hold-timer");
  const el2 = document.getElementById("hold-timer-previsit");
  function tick(){
    const rem = expires - Date.now();
    const txt = rem>0 ? `Hold: ${fmtMMSS(rem)} left` : "Hold expired — please pick a slot again";
    if(el1) { el1.textContent = txt; el1.className = rem>60000 ? "alert" : rem>0 ? "alert error" : "alert error"; }
    if(el2) { el2.textContent = txt; el2.className = rem>60000 ? "alert" : rem>0 ? "alert error" : "alert error"; }
    if(rem<=0){
      clearHoldTimer();
      document.getElementById("confirm-btn").disabled = true;
      document.getElementById("submit-symptoms").disabled = true;
      toast("Hold expired — pick another slot", "error");
      setTimeout(()=>{ document.getElementById("step-symptoms").classList.add("hidden"); document.getElementById("step-previsit").classList.add("hidden"); document.getElementById("step-idle").classList.remove("hidden"); loadSlots(); }, 1200);
    }
  }
  tick();
  holdTimer = setInterval(tick, 1000);
}

async function loadDoctorHeader() {
  const r = await api("GET", "/api/doctors/" + bookState.doctorId);
  if (r.ok) {
    document.getElementById("doctor-name").textContent = r.data.full_name;
    document.getElementById("doctor-meta").textContent =
      r.data.specialisation + " · " + r.data.slot_duration_min + " min slots";
  }
}

async function loadSlots() {
  const date = document.getElementById("slot-date").value;
  const list = document.getElementById("slot-list");
  const empty = document.getElementById("slot-empty");
  list.innerHTML = '<p class="muted">Loading…</p>';
  empty.classList.add("hidden");
  const r = await api("GET", `/api/doctors/${bookState.doctorId}/slots?date=${date}`);
  if (!r.ok) { list.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  list.innerHTML = "";
  if (!r.data.length) { empty.classList.remove("hidden"); return; }
  r.data.forEach(s => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "slot"; b.textContent = fmtTime(s.start_time);
    b.onclick = () => holdSlot(s, b);
    list.appendChild(b);
  });
}

async function holdSlot(slot, btn) {
  document.querySelectorAll(".slot").forEach(s => s.classList.remove("selected"));
  btn.classList.add("selected");
  const r = await api("POST", "/api/appointments/hold", { body: { slot_id: slot.id }, idempotencyKey: uuid() });
  if (!r.ok) {
    toast(errText(r), "error");
    loadSlots();  // refresh — someone likely took it
    return;
  }
  bookState.appointmentId = r.data.id;
  bookState.confirmKey = uuid();
  bookState.holdExpiresAt = r.data.hold_expires_at || null;
  document.getElementById("step-idle").classList.add("hidden");
  document.getElementById("step-previsit").classList.add("hidden");
  document.getElementById("step-done").classList.add("hidden");
  document.getElementById("step-symptoms").classList.remove("hidden");
  document.getElementById("hold-note").textContent =
    "Slot held for you at " + fmtTime(slot.start_time) + ". Complete the form to confirm.";
  document.getElementById("symptoms").focus();
  document.getElementById("submit-symptoms").disabled = false;
  document.getElementById("confirm-btn").disabled = false;
  startHoldTimer(bookState.holdExpiresAt);
}

async function submitSymptoms() {
  const val = document.getElementById("symptoms").value.trim();
  if (!val) return toast("Please describe your symptoms", "error");
  const btn = document.getElementById("submit-symptoms");
  btn.disabled = true; btn.textContent = "Analysing…";
  const r = await api("POST", `/api/appointments/${bookState.appointmentId}/symptoms`, { body: { symptoms: val } });
  btn.disabled = false; btn.textContent = "Analyse & preview";
  if (!r.ok) return toast(errText(r), "error");
  const pv = r.data.previsit;
  document.getElementById("previsit-source").textContent = r.data.source === "ok" ? "AI" : "offline fallback";
  document.getElementById("previsit-body").innerHTML = `
    <p><span class="urgency ${esc(pv.urgency_level)}">Urgency: ${esc(pv.urgency_level)}</span></p>
    <p><strong>Chief complaint:</strong> ${esc(pv.chief_complaint)}</p>
    <strong>Questions to ask your doctor</strong>
    <ul class="tight">${pv.suggested_questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul>`;
  document.getElementById("step-previsit").classList.remove("hidden");
}

async function confirmAppointment() {
  const btn = document.getElementById("confirm-btn");
  btn.disabled = true; btn.textContent = "Confirming…";
  const r = await api("POST", `/api/appointments/${bookState.appointmentId}/confirm`, { idempotencyKey: bookState.confirmKey });
  btn.disabled = false; btn.textContent = "Confirm appointment";
  if (!r.ok) {
    toast(errText(r), "error");
    if (r.status === 409) {  // hold expired / slot lost — restart
      clearHoldTimer();
      document.getElementById("step-symptoms").classList.add("hidden");
      document.getElementById("step-previsit").classList.add("hidden");
      document.getElementById("step-idle").classList.remove("hidden");
      loadSlots();
    }
    return;
  }
  clearHoldTimer();
  document.getElementById("step-symptoms").classList.add("hidden");
  document.getElementById("step-previsit").classList.add("hidden");
  document.getElementById("step-done").classList.remove("hidden");
}

function initBook() {
  bookState.doctorId = document.body.dataset.doctorId;
  const dateEl = document.getElementById("slot-date");
  const today = new Date();
  dateEl.value = today.toISOString().slice(0, 10);
  dateEl.min = today.toISOString().slice(0, 10);
  dateEl.onchange = loadSlots;
  document.getElementById("submit-symptoms").onclick = submitSymptoms;
  document.getElementById("confirm-btn").onclick = confirmAppointment;
  loadDoctorHeader();
  loadSlots();
}

// ---------------------------------------------------------------- DOCTOR schedule
function prescRowHtml() {
  return `<div class="presc-row">
    <label>Medication<input name="medication_name" placeholder="e.g. Amoxicillin"></label>
    <label>Dosage<input name="dosage" placeholder="500 mg"></label>
    <label>Frequency<input name="frequency" placeholder="twice daily / 1-0-1"></label>
    <label>Days<input type="number" name="duration_days" value="5" min="1" max="365"></label>
    <button class="btn-ghost remove-presc" type="button">&times;</button>
  </div>`;
}

function scheduleCard(a) {
  // NOTE: /api/visits/schedule returns the primary key as `appointment_id`
  // (not `id`, which is what /api/appointments/mine uses). Reading `a.id` here
  // yields undefined and posts to /appointments/undefined/complete.
  const apptId = a.appointment_id;
  const previsit = a.urgency
    ? `<span class="urgency ${esc(a.urgency)}">Urgency: ${esc(a.urgency)}</span>`
    : `<span class="tag">no pre-visit summary</span>`;
  const questions = (a.suggested_questions || []).length
    ? `<strong>Suggested questions</strong><ul class="tight">${a.suggested_questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul>` : "";
  const completeBtn = a.status === "confirmed"
    ? `<button class="btn complete-toggle" data-id="${apptId}" type="button">Complete visit</button>` : "";
  return `<div class="item" data-appt="${apptId}">
    <div class="row space-between">
      <div><h4>${esc(a.patient_name)}</h4><div class="meta">${fmtDateTime(a.scheduled_start)}</div></div>
      <div>${previsit} <span class="badge ${esc(a.status)}">${esc(a.status)}</span></div>
    </div>
    <div class="body">
      ${a.chief_complaint ? `<p><strong>Chief complaint:</strong> ${esc(a.chief_complaint)}</p>` : ""}
      ${a.symptoms ? `<p class="muted">${esc(a.symptoms)}</p>` : ""}
      ${questions}
    </div>
    <div style="margin-top:10px">${completeBtn}</div>
    <div class="complete-panel hidden"></div>
  </div>`;
}

function completeForm(id) {
  return `<hr style="border:none;border-top:1px solid var(--line);margin:12px 0">
    <label>Doctor notes<textarea name="doctor_notes" rows="4" placeholder="Assessment, diagnosis, advice…"></textarea></label>
    <h4>Prescriptions</h4>
    <div class="presc-rows">${prescRowHtml()}</div>
    <button class="btn-ghost add-presc" type="button">+ Add medication</button>
    <div style="margin-top:12px"><button class="btn submit-complete" data-id="${id}" type="button">Save &amp; generate summary</button></div>
    <div class="complete-result"></div>`;
}

async function submitComplete(panel, id) {
  // Guard: a missing/renamed API key would send "undefined" into an int path
  // param and surface as an opaque 422 from the server. Fail clearly instead.
  if (!/^\d+$/.test(String(id))) {
    return toast("Could not identify the appointment — please refresh the schedule.", "error");
  }
  const notes = panel.querySelector('[name="doctor_notes"]').value.trim();
  if (!notes) return toast("Doctor notes are required", "error");
  const prescriptions = [];
  panel.querySelectorAll(".presc-row").forEach(row => {
    const name = row.querySelector('[name="medication_name"]').value.trim();
    if (!name) return;
    prescriptions.push({
      medication_name: name,
      dosage: row.querySelector('[name="dosage"]').value.trim() || null,
      frequency: row.querySelector('[name="frequency"]').value.trim() || null,
      duration_days: parseInt(row.querySelector('[name="duration_days"]').value, 10) || 1,
    });
  });
  const btn = panel.querySelector(".submit-complete");
  btn.disabled = true; btn.textContent = "Saving…";
  const r = await api("POST", `/api/visits/appointments/${id}/complete`, { body: { doctor_notes: notes, prescriptions } });
  btn.disabled = false; btn.textContent = "Save & generate summary";
  if (!r.ok) return toast(errText(r), "error");
  const pv = r.data.postvisit;
  const reminders = r.data.prescriptions.reduce((n, p) => n + (p.reminders_scheduled || 0), 0);
  panel.querySelector(".complete-result").innerHTML = `
    <div class="alert success">Visit completed (${r.data.source === "ok" ? "AI summary" : "offline fallback"}).
      ${reminders} medication reminder(s) scheduled. Patient notified.</div>
    <strong>Patient-friendly summary</strong><p>${esc(pv.summary_text)}</p>`;
  panel.querySelector(".submit-complete").disabled = true;
  toast("Visit completed", "success");
}

async function loadSchedule() {
  const box = document.getElementById("schedule");
  box.innerHTML = '<p class="muted">Loading…</p>';
  const r = await api("GET", "/api/visits/schedule");
  if (!r.ok) { box.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  if (!r.data.length) { box.innerHTML = '<p class="muted">No holding or confirmed visits.</p>'; return; }
  box.innerHTML = r.data.map(scheduleCard).join("");
  box.querySelectorAll(".complete-toggle").forEach(btn => (btn.onclick = () => {
    const item = btn.closest(".item");
    const panel = item.querySelector(".complete-panel");
    if (!panel.dataset.built) { panel.innerHTML = completeForm(btn.dataset.id); panel.dataset.built = "1"; wireCompletePanel(panel, btn.dataset.id); }
    panel.classList.toggle("hidden");
  }));
}

function wireCompletePanel(panel, id) {
  panel.querySelector(".add-presc").onclick = () => {
    const wrap = document.createElement("div");
    wrap.innerHTML = prescRowHtml();
    const row = wrap.firstElementChild;
    panel.querySelector(".presc-rows").appendChild(row);
    row.querySelector(".remove-presc").onclick = () => row.remove();
  };
  panel.querySelectorAll(".remove-presc").forEach(b => (b.onclick = () => b.closest(".presc-row").remove()));
  panel.querySelector(".submit-complete").onclick = () => submitComplete(panel, id);
}

function initDoctor() {
  document.getElementById("refresh-schedule").onclick = loadSchedule;
  loadSchedule();
  flashQueryStatus();
  initGoogle();
}

// ---------------------------------------------------------------- ADMIN
function addWhRow() {
  const tpl = document.getElementById("wh-template");
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".remove-wh").onclick = () => node.remove();
  document.getElementById("wh-rows").appendChild(node);
}

function collectWorkingHours(scope) {
  const hours = [];
  scope.querySelectorAll(".wh-row").forEach(row => {
    hours.push({
      day_of_week: parseInt(row.querySelector('[name="day_of_week"]').value, 10),
      start_time: row.querySelector('[name="start_time"]').value,
      end_time: row.querySelector('[name="end_time"]').value,
    });
  });
  return hours;
}

async function createDoctor(e) {
  e.preventDefault();
  const f = e.target;
  const payload = {
    full_name: f.full_name.value.trim(),
    email: f.email.value.trim(),
    password: f.password.value,
    specialisation: f.specialisation.value.trim(),
    bio: f.bio.value.trim() || null,
    slot_duration_min: parseInt(f.slot_duration_min.value, 10) || 30,
    phone: f.phone.value.trim() || null,
    working_hours: collectWorkingHours(f),
  };
  const r = await api("POST", "/api/admin/doctors", { body: payload });
  if (!r.ok) return toast(errText(r), "error");
  toast("Doctor created", "success");
  f.reset();
  document.getElementById("wh-rows").innerHTML = "";
  addWhRow();
  loadAdminDoctors();
}

function dayName(n) { return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][n] || "?"; }

async function toggleDoctorDetail(item, id) {
  const panel = item.querySelector(".doc-detail");
  if (panel.dataset.built) { panel.classList.toggle("hidden"); return; }
  const r = await api("GET", "/api/admin/doctors/" + id);
  if (!r.ok) return toast(errText(r), "error");
  const d = r.data;
  const whHtml = d.working_hours.map(w => `<li>${dayName(w.day_of_week)} ${w.start_time.slice(0,5)}–${w.end_time.slice(0,5)}</li>`).join("") || "<li class='muted'>None set</li>";
  const leavesHtml = d.leaves.map(l => `<li>${esc(l.leave_date)} ${l.reason ? "· " + esc(l.reason) : ""}
      <button class="btn-ghost remove-leave" data-date="${esc(l.leave_date)}" type="button">remove</button></li>`).join("") || "<li class='muted'>None</li>";
  panel.innerHTML = `
    <hr style="border:none;border-top:1px solid var(--line);margin:12px 0">
    <strong>Working hours</strong><ul class="tight">${whHtml}</ul>
    <strong>Leave days</strong><ul class="tight leaves-list">${leavesHtml}</ul>
    <div class="row">
      <input type="date" class="leave-date">
      <input class="leave-reason" placeholder="Reason (optional)">
      <button class="btn add-leave" type="button">Add leave</button>
    </div>`;
  panel.dataset.built = "1";
  panel.querySelector(".add-leave").onclick = async () => {
    const date = panel.querySelector(".leave-date").value;
    if (!date) return toast("Pick a date", "error");
    const rr = await api("POST", `/api/admin/doctors/${id}/leave`,
      { body: { leave_date: date, reason: panel.querySelector(".leave-reason").value.trim() || null } });
    if (!rr.ok) return toast(errText(rr), "error");
    toast(`Leave added. ${rr.data.cancelled_appointments} appointment(s) cancelled & patients notified.`, "success");
    panel.dataset.built = ""; toggleDoctorDetail(item, id);
  };
  panel.querySelectorAll(".remove-leave").forEach(b => (b.onclick = async () => {
    const rr = await api("DELETE", `/api/admin/doctors/${id}/leave/${b.dataset.date}`);
    if (!rr.ok) return toast(errText(rr), "error");
    toast("Leave removed", "success");
    panel.dataset.built = ""; toggleDoctorDetail(item, id);
  }));
}

async function loadAdminDoctors() {
  const box = document.getElementById("doctor-admin-list");
  box.innerHTML = '<p class="muted">Loading…</p>';
  const r = await api("GET", "/api/admin/doctors");
  if (!r.ok) { box.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  if (!r.data.length) { box.innerHTML = '<p class="muted">No doctors yet.</p>'; return; }
  box.innerHTML = r.data.map(d => `<div class="item" data-id="${d.id}">
      <div class="row space-between">
        <div><h4>${esc(d.full_name)}</h4><div class="meta">${esc(d.specialisation)} · ${d.slot_duration_min} min</div></div>
        <div><span class="badge ${d.active ? "confirmed" : "cancelled"}">${d.active ? "active" : "inactive"}</span>
          <button class="btn-ghost detail-toggle" type="button">Manage</button></div>
      </div>
      <div class="doc-detail hidden"></div>
    </div>`).join("");
  box.querySelectorAll(".detail-toggle").forEach(b => (b.onclick = () => {
    const item = b.closest(".item");
    toggleDoctorDetail(item, item.dataset.id);
  }));
}

async function loadOutbox() {
  const status = document.getElementById("outbox-filter").value;
  const tbody = document.querySelector("#outbox-table tbody");
  tbody.innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
  const r = await api("GET", "/api/admin/outbox" + (status ? "?status=" + status : ""));
  if (!r.ok) { tbody.innerHTML = `<tr><td colspan="6" class="alert error">${esc(errText(r))}</td></tr>`; return; }
  if (!r.data.length) { tbody.innerHTML = '<tr><td colspan="6" class="muted">Empty.</td></tr>'; return; }
  tbody.innerHTML = r.data.map(o => `<tr>
      <td>${esc(o.to_email)}</td><td>${esc(o.subject)}</td><td>${esc(o.kind)}</td>
      <td><span class="badge ${esc(o.status)}">${esc(o.status)}</span></td>
      <td>${o.attempts}/${o.max_attempts}</td><td class="muted">${esc(o.last_error || "")}</td>
    </tr>`).join("");
}

async function loadAdminStats() {
  const box = document.getElementById("admin-stats");
  const recent = document.getElementById("admin-recent");
  box.innerHTML = '<p class="muted">Loading…</p>';
  const r = await api("GET", "/api/admin/stats");
  if (!r.ok) { box.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  const d = r.data;
  box.innerHTML = `
    <div class="card" style="padding:12px"><div class="muted">Patients</div><h3>${d.totals.patients}</h3></div>
    <div class="card" style="padding:12px"><div class="muted">Doctors</div><h3>${d.totals.doctors}</h3></div>
    <div class="card" style="padding:12px"><div class="muted">Appointments</div><h3>${d.totals.appointments}</h3></div>
    <div class="card" style="padding:12px"><div class="muted">Slots</div><h3>${d.totals.slots}</h3></div>`;
  const byStatus = Object.entries(d.appointments_by_status).map(([k,v])=> `${esc(k)}: ${v}`).join(" · ") || "No appointments";
  const outbox = Object.entries(d.outbox_by_status).map(([k,v])=> `${esc(k)}: ${v}`).join(" · ") || "No emails";
  recent.innerHTML = `<div class="muted" style="margin-top:8px"><strong>By status:</strong> ${byStatus}</div><div class="muted"><strong>Outbox:</strong> ${outbox}</div>` +
    (d.recent_appointments.length ? `<div style="margin-top:10px"><strong>Recent</strong><ul class="tight">${d.recent_appointments.map(a=> `<li>${esc(a.patient||"?")} → ${esc(a.doctor||"?")} · ${esc(a.status)} · ${fmtDateTime(a.scheduled_start)}</li>`).join("")}</ul></div>` : "");
}

async function loadAdminPatients() {
  const box = document.getElementById("patient-admin-list");
  box.innerHTML = '<p class="muted">Loading…</p>';
  const r = await api("GET", "/api/admin/patients");
  if (!r.ok) { box.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  if (!r.data.length) { box.innerHTML = '<p class="muted">No patients yet.</p>'; return; }
  box.innerHTML = r.data.map(u=> `<div class="item"><h4>${esc(u.full_name)}</h4><div class="meta">${esc(u.email)}${u.phone? " · "+esc(u.phone):""}</div></div>`).join("");
}

async function loadAdminAppointments() {
  const box = document.getElementById("admin-appt-list");
  const status = document.getElementById("appt-status-filter").value;
  box.innerHTML = '<p class="muted">Loading…</p>';
  const r = await api("GET", "/api/admin/appointments" + (status? "?status="+status:""));
  if (!r.ok) { box.innerHTML = `<p class="alert error">${esc(errText(r))}</p>`; return; }
  if (!r.data.length) { box.innerHTML = '<p class="muted">No appointments.</p>'; return; }
  box.innerHTML = r.data.map(a=> `<div class="item"><div class="row space-between"><div><strong>${esc(a.patient||"?")}</strong> → ${esc(a.doctor||"?")}<div class="meta">${esc(a.specialisation||"")} · ${fmtDateTime(a.scheduled_start)}</div></div><span class="badge ${esc(a.status)}">${esc(a.status)}</span></div></div>`).join("");
}

function initAdmin() {
  addWhRow();
  document.getElementById("add-wh").onclick = addWhRow;
  document.getElementById("create-doctor").addEventListener("submit", createDoctor);
  document.getElementById("refresh-doctors").onclick = loadAdminDoctors;
  document.getElementById("refresh-outbox").onclick = loadOutbox;
  document.getElementById("outbox-filter").onchange = loadOutbox;
  document.getElementById("refresh-stats").onclick = loadAdminStats;
  document.getElementById("refresh-patients").onclick = loadAdminPatients;
  document.getElementById("refresh-admin-appts").onclick = loadAdminAppointments;
  document.getElementById("appt-status-filter").onchange = loadAdminAppointments;
  loadAdminStats();
  loadAdminPatients();
  loadAdminAppointments();
  loadAdminDoctors();
  loadOutbox();
}

// ---------------------------------------------------------------- router
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  ({ login: initLogin, patient: initPatient, book: initBook, doctor: initDoctor, admin: initAdmin }[page] || (() => {}))();
});
