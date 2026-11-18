"use strict";
/* ZKTeco Access – interfaccia web (ingress Home Assistant). Vanilla JS, nessuna build. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => { const d = new Date(ts * 1000); return d.toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
const today = () => new Date().toISOString().slice(0, 10);

const S = { state: null, doors: [], aux: [], levels: [], tzs: [], users: [], categories: {}, panels: [], evOldest: null, selected: new Set() };

async function api(path, opts = {}) {
  const o = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) {
    if (opts.raw) o.body = opts.body; else { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data && data.error ? data.error : `HTTP ${r.status}`);
  return data;
}

function toast(msg, err = false) {
  const d = document.createElement("div");
  d.textContent = msg; if (err) d.className = "err";
  $("#toast").appendChild(d); setTimeout(() => d.remove(), err ? 7000 : 3500);
}
async function run(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}
function pushSummary(push) {
  if (!push) return "";
  const bad = Object.entries(push).filter(([, v]) => v !== "ok");
  return bad.length ? "\nIn coda: " + bad.map(([k]) => k).join(", ") : "\nInviata a tutti i controller";
}

/* ---------------------------------------------------------------- modal */
function modal(title, html, buttons = []) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  const foot = $("#modalFoot"); foot.innerHTML = "";
  for (const b of buttons) {
    const el = document.createElement("button");
    el.className = "btn " + (b.cls || ""); el.textContent = b.label;
    el.onclick = async () => { el.disabled = true; try { if ((await b.click?.()) !== false) closeModal(); } catch (e) { /* già notificato */ } finally { el.disabled = false; } };
    foot.appendChild(el);
  }
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; $("#modalBody").innerHTML = ""; }
$("#modalClose").onclick = closeModal;
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });

/* ----------------------------------------------------------------- tabs */
$$("#tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + t));
  location.hash = t;
  ({ users: loadUsers, levels: renderLevels, doors: renderDoors, events: () => loadEvents(true), panels: loadPanels, times: loadTimes, settings: loadSettings }[t] || (() => {}))();
}

/* ---------------------------------------------------------------- stato */
async function loadState() {
  const st = await api("api/state");
  S.state = st; S.doors = st.doors; S.aux = st.aux; S.levels = st.levels; S.tzs = st.timezones; S.categories = st.categories;
  $("#ver").textContent = "v" + st.version + " · " + st.user_count + (st.user_count === 1 ? " tessera" : " tessere");
  const m = $("#mqtt");
  m.textContent = st.mqtt ? "MQTT ✓" : (st.mqtt_configured ? "MQTT …" : "MQTT off");
  m.className = "pill " + (st.mqtt ? "ok" : "warn");
  fillSelects();
  renderMonitor();
}
function fillSelects() {
  const lvOpts = S.levels.map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("");
  $("#userLevelFilter").innerHTML = `<option value="">Tutti i livelli</option>` + lvOpts;
  $("#bulkLevel").innerHTML = lvOpts;
  $("#evPanel").innerHTML = `<option value="">Tutti i dispositivi</option>` + (S.state.panels || []).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("#evCat").innerHTML = `<option value="">Tutte le categorie</option>` + Object.entries(S.categories).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
}
function panelById(id) { return (S.state?.panels || []).find((p) => p.id === id); }
function doorLabel(id) { const d = S.doors.find((x) => x.id === id); return d ? d.name : "#" + id; }

/* -------------------------------------------------------------- monitor */
function renderMonitor() {
  if (!S.state) return;
  const q = $("#doorFilter").value.toLowerCase();
  const onlyAl = $("#onlyAlarms").checked;
  const online = S.state.panels.filter((p) => p.online).length;
  const c = $("#conn");
  c.textContent = `${online}/${S.state.panels.length} controller online`;
  c.className = "pill " + (online === S.state.panels.length && online ? "ok" : online ? "warn" : "bad");
  const html = S.state.panels.map((p) => {
    const doors = S.doors.filter((d) => d.panel_id === p.id && !d.hidden).filter((d) => {
      const ds = p.doors[d.door_no] || {};
      if (onlyAl && !(ds.alarm || ds.unlocked || ds.sensor === 2)) return false;
      return !q || d.name.toLowerCase().includes(q) || p.name.toLowerCase().includes(q);
    });
    if (!doors.length && (q || onlyAl)) return "";
    const tiles = doors.map((d) => {
      const ds = p.doors[d.door_no] || {};
      const sensor = ds.sensor === 2 ? "🚪 aperta" : ds.sensor === 1 ? "chiusa" : "";
      const lock = ds.normal_open ? "🔓 sempre aperta" : ds.unlocked ? "🔓 sbloccata" : "🔒";
      const al = ds.alarm ? `<span title="Allarme">⚠️</span>` : "";
      const le = ds.last_event ? `${fmtTime(ds.last_event.ts).slice(-8)} · ${esc(ds.last_event.description)}${ds.last_event.user ? " · " + esc(ds.last_event.user) : ""}` : "";
      return `<div class="door ${ds.alarm ? "alarm" : ""} ${ds.unlocked ? "unlocked" : ""}">
        <div class="dn"><span>${esc(d.name)}</span><span class="icons">${al}</span></div>
        <div class="st">${lock} ${sensor ? "· " + sensor : ""}</div>
        <div class="last" title="${esc(le)}">${le || "&nbsp;"}</div>
        <div class="acts">
          <button class="btn sm primary" data-open="${d.id}" ${p.online ? "" : "disabled"}>Apri</button>
          <button class="btn sm" data-hold="${d.id}" data-on="${ds.normal_open ? 0 : 1}" ${p.online ? "" : "disabled"}>${ds.normal_open ? "Ripristina" : "Sempre aperta"}</button>
        </div></div>`;
    }).join("");
    return `<div class="panel-card"><div class="panel-head"><span class="dot ${p.online ? "on" : ""}"></span><b>${esc(p.name)}</b>
      <span class="muted">${esc(p.host || "")}</span>${p.queue ? `<span class="pill warn">${p.queue} in coda</span>` : ""}
      ${!p.online ? `<span class="muted" title="${esc(p.error)}">offline${p.error ? ": " + esc(p.error).slice(0, 60) : ""}</span>` : ""}
      <span class="grow"></span>
      <button class="btn sm" data-pact="cancel_alarm" data-pid="${p.id}" ${p.online ? "" : "disabled"}>Annulla allarmi</button></div>
      <div class="doors">${tiles || '<span class="muted">Nessuna porta</span>'}</div></div>`;
  }).join("");
  $("#panelsGrid").innerHTML = html || `<div class="card"><p>Nessun controller configurato. Vai su <b>Impostazioni</b> per importare il database di ZKAccess oppure su <b>Dispositivi</b> per aggiungerne uno.</p></div>`;
}
$("#doorFilter").oninput = renderMonitor;
$("#onlyAlarms").onchange = renderMonitor;
$("#panelsGrid").onclick = async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.open) await run(() => api(`api/doors/${b.dataset.open}/open`, { body: {} }), "Porta aperta");
  if (b.dataset.hold) await run(() => api(`api/doors/${b.dataset.hold}/normal_open`, { body: { enable: b.dataset.on === "1" } }), b.dataset.on === "1" ? "Porta sempre aperta" : "Porta ripristinata");
  if (b.dataset.pact) await run(() => api(`api/panels/${b.dataset.pid}/action`, { body: { action: b.dataset.pact } }), "Fatto");
};
$("#btnCancelAll").onclick = () => run(() => api("api/cmd", { body: { action: "cancel_alarm" } }), "Allarmi annullati");

function feedItem(e) {
  return `<div class="ev c-${e.category}"><span class="t">${fmtTime(e.ts).slice(-8)}</span><b>${esc(e.door)}</b><br>${esc(e.description)}${e.user ? " · <b>" + esc(e.user) + "</b>" : ""}${e.card && !e.user ? ` · tessera ${esc(e.card)}` : ""}</div>`;
}
async function loadFeed() {
  const evs = await api("api/events?limit=60");
  $("#liveFeed").innerHTML = evs.map(feedItem).join("") || '<p class="muted">Nessun evento</p>';
}

/* ---------------------------------------------------------------- live */
function connectStream() {
  let es;
  try { es = new EventSource("api/stream"); } catch (e) { setInterval(loadState, 5000); return; }
  es.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "state") { Object.assign(S.state, msg.data); renderMonitor(); }
    else if (msg.type === "panel") {
      const i = S.state.panels.findIndex((p) => p.id === msg.data.id);
      if (i >= 0) S.state.panels[i] = msg.data; else S.state.panels.push(msg.data);
      renderMonitor();
    } else if (msg.type === "event") {
      const f = $("#liveFeed"); f.insertAdjacentHTML("afterbegin", feedItem(msg.data));
      while (f.children.length > 80) f.lastChild.remove();
      if ($("#tab-events").classList.contains("active") && !$("#evSearch").value) $("#eventsTable tbody")?.insertAdjacentHTML("afterbegin", eventRow(msg.data));
    } else if (msg.type === "panels") { loadState(); }
    else if (msg.type === "users") { if ($("#tab-users").classList.contains("active")) loadUsers(); }
    else if (msg.type === "sync") { const s = msg.data; $("#syncStatus").textContent = s.running ? `Sincronizzazione ${s.progress}% – ${s.message}` : (s.result ? "Sincronizzazione completata" : ""); if (!s.running && s.result) { toast("Sincronizzazione completata:\n" + Object.entries(s.result).map(([k, v]) => `${k}: ${typeof v === "object" ? `${v.inviati} inviati, ${v.rimossi} rimossi` : v}`).join("\n")); loadPanels(); } }
  };
  es.onerror = () => { $("#conn").textContent = "riconnessione…"; $("#conn").className = "pill warn"; };
}

/* ---------------------------------------------------------------- utenti */
async function loadUsers() {
  S.users = await api("api/users?q=" + encodeURIComponent($("#userSearch").value));
  renderUsers();
}
function renderUsers() {
  const lvf = $("#userLevelFilter").value, stf = $("#userStateFilter").value, t = today();
  const lvName = Object.fromEntries(S.levels.map((l) => [l.id, l.name]));
  const rows = S.users.filter((u) => {
    if (lvf && !u.levels.includes(+lvf)) return false;
    if (stf === "1" && !u.enabled) return false;
    if (stf === "0" && u.enabled) return false;
    if (stf === "exp" && !(u.end_date && u.end_date < t)) return false;
    return true;
  });
  const head = `<thead><tr><th><input type="checkbox" id="selAll"></th><th>PIN</th><th>Nome</th><th>Camera / gruppo</th><th>Tessera</th><th>Accessi</th><th>Validità</th><th>Stato</th><th></th></tr></thead>`;
  const body = rows.map((u) => {
    const acc = u.levels.map((l) => `<span class="tag">${esc(lvName[l] || l)}</span>`).join("") + u.doors.map((d) => `<span class="tag" title="porta singola">🚪 ${esc(doorLabel(d))}</span>`).join("");
    const exp = u.end_date && u.end_date < t;
    const val = (u.start_date || u.end_date) ? `${u.start_date || "…"} → ${u.end_date || "…"}` : '<span class="muted">sempre</span>';
    return `<tr data-id="${u.id}"><td><input type="checkbox" class="sel" ${S.selected.has(u.id) ? "checked" : ""}></td><td>${esc(u.pin)}</td><td>${esc(u.name)}</td><td>${esc(u.grp)}</td>
      <td><code>${esc(u.card) || '<span class="muted">—</span>'}</code></td><td>${acc || '<span class="muted">nessuno</span>'}</td><td>${val}</td>
      <td>${!u.enabled ? '<span class="pill bad">disattivata</span>' : exp ? '<span class="pill warn">scaduta</span>' : '<span class="pill ok">attiva</span>'}</td>
      <td><button class="btn sm" data-edit="${u.id}">Modifica</button></td></tr>`;
  }).join("");
  $("#usersTable").innerHTML = head + `<tbody>${body || '<tr><td colspan="9" class="muted">Nessuna tessera</td></tr>'}</tbody>`;
  $("#selAll").onchange = (e) => { rows.forEach((u) => e.target.checked ? S.selected.add(u.id) : S.selected.delete(u.id)); renderUsers(); };
  updateBulk();
}
let userSearchTimer;
$("#userSearch").oninput = () => { clearTimeout(userSearchTimer); userSearchTimer = setTimeout(loadUsers, 250); };
$("#userLevelFilter").onchange = renderUsers; $("#userStateFilter").onchange = renderUsers;
$("#usersTable").onclick = (e) => {
  const b = e.target.closest("[data-edit]"); if (b) return editUser(S.users.find((u) => u.id === +b.dataset.edit));
  const c = e.target.closest(".sel"); if (c) { const id = +c.closest("tr").dataset.id; c.checked ? S.selected.add(id) : S.selected.delete(id); updateBulk(); }
};
$("#usersTable").ondblclick = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) editUser(S.users.find((u) => u.id === +tr.dataset.id)); };
function updateBulk() { $("#bulkBar").hidden = !S.selected.size; $("#bulkCount").textContent = `${S.selected.size} selezionate`; }
$$("[data-bulk]").forEach((b) => b.onclick = async () => {
  const action = b.dataset.bulk;
  if (action === "delete" && !confirm(`Eliminare ${S.selected.size} tessere anche dai controller?`)) return;
  await run(() => api("api/users/bulk", { body: { ids: [...S.selected], action, level_id: $("#bulkLevel").value, end_date: $("#bulkEnd").value } }), (r) => `${r.aggiornati} tessere aggiornate`);
  S.selected.clear(); loadUsers();
});
$("#btnNewUser").onclick = async () => editUser({ pin: (await api("api/users/next_pin")).pin, enabled: 1, levels: [], doors: [] });

function doorPicker(selected, name = "door") {
  const groups = {};
  S.doors.forEach((d) => (groups[d.panel_name] ||= []).push(d));
  return `<input type="search" class="pickfilter" placeholder="Filtra…" style="width:100%;margin-bottom:6px">` + Object.entries(groups).map(([g, ds]) => `<div class="pg"><div class="grp">${esc(g)}</div><div class="items">${ds.map((d) =>
    `<label data-t="${esc((g + " " + d.name).toLowerCase())}"><input type="checkbox" name="${name}" value="${d.id}" ${selected.includes(d.id) ? "checked" : ""}> ${esc(d.name)}</label>`).join("")}</div></div>`).join("");
}
function levelPicker(selected) {
  return `<input type="search" class="pickfilter" placeholder="Filtra…" style="width:100%;margin-bottom:6px"><div class="items">` + S.levels.map((l) =>
    `<label data-t="${esc(l.name.toLowerCase())}" title="${esc(l.doors.map(doorLabel).join(", "))}"><input type="checkbox" name="level" value="${l.id}" ${selected.includes(l.id) ? "checked" : ""}> ${esc(l.name)}</label>`).join("") + "</div>";
}
function bindPickFilters(root) {
  $$(".pickfilter", root).forEach((inp) => inp.oninput = () => {
    const q = inp.value.toLowerCase();
    $$("label[data-t]", inp.parentElement).forEach((l) => l.style.display = !q || l.dataset.t.includes(q) || l.querySelector("input").checked ? "" : "none");
  });
}

function editUser(u) {
  const panelsOpt = `<option value="">qualsiasi lettore</option>` + (S.state.panels || []).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  modal(u.id ? `Tessera ${u.pin}${u.name ? " – " + u.name : ""}` : "Nuova tessera", `
  <div class="form">
    <label>PIN / n. utente<input id="uPin" value="${esc(u.pin)}" inputmode="numeric"></label>
    <label>Nome<input id="uName" value="${esc(u.name)}"></label>
    <label>Camera / gruppo<input id="uGrp" value="${esc(u.grp)}" list="grpList"></label>
    <label class="full">Numero tessera RFID
      <div class="learn"><input id="uCard" value="${esc(u.card)}" inputmode="numeric" placeholder="es. 2095868724">
      <select id="uLearnPanel">${panelsOpt}</select><button class="btn" id="btnLearn">📇 Leggi dal lettore</button></div>
      <span class="hint" id="learnHint">Premi "Leggi dal lettore" e avvicina la tessera a un lettore: il numero verrà compilato automaticamente.</span></label>
    <label>Password (opzionale)<input id="uPwd" value="${esc(u.password)}" inputmode="numeric"></label>
    <label>Valida dal<input id="uStart" type="date" value="${esc(u.start_date)}"></label>
    <label>Valida fino al<input id="uEnd" type="date" value="${esc(u.end_date)}"></label>
    <label>Stato<select id="uEnabled"><option value="1" ${u.enabled ? "selected" : ""}>Attiva</option><option value="0" ${u.enabled ? "" : "selected"}>Disattivata</option></select></label>
    <label class="full">Livelli di accesso<div class="picker">${levelPicker(u.levels || [])}</div></label>
    <label class="full">Porte singole (in aggiunta ai livelli)<div class="picker">${doorPicker(u.doors || [])}</div></label>
    <label class="full">Note<textarea id="uNotes" rows="2">${esc(u.notes)}</textarea></label>
  </div><datalist id="grpList">${[...new Set(S.users.map((x) => x.grp).filter(Boolean))].map((g) => `<option value="${esc(g)}">`).join("")}</datalist>`, [
    ...(u.id ? [{ label: "Elimina", cls: "danger", click: async () => { if (!confirm("Eliminare la tessera anche dai controller?")) return false; await run(() => api(`api/users/${u.id}`, { method: "DELETE" }), "Tessera eliminata"); loadUsers(); } },
      { label: "Reinvia ai controller", click: async () => { await run(() => api(`api/users/${u.id}/push`, { body: {} }), (r) => "Inviata" + pushSummary(r)); return false; } }] : []),
    { label: "Annulla" },
    { label: "Salva e invia", cls: "primary", click: async () => {
      const body = {
        id: u.id, pin: $("#uPin").value.trim(), name: $("#uName").value.trim(), grp: $("#uGrp").value.trim(), card: $("#uCard").value.trim(),
        password: $("#uPwd").value.trim(), start_date: $("#uStart").value, end_date: $("#uEnd").value, enabled: $("#uEnabled").value === "1",
        notes: $("#uNotes").value, levels: $$("input[name=level]:checked").map((i) => +i.value), doors: $$("input[name=door]:checked").map((i) => +i.value),
      };
      const r = await run(() => api("api/users", { body }), (r) => "Tessera salvata" + pushSummary(r.push));
      loadUsers(); return r;
    } },
  ]);
  bindPickFilters($("#modalBody"));
  $("#btnLearn").onclick = async (e) => {
    e.preventDefault();
    const b = $("#btnLearn"), h = $("#learnHint");
    b.disabled = true; h.textContent = "In attesa della tessera sul lettore… (45 s)"; h.classList.add("blink");
    try {
      const r = await api("api/learn", { body: { timeout: 45, panel_id: $("#uLearnPanel").value || null } });
      if (r.card) {
        $("#uCard").value = r.card.card;
        h.textContent = `Letta tessera ${r.card.card} su ${r.card.door}` + (r.card.known_user ? ` – ATTENZIONE: già assegnata all'utente ${r.card.known_user}` : "");
      } else h.textContent = "Nessuna tessera letta: riprova.";
    } catch (err) { h.textContent = err.message; }
    finally { b.disabled = false; h.classList.remove("blink"); }
  };
}

/* ---------------------------------------------------------------- livelli */
async function renderLevels() {
  S.levels = await api("api/levels");
  const tzName = Object.fromEntries(S.tzs.map((t) => [t.id, t.name]));
  $("#levelsList").innerHTML = S.levels.map((l) => `<div class="card"><div class="row"><b style="font-size:15px">${esc(l.name)}</b><span class="grow"></span><button class="btn sm" data-lv="${l.id}">Modifica</button></div>
    <p class="hint">${l.users} tessere · fascia: ${esc(tzName[l.timezone_id] || l.timezone_id)}</p>
    <div>${l.doors.map((d) => `<span class="tag">${esc(doorLabel(d))}</span>`).join("") || '<span class="muted">nessuna porta</span>'}</div></div>`).join("") || '<p class="muted">Nessun livello</p>';
}
$("#levelsList").onclick = (e) => { const b = e.target.closest("[data-lv]"); if (b) editLevel(S.levels.find((l) => l.id === +b.dataset.lv)); };
$("#btnNewLevel").onclick = () => editLevel({ name: "", timezone_id: 1, doors: [] });
function editLevel(l) {
  modal(l.id ? "Livello " + l.name : "Nuovo livello", `<div class="form">
    <label>Nome<input id="lName" value="${esc(l.name)}"></label>
    <label>Fascia oraria<select id="lTz">${S.tzs.map((t) => `<option value="${t.id}" ${t.id === l.timezone_id ? "selected" : ""}>${t.id} – ${esc(t.name)}</option>`).join("")}</select></label>
    <label class="full">Porte<div class="picker">${doorPicker(l.doors)}</div></label></div>
    ${l.id ? `<p class="hint">Salvando, le ${l.users} tessere con questo livello verranno aggiornate sui controller.</p>` : ""}`, [
    ...(l.id ? [{ label: "Elimina", cls: "danger", click: async () => { if (!confirm("Eliminare il livello? Le tessere perderanno queste porte.")) return false; await run(() => api(`api/levels/${l.id}`, { method: "DELETE" }), "Livello eliminato"); renderLevels(); loadState(); } }] : []),
    { label: "Annulla" },
    { label: "Salva", cls: "primary", click: async () => {
      await run(() => api("api/levels", { body: { id: l.id, name: $("#lName").value, timezone_id: +$("#lTz").value, doors: $$("input[name=door]:checked").map((i) => +i.value) } }), (r) => `Livello salvato${r.aggiornati ? ` – ${r.aggiornati} tessere aggiornate` : ""}`);
      renderLevels(); loadState();
    } },
  ]);
  bindPickFilters($("#modalBody"));
}

/* ---------------------------------------------------------------- porte */
async function renderDoors() {
  await loadState();
  $("#doorsTable").innerHTML = `<thead><tr><th>Controller</th><th>N.</th><th>Nome</th><th>Apertura (s)</th><th>Nascosta</th><th></th></tr></thead><tbody>` +
    S.doors.map((d) => `<tr data-id="${d.id}"><td>${esc(d.panel_name)}</td><td>${d.door_no}</td><td><input value="${esc(d.name)}" data-f="name"></td>
    <td><input type="number" min="1" max="254" value="${d.open_seconds}" data-f="open_seconds" style="width:70px"></td>
    <td><input type="checkbox" data-f="hidden" ${d.hidden ? "checked" : ""}></td>
    <td><button class="btn sm" data-params="${d.id}">Parametri controller</button> <button class="btn sm primary" data-open="${d.id}">Apri</button></td></tr>`).join("") + "</tbody>";
  $("#auxTable").innerHTML = `<thead><tr><th>Controller</th><th>Tipo</th><th>N.</th><th>Nome</th><th>Nascosto</th><th></th></tr></thead><tbody>` +
    S.aux.map((a) => `<tr data-aux="${a.id}"><td>${esc(a.panel_name)}</td><td>${a.kind === "in" ? "Ingresso" : "Uscita"}</td><td>${a.aux_no}</td><td><input value="${esc(a.name)}" data-f="name"></td>
    <td><input type="checkbox" data-f="hidden" ${a.hidden ? "checked" : ""}></td>
    <td>${a.kind === "out" ? `<button class="btn sm" data-pulse="${a.id}">Attiva 5 s</button> <button class="btn sm" data-off="${a.id}">Spegni</button>` : ""}</td></tr>`).join("") + "</tbody>";
}
$("#doorsTable").onchange = async (e) => {
  const inp = e.target.closest("[data-f]"); if (!inp) return;
  const id = inp.closest("tr").dataset.id, f = inp.dataset.f;
  await run(() => api(`api/doors/${id}`, { body: { [f]: inp.type === "checkbox" ? inp.checked : inp.value } }), "Salvato");
};
$("#auxTable").onchange = async (e) => {
  const inp = e.target.closest("[data-f]"); if (!inp) return;
  const id = inp.closest("tr").dataset.aux, f = inp.dataset.f;
  await run(() => api(`api/aux/${id}`, { body: { [f]: inp.type === "checkbox" ? inp.checked : inp.value } }), "Salvato");
};
$("#auxTable").onclick = async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.pulse) await run(() => api(`api/aux/${b.dataset.pulse}/output`, { body: { seconds: 5 } }), "Uscita attivata");
  if (b.dataset.off) await run(() => api(`api/aux/${b.dataset.off}/output`, { body: { seconds: 0 } }), "Uscita spenta");
};
$("#doorsTable").onclick = async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.open) await run(() => api(`api/doors/${b.dataset.open}/open`, { body: {} }), "Porta aperta");
  if (b.dataset.params) doorParams(+b.dataset.params);
};
const SENSOR = { 0: "Nessuno", 1: "Normalmente aperto (NO)", 2: "Normalmente chiuso (NC)" };
const VERIFY = { 4: "Solo tessera", 3: "Solo password", 11: "Tessera + password", 1: "Solo impronta", 6: "Tessera o impronta", 10: "Tessera + impronta", 200: "Altro" };
async function doorParams(id) {
  const d = S.doors.find((x) => x.id === id);
  modal("Parametri – " + d.name, `<p class="hint">Lettura dal controller…</p>`);
  let p;
  try { p = await api(`api/doors/${id}/params`); } catch (e) { $("#modalBody").innerHTML = `<p class="hint">Errore: ${esc(e.message)}</p>`; return; }
  const sel = (id2, map, v) => `<select id="${id2}">${v == null ? '<option value="" selected>— non letto —</option>' : ""}${Object.entries(map).map(([k, t]) => `<option value="${k}" ${String(v) === k ? "selected" : ""}>${t}</option>`).join("")}${map[v] === undefined && v != null ? `<option selected value="${esc(v)}">valore ${esc(v)}</option>` : ""}</select>`;
  const tzOpt = (v) => `<select id="pKeep">${v == null ? '<option value="" selected>— non letto —</option>' : ""}<option value="0" ${String(v) === "0" ? "selected" : ""}>nessuna</option>${S.tzs.map((t) => `<option value="${t.id}" ${String(v) === String(t.id) ? "selected" : ""}>${t.id} – ${esc(t.name)}</option>`).join("")}</select>`;
  modal("Parametri – " + d.name, `<div class="form">
    <label>Tempo apertura serratura (s)<input id="pDrive" type="number" min="0" max="254" value="${esc(p.Drivertime)}"></label>
    <label>Ritardo allarme porta aperta (s)<input id="pDetect" type="number" min="0" max="254" value="${esc(p.Detectortime)}"></label>
    <label>Sensore porta${sel("pSensor", SENSOR, p.SensorType)}</label>
    <label>Modalità di verifica${sel("pVerify", VERIFY, p.VerifyType)}</label>
    <label>Intervallo minimo tra letture (s)<input id="pInter" type="number" min="0" max="254" value="${esc(p.Intertime)}"></label>
    <label>Fascia "sempre aperta"${tzOpt(p.KeepOpenTimeZone)}</label>
    <label>Fascia validità porta<select id="pValid">${p.ValidTZ == null ? '<option value="" selected>— non letto —</option>' : ""}${S.tzs.map((t) => `<option value="${t.id}" ${String(p.ValidTZ) === String(t.id) ? "selected" : ""}>${t.id} – ${esc(t.name)}</option>`).join("")}</select></label>
    <label>Chiudi alla chiusura porta<select id="pClose">${p.CloseAndLock == null ? '<option value="" selected>— non letto —</option>' : ""}<option value="0" ${p.CloseAndLock == 0 ? "selected" : ""}>No</option><option value="1" ${p.CloseAndLock == 1 ? "selected" : ""}>Sì</option></select></label>
    <label>Super password (apertura)<input id="pSuper" value="${esc(p.SupperPassWord || "")}"></label>
    <label>Password antirapina<input id="pForce" value="${esc(p.ForcePassWord || "")}"></label>
  </div><p class="hint">I valori vengono scritti direttamente nel controller.</p>`, [
    { label: "Annulla" },
    { label: "Scrivi nel controller", cls: "primary", click: () => {
      const vals = {
        Drivertime: $("#pDrive").value, Detectortime: $("#pDetect").value, SensorType: $("#pSensor").value, VerifyType: $("#pVerify").value,
        Intertime: $("#pInter").value, KeepOpenTimeZone: $("#pKeep").value, ValidTZ: $("#pValid").value, CloseAndLock: $("#pClose").value,
        SupperPassWord: $("#pSuper").value, ForcePassWord: $("#pForce").value };
      // scrive solo i valori letti dal controller e modificati
      const changed = Object.fromEntries(Object.entries(vals).filter(([k, v]) => p[k] != null && v !== "" && String(v) !== String(p[k])));
      if (!Object.keys(changed).length) { toast("Nessuna modifica"); return; }
      return run(() => api(`api/doors/${id}/params`, { body: changed }), "Parametri salvati: " + Object.keys(changed).join(", "));
    } },
  ]);
}

/* ---------------------------------------------------------------- eventi */
function eventRow(e) {
  return `<tr><td style="white-space:nowrap">${fmtTime(e.ts)}</td><td>${esc(e.panel)}</td><td>${esc(e.door)}</td><td><span class="cat ${e.category}">${esc(S.categories[e.category] || e.category)}</span> ${esc(e.description)}</td>
    <td>${esc(e.user)}</td><td>${esc(e.pin)}</td><td><code>${esc(e.card)}</code></td></tr>`;
}
function evQuery() {
  const days = +$("#evSince").value;
  const since = days ? Date.now() / 1000 - days * 86400 : "";
  return `q=${encodeURIComponent($("#evSearch").value)}&panel=${$("#evPanel").value}&category=${$("#evCat").value}&since=${since}`;
}
async function loadEvents(reset) {
  if (reset) { S.evOldest = null; $("#eventsTable").innerHTML = `<thead><tr><th>Data/ora</th><th>Controller</th><th>Porta</th><th>Evento</th><th>Utente</th><th>PIN</th><th>Tessera</th></tr></thead><tbody></tbody>`; }
  const evs = await api(`api/events?limit=200&${evQuery()}${S.evOldest ? "&before=" + S.evOldest : ""}`);
  if (evs.length) S.evOldest = evs[evs.length - 1].ts;
  $("#eventsTable tbody").insertAdjacentHTML("beforeend", evs.map(eventRow).join(""));
  $("#evMore").hidden = evs.length < 200;
  $("#evCsv").href = "api/events.csv?" + evQuery();
}
let evTimer;
$("#evSearch").oninput = () => { clearTimeout(evTimer); evTimer = setTimeout(() => loadEvents(true), 300); };
["#evPanel", "#evCat", "#evSince"].forEach((s) => $(s).onchange = () => loadEvents(true));
$("#evMore").onclick = () => loadEvents(false);

/* ------------------------------------------------------------- pannelli */
async function loadPanels() {
  S.panels = await api("api/panels");
  $("#panelsTable").innerHTML = `<thead><tr><th></th><th>Nome</th><th>IP:porta</th><th>Modello / firmware</th><th>Seriale</th><th>Porte</th><th>Coda</th><th></th></tr></thead><tbody>` +
    S.panels.map((p) => `<tr><td><span class="dot ${p.state.online ? "on" : ""}" title="${esc(p.state.error)}"></span></td><td><b>${esc(p.name)}</b>${p.enabled ? "" : ' <span class="pill">disattivato</span>'}</td>
      <td>${esc(p.host)}:${p.port}</td><td>${esc(p.model)} <span class="muted">${esc(p.firmware)}</span></td><td><code>${esc(p.serial)}</code></td><td>${p.locks}</td><td>${p.queue || ""}</td>
      <td style="white-space:nowrap"><button class="btn sm" data-pe="${p.id}">Modifica</button> <button class="btn sm" data-ps="${p.id}">Sincronizza</button> <button class="btn sm" data-pu="${p.id}">Tessere nel controller</button>
      <button class="btn sm" data-pa="sync_time" data-id="${p.id}">Ora</button> <button class="btn sm" data-pa="restart" data-id="${p.id}">Riavvia</button></td></tr>`).join("") + "</tbody>";
}
$("#panelsTable").onclick = async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.pe) editPanel(S.panels.find((p) => p.id === +b.dataset.pe));
  if (b.dataset.ps) { if (!confirm("Riscrivere tutte le tessere su questo controller? Le tessere non presenti nell'add-on verranno rimosse dal controller.")) return; await run(() => api(`api/panels/${b.dataset.ps}/sync`, { body: {} }), (r) => `Sincronizzato: ${r.inviati} inviate, ${r.rimossi} rimosse`); loadPanels(); }
  if (b.dataset.pu) panelUsers(+b.dataset.pu);
  if (b.dataset.pa) { if (b.dataset.pa === "restart" && !confirm("Riavviare il controller?")) return; await run(() => api(`api/panels/${b.dataset.id}/action`, { body: { action: b.dataset.pa } }), "Fatto"); }
};
$("#btnNewPanel").onclick = () => editPanel({ name: "", host: "", port: 4370, password: "", enabled: 1 });
$("#btnSyncAll").onclick = async () => { if (!confirm("Sincronizzare tutte le tessere su tutti i controller? (le tessere sconosciute verranno rimosse dai controller)")) return; await run(() => api("api/sync_all", { body: {} }), "Sincronizzazione avviata"); };
$("#btnImportPanels").onclick = async () => { await run(() => api("api/import_from_panels", { body: {} }), (r) => `Importate: ${r.aggiunti} nuove, ${r.aggiornati} aggiornate` + (Object.keys(r.errori).length ? `\nErrori: ${Object.keys(r.errori).join(", ")}` : "")); };
function editPanel(p) {
  modal(p.id ? "Controller " + p.name : "Nuovo controller", `<div class="form">
    <label>Nome<input id="pName" value="${esc(p.name)}"></label>
    <label>Indirizzo IP<input id="pHost" value="${esc(p.host)}" placeholder="192.168.2.201"></label>
    <label>Porta TCP<input id="pPort" type="number" value="${p.port || 4370}"></label>
    <label>Password di comunicazione<input id="pPwd" value="${esc(p.password)}"></label>
    <label>Stato<select id="pEn"><option value="1" ${p.enabled ? "selected" : ""}>Attivo</option><option value="0" ${p.enabled ? "" : "selected"}>Disattivato</option></select></label>
  </div><p id="pTestRes" class="hint"></p>`, [
    ...(p.id ? [{ label: "Elimina", cls: "danger", click: async () => { if (!confirm("Eliminare il controller dall'add-on? (i dati nel controller restano)")) return false; await run(() => api(`api/panels/${p.id}`, { method: "DELETE" }), "Eliminato"); loadPanels(); loadState(); } }] : []),
    { label: "Prova connessione", click: async () => {
      $("#pTestRes").textContent = "Connessione…";
      const r = await api("api/panels/test", { body: { host: $("#pHost").value, port: $("#pPort").value, password: $("#pPwd").value } });
      $("#pTestRes").textContent = r.ok ? `✓ Connesso: ${r.info["~DeviceName"] || ""} ${r.info.FirmVer || ""} · S/N ${r.info["~SerialNumber"] || "?"} · ${r.info.LockCount || "?"} porte` : "✗ " + r.error;
      return false;
    } },
    { label: "Salva", cls: "primary", click: async () => { await run(() => api("api/panels", { body: { id: p.id, name: $("#pName").value, host: $("#pHost").value, port: $("#pPort").value, password: $("#pPwd").value, enabled: $("#pEn").value === "1" } }), "Controller salvato"); loadPanels(); loadState(); } },
  ]);
}
async function panelUsers(pid) {
  const p = S.panels.find((x) => x.id === pid);
  modal("Tessere memorizzate in " + p.name, `<p class="hint">Lettura…</p>`);
  try {
    const rows = await api(`api/panels/${pid}/users`);
    const dn = Object.fromEntries(S.doors.filter((d) => d.panel_id === pid).map((d) => [d.door_no, d.name]));
    $("#modalBody").innerHTML = `<p class="hint">${rows.length} tessere nel controller</p><table class="tbl"><thead><tr><th>PIN</th><th>Tessera</th><th>Nome</th><th>Porte</th><th>Validità</th></tr></thead><tbody>` +
      rows.map((r) => `<tr><td>${esc(r.pin)}</td><td><code>${esc(r.card)}</code></td><td>${esc(r.name)}</td><td>${r.doors.map((n) => `<span class="tag">${esc(dn[n] || n)}</span>`).join("")}</td><td>${esc(r.start)} ${r.end ? "→ " + esc(r.end) : ""}</td></tr>`).join("") + "</tbody></table>";
  } catch (e) { $("#modalBody").innerHTML = `<p class="hint">Errore: ${esc(e.message)}</p>`; }
}

/* --------------------------------------------------------- fasce orarie */
const DAYN = { mon: "Lunedì", tue: "Martedì", wed: "Mercoledì", thu: "Giovedì", fri: "Venerdì", sat: "Sabato", sun: "Domenica", hol1: "Festivo 1", hol2: "Festivo 2", hol3: "Festivo 3" };
async function loadTimes() {
  S.tzFull = await api("api/timezones");
  $("#tzList").innerHTML = S.tzFull.map((t) => `<div class="card"><div class="row"><b>${t.id} – ${esc(t.name)}</b><span class="grow"></span><button class="btn sm" data-tz="${t.id}">Modifica</button></div>
    <p class="hint">${["mon", "sat", "sun"].map((d) => `${DAYN[d].slice(0, 3)}: ${(t.data[d] || []).filter((iv) => iv[0] !== iv[1]).map((iv) => iv.join("-")).join(", ") || "chiuso"}`).join(" · ")}</p></div>`).join("");
  const hol = await api("api/holidays");
  $("#holTable").innerHTML = `<thead><tr><th>Nome</th><th>Data</th><th>Tipo</th><th>Ogni anno</th><th></th></tr></thead><tbody>` +
    hol.map((h) => `<tr><td>${esc(h.name)}</td><td>${h.day}</td><td>${h.type}</td><td>${h.loop ? "sì" : "no"}</td><td><button class="btn sm danger" data-hdel="${h.id}">Elimina</button></td></tr>`).join("") + "</tbody>";
}
$("#tzList").onclick = (e) => { const b = e.target.closest("[data-tz]"); if (b) editTz(S.tzFull.find((t) => t.id === +b.dataset.tz)); };
$("#btnNewTz").onclick = () => editTz({ name: "", data: {} });
$("#holTable").onclick = async (e) => { const b = e.target.closest("[data-hdel]"); if (b) { await run(() => api(`api/holidays/${b.dataset.hdel}`, { method: "DELETE" })); loadTimes(); } };
$("#btnAddHol").onclick = async () => { await run(() => api("api/holidays", { body: { name: $("#holName").value || "Festività", day: $("#holDay").value, type: +$("#holType").value, loop: $("#holLoop").checked } }), "Aggiunta"); loadTimes(); };
$("#btnPushTz").onclick = () => run(() => api("api/timezones/push", { body: {} }), (r) => "Inviate:\n" + Object.entries(r).map(([k, v]) => `${k}: ${v}`).join("\n"));
function editTz(t) {
  const rows = Object.keys(DAYN).map((d) => {
    const iv = t.data[d] || [["00:00", "23:59"], ["00:00", "00:00"], ["00:00", "00:00"]];
    return `<span>${DAYN[d]}</span>` + [0, 1, 2].map((i) => `<span><input type="time" data-d="${d}" data-i="${i}" data-k="0" value="${(iv[i] || ["00:00"])[0]}"> – <input type="time" data-d="${d}" data-i="${i}" data-k="1" value="${(iv[i] || [, "00:00"])[1]}"></span>`).join("");
  }).join("");
  modal(t.id ? `Fascia ${t.id}` : "Nuova fascia oraria", `<div class="form"><label>Nome<input id="tzName" value="${esc(t.name)}"></label></div>
    <p class="hint">Fino a 3 intervalli per giorno; un intervallo 00:00–00:00 è vuoto. Dopo le modifiche usa "Invia fasce e festività ai controller".</p>
    <div class="tzgrid"><b>Giorno</b><b>Intervallo 1</b><b>Intervallo 2</b><b>Intervallo 3</b>${rows}</div>`, [
    ...(t.id && t.id !== 1 ? [{ label: "Elimina", cls: "danger", click: async () => { await run(() => api(`api/timezones/${t.id}`, { method: "DELETE" }), "Eliminata"); loadTimes(); loadState(); } }] : []),
    { label: "Annulla" },
    { label: "Salva", cls: "primary", click: async () => {
      const data = {};
      $$(".tzgrid input").forEach((i) => { ((data[i.dataset.d] ||= [["00:00", "00:00"], ["00:00", "00:00"], ["00:00", "00:00"]])[+i.dataset.i])[+i.dataset.k] = i.value || "00:00"; });
      await run(() => api("api/timezones", { body: { id: t.id, name: $("#tzName").value, data } }), "Fascia salvata"); loadTimes(); loadState();
    } },
  ]);
}

/* ---------------------------------------------------------- impostazioni */
async function loadSettings() { $("#setRetention").value = S.state?.settings?.event_retention_days ?? 365; }
$("#btnSaveSettings").onclick = () => run(() => api("api/settings", { body: { event_retention_days: +$("#setRetention").value } }), "Salvato");
$("#btnImportMdb").onclick = async () => {
  const f = $("#mdbFile").files[0]; if (!f) return toast("Scegli il file zkaccess.mdb", true);
  if ($("#mdbReplace").checked && !confirm("Cancellare tutti i dati attuali e sostituirli con quelli di ZKAccess?")) return;
  const CH = 4 * 1048576, total = Math.ceil(f.size / CH), rep = $("#mdbReplace").checked ? 1 : 0;
  const r = await run(async () => {
    let res;
    for (let n = 0; n < total; n++) {
      $("#importResult").textContent = `Caricamento ${Math.round((n + 1) * 100 / total)}% di ${(f.size / 1048576).toFixed(1)} MB…` + (n + 1 === total ? " importazione…" : "");
      res = await api(`api/import/chunk?n=${n}&total=${total}&replace=${rep}`, { body: f.slice(n * CH, (n + 1) * CH), raw: true });
    }
    return res;
  }, "Importazione completata");
  $("#importResult").textContent = JSON.stringify(r, null, 1); loadState();
};
$("#btnImportPath").onclick = async () => {
  const r = await run(() => api("api/import/mdb?path=" + encodeURIComponent($("#mdbPath").value) + "&replace=" + ($("#mdbReplace").checked ? 1 : 0), { body: "", raw: true }), "Importazione completata");
  $("#importResult").textContent = JSON.stringify(r, null, 1); loadState();
};
$("#btnRestore").onclick = async () => {
  const f = $("#restoreFile").files[0]; if (!f) return toast("Scegli il file di backup", true);
  if (!confirm("Sostituire tutta la configurazione con il backup?")) return;
  await run(async () => api("api/restore", { body: await f.text(), raw: true }), "Backup ripristinato"); loadState();
};

/* ---------------------------------------------------------------- avvio */
(async function init() {
  try { await loadState(); await loadFeed(); } catch (e) { toast("Errore di caricamento: " + e.message, true); }
  connectStream();
  setInterval(() => { if (S.state) renderMonitor(); }, 5000);
  const t = location.hash.slice(1); if (t && $("#tab-" + t)) showTab(t);
  window.addEventListener("hashchange", () => { const h = location.hash.slice(1); if (h && $("#tab-" + h) && !$("#tab-" + h).classList.contains("active")) showTab(h); });
})();
