/* Newtonite Ops frontend: dependency-free.
 *
 * State model
 *   S is the single source of truth. The SERVER is authoritative: the UI may be optimistic for
 *   claim/release/transition/priority, but every optimistic change is reconciled with the server's
 *   response (success -> replace with server copy; failure -> roll back and, on 409, adopt the
 *   server's `current` item). Nothing is ever "remembered" locally beyond what the server confirmed.
 *
 * Staleness / concurrency handled here
 *   - list loads carry a sequence number: a slow, older response can never overwrite a newer one
 *   - /api/changes is polled; changed items visible on screen are re-fetched
 *   - an edit form is pinned to the version it was opened on, so a concurrent change is detected
 *     (banner while editing, 409 on save) instead of being silently overwritten
 *   - every mutating call carries an Idempotency-Key and is retried on network failure with the SAME key
 */
(() => {
  "use strict";

  // ---------- utilities ----------
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "k" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const label = (s) => String(s || "").replace(/_/g, " ");
  const cap = (s) => label(s).replace(/^./, (c) => c.toUpperCase());
  const clone = (o) => JSON.parse(JSON.stringify(o));

  function ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    if (s < 86400 * 14) return Math.round(s / 86400) + "d ago";
    return new Date(iso).toLocaleDateString();
  }
  const full = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  function dueText(it) {
    if (!it.due_at) return "";
    const d = (new Date(it.due_at).getTime() - Date.now()) / 86400000;
    if (it.overdue) return `<span class="late">Overdue ${Math.max(1, Math.round(-d))}d</span>`;
    if (["resolved", "closed"].includes(it.status)) return "";
    return d < 1 ? "Due today" : `Due in ${Math.round(d)}d`;
  }

  // ---------- state ----------
  const S = {
    token: sessionStorage.getItem("token"), me: null,
    view: "attention", filters: { q: "", team_id: "", status: "", priority: "", sort: "updated" },
    list: { items: [], cursor: null, hasMore: false, loading: false, seq: 0, error: null, dirty: false },
    dash: null, sel: null, detail: null, events: [], eventsMore: null, members: {},
    editing: null, prompt: null, conflict: null, remoteChange: null, commentDraft: "",
    cursor: null, live: true, notifs: { items: [], unread: 0 }, newKey: null, busy: new Set(),
  };

  // ---------- API with idempotent retry ----------
  class ApiError extends Error {
    constructor(status, body) {
      super(body?.error?.message || "Request failed");
      this.status = status; this.code = body?.error?.code; this.details = body?.error?.details || {};
    }
  }
  async function api(method, path, { body, idem } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (S.token) headers.Authorization = "Bearer " + S.token;
    if (idem) headers["Idempotency-Key"] = idem;
    for (let attempt = 0; ; attempt++) {
      let res;
      try {
        res = await fetch("/api" + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
      } catch (_) {
        // Network failure: the outcome is unknown. Only safe to retry when the server can de-duplicate.
        if ((idem || method === "GET") && attempt < 2) { await sleep(500 * (attempt + 1)); continue; }
        throw new ApiError(0, { error: { code: "network", message: "Cannot reach the server. Check your connection; the change may not have been saved." } });
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status === 401 && path !== "/auth/login") { signOut(true); }
        throw new ApiError(res.status, data);
      }
      return data;
    }
  }

  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = "toast " + kind; el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
  }

  // ---------- auth ----------
  const DEMO = [["sam", "Member (Support)"], ["priya", "Lead (Support)"], ["maya", "Member (Support+Payments)"], ["ethan", "Lead (Engineering)"], ["vik", "Viewer (Support)"], ["ada", "Team admin"]];

  function renderLogin(error) {
    $("#root").innerHTML = `
      <div class="login"><form class="login-box" id="login">
        <div><h1>Newtonite Ops</h1><p>Sign in to coordinate operational work.</p></div>
        ${error ? `<div class="err" role="alert">${esc(error)}</div>` : ""}
        <label class="field">Email<input type="email" name="email" value="sam@newtonite.test" autocomplete="username" required></label>
        <label class="field">Password<input type="password" name="password" value="password123" autocomplete="current-password" required></label>
        <button class="btn primary" type="submit">Sign in</button>
        <div><p>Demo accounts (password <b>password123</b>):</p>
          <div class="demo">${DEMO.map(([u, l]) => `<button type="button" class="btn small" data-demo="${u}">${esc(l)}</button>`).join("")}</div></div>
      </form></div>`;
    $("#login").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try {
        const r = await api("POST", "/auth/login", { body: { email: f.get("email"), password: f.get("password") } });
        S.token = r.token; sessionStorage.setItem("token", r.token); S.me = r.user;
        await boot();
      } catch (err) { renderLogin(err.message); }
    });
    $("#login").addEventListener("click", (e) => {
      const u = e.target.dataset?.demo;
      if (u) { $("#login [name=email]").value = u + "@newtonite.test"; $("#login [name=password]").value = "password123"; }
    });
  }

  function signOut(expired) {
    if (!expired && S.token) api("POST", "/auth/logout").catch(() => {});
    sessionStorage.removeItem("token");
    stopPolling();
    Object.assign(S, { token: null, me: null, sel: null, detail: null, cursor: null, dash: null });
    renderLogin(expired ? "Your session ended. Please sign in again." : "");
  }

  // ---------- shell ----------
  function renderShell() {
    const teams = S.me.teams;
    $("#root").innerHTML = `
      <div class="app" id="app">
        <aside class="rail" id="rail" aria-label="Queues"></aside>
        <main class="work">
          <header class="bar">
            <input class="search" id="q" type="search" placeholder="Search title, description or #id   ( / )" aria-label="Search work items" value="${esc(S.filters.q)}">
            <select id="f-team" aria-label="Team"><option value="">All my teams</option>${teams.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
            <select id="f-status" aria-label="Status"><option value="">Any status</option>${["open", "in_progress", "blocked", "awaiting_approval", "resolved", "closed"].map((s) => `<option value="${s}">${cap(s)}</option>`).join("")}</select>
            <select id="f-priority" aria-label="Priority"><option value="">Any priority</option>${["urgent", "high", "normal", "low"].map((s) => `<option value="${s}">${cap(s)}</option>`).join("")}</select>
            <select id="f-sort" aria-label="Sort"><option value="updated">Recently updated</option><option value="priority">Highest priority</option><option value="created">Newest</option></select>
            <span class="live" id="live" title="Updates arrive automatically">Live</span>
            <button class="btn quiet bell" id="bell" aria-label="Notifications">Alerts<span class="badge" id="badge" hidden></span></button>
            <button class="btn primary" data-action="new">New work item</button>
          </header>
          <div id="dirty"></div>
          <div id="listhead" class="listhead"></div>
          <div class="list" id="list" aria-live="polite"></div>
        </main>
        <section class="detail" id="detail" hidden aria-label="Work item"></section>
      </div>
      <dialog id="newdlg"></dialog>
      <div class="popover" id="notifs" hidden></div>`;
    $("#q").addEventListener("input", debounce((e) => { S.filters.q = e.target.value.trim(); loadList(true); }, 250));
    for (const k of ["team", "status", "priority", "sort"]) {
      $("#f-" + k).addEventListener("change", (e) => { S.filters[k === "team" ? "team_id" : k] = e.target.value; loadList(true); renderRail(); });
    }
    $("#bell").addEventListener("click", toggleNotifs);
    renderRail();
  }

  const QUEUES = [
    ["attention", "Needs attention", true], ["mine", "Assigned to me"], ["approvals", "Waiting for my approval", "lead"],
    ["unassigned", "Unassigned"], ["overdue", "Overdue", true], ["stale", "Gone quiet"], ["active", "All active work"], ["all", "Everything"],
  ];
  function renderRail() {
    const c = S.dash?.counts || {};
    const isLead = S.me.teams.some((t) => ["lead", "admin"].includes(t.role));
    $("#rail").innerHTML = `
      <div class="brand">Newtonite Ops<small>Operational work, in one place</small></div>
      <nav>${QUEUES.filter(([, , f]) => f !== "lead" || isLead).map(([k, name, hot]) => {
        const n = c[k];
        return `<button class="q ${S.view === k ? "on" : ""}" data-view="${k}">${name}${n === undefined ? "" : `<span class="n ${hot && n ? "hot" : ""} ${n === 0 ? "zero" : ""}">${n}</span>`}</button>`;
      }).join("")}</nav>
      <h3 class="teams">Teams</h3>
      <nav class="teams">${(S.dash?.teams || []).map((t) => `
        <button class="q ${String(S.filters.team_id) === String(t.team_id) ? "on" : ""}" data-team="${t.team_id}" title="${esc(t.my_role)} in ${esc(t.team_name)}">${esc(t.team_name)}<span class="n">${t.open + t.in_progress + t.blocked + t.awaiting_approval}</span></button>`).join("")}</nav>
      <div class="me"><b>${esc(S.me.name)}</b><span>${esc(S.me.teams.map((t) => t.role + " · " + t.name).join(", "))}</span><br><button data-action="signout">Sign out</button></div>`;
  }

  // ---------- list ----------
  const VIEW_TEXT = {
    attention: ["Needs attention", "Your high-priority, blocked, overdue or quiet work, approvals waiting on you, and urgent work nobody owns."],
    mine: ["Assigned to me", "Everything you currently own."], approvals: ["Waiting for my approval", "Resolutions submitted by others that need a lead's decision."],
    unassigned: ["Unassigned", "Nobody owns these yet. Take one, or ask a lead to assign it."], overdue: ["Overdue", "Past their due date and still active."],
    stale: ["Gone quiet", "Active items with no activity for 3+ days. Nudge them before they're forgotten."], active: ["All active work", "Open, in progress, blocked or awaiting approval."],
    all: ["Everything", "Including resolved and closed."],
  };

  function listParams(cursor) {
    const p = new URLSearchParams({ view: S.view, sort: S.filters.sort, limit: "40" });
    for (const k of ["q", "team_id", "status", "priority"]) if (S.filters[k]) p.set(k, S.filters[k]);
    if (cursor) p.set("cursor", cursor);
    return p.toString();
  }

  async function loadList(reset) {
    const L = S.list;
    const seq = ++L.seq;                       // any response with an older seq is discarded
    if (reset) { L.items = []; L.cursor = null; L.hasMore = false; L.dirty = false; }
    L.loading = true; L.error = null; renderList();
    try {
      const r = await api("GET", "/items?" + listParams(reset ? null : L.cursor));
      if (seq !== L.seq) return;
      const seen = new Set(L.items.map((i) => i.id));
      L.items = L.items.concat(r.items.filter((i) => !seen.has(i.id)));
      L.cursor = r.next_cursor; L.hasMore = r.has_more;
    } catch (e) {
      if (seq !== L.seq) return;
      L.error = e.message;
    } finally {
      if (seq === L.seq) { L.loading = false; renderList(); }
    }
  }

  function signals(it) {
    const out = [];
    if (it.overdue) out.push(`<span class="sig bad">Overdue</span>`);
    if (it.status === "awaiting_approval") out.push(it.can.review ? `<span class="sig warn">Your approval needed</span>` : `<span class="sig warn">Awaiting approval</span>`);
    if (it.status === "blocked") out.push(`<span class="sig bad">Blocked</span>`);
    if (!it.assignee_id && ["open", "blocked"].includes(it.status)) out.push(`<span class="sig ${["high", "urgent"].includes(it.priority) ? "bad" : "info"}">Nobody owns this</span>`);
    if (it.stale) out.push(`<span class="sig warn">Quiet ${ago(it.last_activity_at).replace(" ago", "")}</span>`);
    return out.join("");
  }

  function rowHtml(it) {
    const done = ["resolved", "closed"].includes(it.status);
    return `<button class="row p-${it.priority} ${done ? "done" : ""} ${S.sel === it.id ? "sel" : ""} ${S.busy.has(it.id) ? "pending" : ""}" data-open="${it.id}">
      <div class="title">${esc(it.title)}</div>
      <div class="side">${dueText(it) || ""}<div>${ago(it.updated_at)}</div></div>
      <div class="meta"><span class="id">#${it.id}</span><span>${esc(it.team_name)}</span><span class="pill s-${it.status}">${cap(it.status)}</span>
        <span>${it.assignee_name ? esc(it.assignee_name) : "Unassigned"}</span>${signals(it)}</div></button>`;
  }

  function renderList() {
    const L = S.list, [title, hint] = VIEW_TEXT[S.view];
    $("#listhead").innerHTML = `<div><h2>${title}</h2><p>${hint}</p></div><p>${L.items.length}${L.hasMore ? "+" : ""} shown</p>`;
    $("#dirty").innerHTML = L.dirty ? `<div class="dirty"><span>There is new activity elsewhere in this list.</span><button class="btn small" data-action="refresh">Refresh list</button></div>` : "";
    const el = $("#list");
    let html = "";
    if (L.error) html = `<div class="empty"><b>Couldn't load work items</b>${esc(L.error)}<br><br><button class="btn" data-action="refresh">Try again</button></div>`;
    else if (!L.items.length && L.loading) html = '<div class="skeleton"></div>'.repeat(8);
    else if (!L.items.length) {
      html = S.filters.q || S.filters.status || S.filters.priority || S.filters.team_id
        ? `<div class="empty"><b>No matches</b>Nothing fits these filters. Clear a filter or try different words.<br><br><button class="btn" data-action="clear">Clear filters</button></div>`
        : S.view === "attention"
          ? `<div class="empty"><b>Nothing needs your attention</b>Browse <a href="#" data-view="active">all active work</a> or create a new item.</div>`
          : `<div class="empty"><b>Nothing here</b>No work items in this queue.</div>`;
    } else {
      html = L.items.map(rowHtml).join("");
      html += L.hasMore ? `<div class="more" id="sentinel">${L.loading ? "Loading more…" : '<button class="btn small" data-action="more">Load more</button>'}</div>` : "";
    }
    const top = el.scrollTop;
    el.innerHTML = html;
    el.scrollTop = top;
    observeSentinel();
  }

  let io;
  function observeSentinel() {
    io?.disconnect();
    const s = $("#sentinel");
    if (!s || !("IntersectionObserver" in window)) return;
    io = new IntersectionObserver((es) => { if (es[0].isIntersecting && !S.list.loading && S.list.hasMore) loadList(false); }, { root: $("#list"), rootMargin: "200px" });
    io.observe(s);
  }

  async function loadDash() {
    try { S.dash = await api("GET", "/dashboard"); renderRail(); } catch (_) { /* non-critical */ }
  }
  const loadDashSoon = debounce(loadDash, 400);

  // ---------- local item cache: one place that keeps list + detail consistent ----------
  function findItem(id) { return S.list.items.find((i) => i.id === id) || (S.detail?.id === id ? S.detail : null); }
  function putItem(item) {
    const i = S.list.items.findIndex((x) => x.id === item.id);
    if (i >= 0) S.list.items[i] = { ...S.list.items[i], ...stripFull(item) };
    if (S.detail?.id === item.id) S.detail = item.description !== undefined ? item : { ...S.detail, ...item };
  }
  const stripFull = (it) => { const { description, ...rest } = it; return rest; };

  // ---------- optimistic actions ----------
  /** Applies `patch` immediately, sends the request, then reconciles with whatever the server decided. */
  async function act(id, path, body, patch) {
    const before = findItem(id) ? clone(findItem(id)) : null;
    S.busy.add(id);
    if (patch && before) putItem({ ...before, ...patch });
    renderAll();
    try {
      const res = await api("POST", `/items/${id}/${path}`, { body, idem: uuid() });
      putItem(res.item);
      if (id === S.sel) loadEvents(true);
      loadDashSoon();
      return res;
    } catch (e) {
      if (before) putItem(before);                          // roll back the optimistic guess
      if (e.details?.current) putItem(e.details.current);   // ...then adopt the server's truth
      if (e.status === 409 && id === S.sel && e.code === "version_conflict") { S.remoteChange = e.details.last_change; }
      toast(e.message, "error");
      if (id === S.sel) loadEvents(true);
      throw e;
    } finally {
      S.busy.delete(id);
      renderAll();
    }
  }
  const ver = (id) => findItem(id)?.version;
  const claim = (id) => act(id, "claim", {}, { assignee_id: S.me.id, assignee_name: S.me.name, can: { ...findItem(id).can, claim: false, release: true, transitions: [] } }).catch(() => {});
  const release = (id) => act(id, "release", { expected_version: ver(id) }, { assignee_id: null, assignee_name: null }).catch(() => {});
  const transition = (id, to, extra = {}) => act(id, "transition", { to, expected_version: ver(id), ...extra }, { status: to }).then(() => { S.prompt = null; renderAll(); }).catch(() => {});

  async function patchItem(id, fields, base) {
    S.busy.add(id); renderAll();
    try {
      const res = await api("PATCH", `/items/${id}`, { body: { ...fields, expected_version: base ?? ver(id) }, idem: uuid() });
      putItem(res.item); loadEvents(true); loadDashSoon(); return res;
    } catch (e) {
      if (e.details?.current) putItem(e.details.current);
      throw e;
    } finally { S.busy.delete(id); renderAll(); }
  }

  // ---------- detail ----------
  async function openItem(id, push = true) {
    if (!id) return closeDetail();
    S.sel = id; S.editing = null; S.prompt = null; S.conflict = null; S.remoteChange = null; S.commentDraft = "";
    S.detail = findItem(id) ? { ...findItem(id) } : null;     // show what we have immediately
    S.events = []; S.eventsMore = null;
    if (push) history.pushState(null, "", `#/item/${id}`);
    renderAll();
    try {
      const r = await api("GET", `/items/${id}`);
      if (S.sel !== id) return;
      S.detail = r.item; putItem(r.item);
      loadMembers(r.item.team_id);
      loadEvents(true);
      renderAll();
    } catch (e) {
      if (S.sel !== id) return;
      toast(e.status === 404 ? "That work item doesn't exist or you don't have access to it." : e.message, "error");
      closeDetail();
    }
  }
  function closeDetail() {
    S.sel = null; S.detail = null; S.editing = null; S.prompt = null; S.conflict = null; S.remoteChange = null;
    if (location.hash) history.pushState(null, "", location.pathname);
    renderAll();
  }
  async function loadMembers(teamId) {
    if (S.members[teamId]) return;
    try { S.members[teamId] = (await api("GET", `/teams/${teamId}/members`)).members; renderDetail(); } catch (_) {}
  }
  async function loadEvents(reset) {
    const id = S.sel; if (!id) return;
    try {
      const r = await api("GET", `/items/${id}/events?limit=25` + (!reset && S.eventsMore ? `&before=${S.eventsMore}` : ""));
      if (S.sel !== id) return;
      S.events = reset ? r.events : S.events.concat(r.events);
      S.eventsMore = r.next_before; renderDetail();
    } catch (_) {}
  }

  function describe(ev) {
    const d = ev.data || {};
    switch (ev.type) {
      case "created": return "opened this item";
      case "assigned":
        if (d.via === "claim") return "took ownership";
        if (d.via === "removed_from_team") return "was unassigned (owner left the team)";
        return d.to ? `assigned it to <b>${esc(d.to_name || "someone")}</b>` : "released ownership";
      case "status_changed": return `moved it from ${label(d.from)} to <b>${label(d.to)}</b>${d.reason ? ` (${esc(d.reason)})` : ""}${d.resolution ? `<div class="quote">${esc(d.resolution)}</div>` : ""}`;
      case "approval_requested": return `asked for approval to resolve<div class="quote">${esc(d.resolution)}</div>`;
      case "approved": return `approved the resolution${d.note ? `<div class="quote">${esc(d.note)}</div>` : ""}`;
      case "rejected": return `sent it back to in progress<div class="quote">${esc(d.note)}</div>`;
      case "priority_changed": return `changed priority from ${esc(d.from)} to <b>${esc(d.to)}</b>`;
      case "updated": return `edited ${Object.keys(d.changes || {}).map((k) => esc(label(k))).join(", ")}`;
      case "comment": return `commented<div class="quote">${esc(d.body)}</div>`;
      default: return esc(ev.type);
    }
  }

  function renderDetail() {
    const el = $("#detail"); if (!el) return;
    const it = S.detail;
    if (!S.sel || !it) { el.hidden = true; return; }
    el.hidden = false;
    const can = it.can || { transitions: [] }, busy = S.busy.has(it.id), ed = S.editing;
    const members = (S.members[it.team_id] || []).filter((m) => m.role !== "viewer");
    const TLABEL = { in_progress: "Start work", blocked: "Mark blocked", open: ["resolved", "closed"].includes(it.status) ? "Reopen" : "Put back in queue", resolved: "Resolve…", awaiting_approval: "Submit for approval…", closed: "Close" };

    const actions = [];
    if (can.claim) actions.push(`<button class="btn primary" data-action="claim" ${busy ? "disabled" : ""}>Take this</button>`);
    for (const t of can.transitions || []) {
      actions.push(`<button class="btn ${t.to === "in_progress" ? "primary" : ""}" data-action="move" data-to="${t.to}" data-needs="${t.needs_resolution ? 1 : 0}" ${busy ? "disabled" : ""}>${TLABEL[t.to]}</button>`);
    }
    if (can.review) actions.push(`<button class="btn primary" data-action="review" data-kind="approve">Approve…</button><button class="btn danger" data-action="review" data-kind="reject">Send back…</button>`);
    if (can.release) actions.push(`<button class="btn quiet" data-action="release" ${busy ? "disabled" : ""}>Release ownership</button>`);

    let prompt = "";
    if (S.prompt) {
      const P = S.prompt;
      const text = P.kind === "move" ? (P.to === "resolved" ? "How was this resolved?" : "What should the approver check?") : P.kind === "approve" ? "Approval note (optional)" : "Why is this being sent back?";
      const go = P.kind === "move" ? (P.to === "resolved" ? "Resolve" : "Submit for approval") : P.kind === "approve" ? "Approve" : "Send back";
      prompt = `<div class="box"><label class="field">${text}<textarea id="prompt-text" maxlength="2000">${esc(P.text || "")}</textarea></label>
        <div class="actions"><button class="btn primary" data-action="prompt-go" ${busy ? "disabled" : ""}>${go}</button><button class="btn quiet" data-action="prompt-cancel">Cancel</button></div></div>`;
    }

    let banner = "";
    if (S.conflict) {
      const c = S.conflict;
      banner = `<div class="banner bad" role="alert"><div><b>${esc(c.who)}</b> changed this item while you were editing. Your draft is still below.</div>
        <div class="row-actions"><button class="btn small" data-action="rebase">Keep my edits on top of the latest</button><button class="btn small" data-action="discard-edit">Discard my edits</button></div></div>`;
    } else if (S.remoteChange && ed) {
      banner = `<div class="banner" role="status"><div>${esc(S.remoteChange.actor_name || "Someone")} updated this item while you were editing. Saving will ask you to reconcile.</div></div>`;
    }

    const factRows = `
      <dt>Owner</dt><dd>${it.assignee_name ? `<b>${esc(it.assignee_name)}</b>` : "<span>Unassigned</span>"}
        ${can.assign ? `<select data-action="assign" aria-label="Assign to"><option value="">Assign to…</option>${members.map((m) => `<option value="${m.id}" ${m.id === it.assignee_id ? "disabled" : ""}>${esc(m.name)} (${m.role})</option>`).join("")}</select>` : ""}</dd>
      <dt>Priority</dt><dd>${can.set_priority ? `<select data-action="priority" aria-label="Priority">${["low", "normal", "high", "urgent"].map((p) => `<option ${p === it.priority ? "selected" : ""}>${p}</option>`).join("")}</select>` : esc(cap(it.priority))}</dd>
      <dt>Due</dt><dd>${it.due_at ? `${full(it.due_at)} ${dueText(it) ? "· " + dueText(it) : ""}` : "No due date"}</dd>
      <dt>Opened by</dt><dd>${esc(it.creator_name)} · ${ago(it.created_at)}</dd>
      <dt>Approval</dt><dd>${it.requires_approval ? esc(cap(it.approval_state === "none" ? "required" : it.approval_state)) : "Not required"}</dd>
      <dt>Your role</dt><dd>${esc(it.my_role)} in ${esc(it.team_name)}</dd>`;

    const editor = ed ? `
      <div class="box"><label class="field">Title<input type="text" id="e-title" maxlength="200" value="${esc(ed.title)}"></label>
        <label class="field">Description<textarea id="e-desc" rows="6" maxlength="10000">${esc(ed.description)}</textarea></label>
        <label class="field">Due date<input type="date" id="e-due" value="${esc(ed.due)}"></label>
        <div class="actions"><button class="btn primary" data-action="save-edit" ${busy ? "disabled" : ""}>Save changes</button><button class="btn quiet" data-action="cancel-edit">Cancel</button></div></div>` : "";

    el.innerHTML = `
      <div class="d-top"><span><span class="id">#${it.id}</span> · ${esc(it.team_name)} · <span class="pill s-${it.status}">${cap(it.status)}</span>${busy ? " · saving…" : ""}</span>
        <span>${can.edit && !ed ? '<button class="btn small" data-action="edit">Edit</button> ' : ""}<button class="btn small quiet" data-action="close" aria-label="Close panel">Close</button></span></div>
      <div class="d-body">
        <div class="d-edge p-${it.priority}"><h2 class="d-title">${esc(it.title)}</h2><div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${signals(it)}</div></div>
        ${banner}
        ${actions.length ? `<div class="actions">${actions.join("")}</div>` : (can.comment ? "" : '<div class="box">You have read-only access to this team.</div>')}
        ${prompt}
        <dl class="facts">${factRows}</dl>
        ${editor}
        ${!ed ? `<div class="sec"><h3>What this is and why it exists</h3><div class="desc">${it.description ? esc(it.description) : '<span class="id">No description yet.</span>'}</div></div>` : ""}
        ${it.resolution ? `<div class="sec"><h3>Resolution</h3><div class="desc">${esc(it.resolution)}</div></div>` : ""}
        <div class="sec"><h3>Activity</h3>
          ${can.comment ? `<div class="box"><textarea id="c-text" placeholder="Add a comment" maxlength="5000">${esc(S.commentDraft)}</textarea><div class="actions"><button class="btn primary small" data-action="comment" ${S.commentBusy ? "disabled" : ""}>Comment</button></div></div>` : ""}
          <ul class="timeline">${S.events.map((ev) => `<li class="${ev.type === "comment" ? "c" : ""}"><span class="who">${esc(ev.actor_name)}</span><time title="${full(ev.created_at)}">${ago(ev.created_at)}</time><div class="chg">${describe(ev)}</div></li>`).join("")}</ul>
          ${S.eventsMore ? '<button class="btn small" data-action="older">Show older activity</button>' : ""}
        </div>
      </div>`;
  }

  // ---------- notifications ----------
  async function loadNotifs() {
    try {
      S.notifs = await api("GET", "/notifications");
      const b = $("#badge"); if (b) { b.hidden = !S.notifs.unread; b.textContent = S.notifs.unread; }
      if (!$("#notifs").hidden) renderNotifs();
    } catch (_) {}
  }
  function renderNotifs() {
    $("#notifs").innerHTML = `<header>Alerts <button class="btn small quiet" data-action="read-all">Mark all read</button></header>` +
      (S.notifs.notifications.length ? S.notifs.notifications.map((n) => `<button class="nf ${n.read_at ? "" : "unread"}" data-nf="${n.id}" data-item="${n.item_id || ""}">${esc(n.message)}<small>${ago(n.created_at)}</small></button>`).join("") : '<div class="empty">No alerts yet.</div>');
  }
  function toggleNotifs() { const p = $("#notifs"); p.hidden = !p.hidden; if (!p.hidden) { renderNotifs(); } }

  // ---------- new item dialog ----------
  function openNew() {
    const writable = S.me.teams.filter((t) => t.role !== "viewer");
    if (!writable.length) return toast("You only have read access, so you can't create work items.", "error");
    S.newKey = uuid();                                    // one key per dialog session: double-clicks & retries are safe
    const d = $("#newdlg");
    d.innerHTML = `<form method="dialog" id="newform">
      <h2>New work item</h2><div id="newerr"></div>
      <label class="field">Title<input type="text" name="title" maxlength="200" required placeholder="What needs investigating or doing?"></label>
      <label class="field">Why does it exist?<textarea name="description" maxlength="10000" placeholder="Context, links, who is affected"></textarea></label>
      <div class="two"><label class="field">Team<select name="team_id">${writable.map((t) => `<option value="${t.id}" ${String(t.id) === String(S.filters.team_id) ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select></label>
        <label class="field">Priority<select name="priority"><option>low</option><option selected>normal</option><option>high</option><option>urgent</option></select></label></div>
      <label class="field">Due date (optional)<input type="date" name="due_at"></label>
      <label class="check"><input type="checkbox" name="requires_approval"><span>Needs a lead's approval before it can be resolved</span></label>
      <div class="foot"><button type="button" class="btn quiet" data-action="new-cancel">Cancel</button><button class="btn primary" type="submit" id="newgo">Create work item</button></div></form>`;
    d.showModal();
    $("#newform", d).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target), btn = $("#newgo");
      btn.disabled = true; btn.textContent = "Creating…";
      try {
        const r = await api("POST", "/items", { idem: S.newKey, body: {
          title: f.get("title"), description: f.get("description"), team_id: Number(f.get("team_id")), priority: f.get("priority"),
          due_at: f.get("due_at") || null, requires_approval: f.get("requires_approval") === "on" } });
        d.close(); toast(`Created #${r.item.id}`, "ok");
        await Promise.all([loadList(true), loadDash()]);
        openItem(r.item.id);
      } catch (err) {
        if (err.code === "idempotency_key_reuse") S.newKey = uuid();
        $("#newerr", d).innerHTML = `<div class="err" role="alert">${esc(err.message)}</div>`;
        btn.disabled = false; btn.textContent = "Create work item";
      }
    });
  }

  // ---------- change feed polling ----------
  let timer = null;
  function startPolling() { stopPolling(); timer = setInterval(poll, 5000); }
  function stopPolling() { clearInterval(timer); timer = null; }
  let polling = false;
  async function poll() {
    if (document.hidden || !S.token || polling) return;
    polling = true;
    try {
      const r = await api("GET", "/changes?since=" + S.cursor);
      setLive(true);
      S.cursor = r.cursor;
      if (r.reset) { await Promise.all([loadList(true), loadDash(), S.sel ? openItem(S.sel, false) : null]); }
      else if (r.item_ids.length) await onChanged(r.item_ids);
      loadNotifs();
    } catch (_) { setLive(false); } finally { polling = false; }
  }
  function setLive(ok) { const l = $("#live"); if (l) { l.classList.toggle("off", !ok); l.textContent = ok ? "Live" : "Reconnecting…"; } }

  async function onChanged(ids) {
    const visible = ids.filter((id) => S.list.items.some((i) => i.id === id) || id === S.sel);
    if (ids.some((id) => !visible.includes(id)) && S.view !== "all") S.list.dirty = true;
    if (visible.length) {
      try {
        const r = await api("GET", "/items?view=all&ids=" + visible.join(",") + "&limit=100");
        for (const it of r.items) {
          if (S.busy.has(it.id)) continue;                // don't clobber an in-flight optimistic update
          const prev = findItem(it.id);
          const changed = !prev || prev.version !== it.version;
          if (it.id === S.sel && S.detail) {
            const wasEditing = S.editing && it.version > S.editing.base;
            if (changed && !wasEditing) { const f = await api("GET", `/items/${it.id}`); S.detail = f.item; }
            if (wasEditing) S.remoteChange = { actor_name: "Someone" };
          }
          putItem(it);
        }
      } catch (_) {}
    }
    if (S.sel && ids.includes(S.sel)) {
      await loadEvents(true);
      if (S.editing && S.remoteChange) {
        const ev = S.events.find((e) => e.actor_id !== S.me.id);
        if (ev) S.remoteChange = { actor_name: ev.actor_name };
      }
    }
    loadDashSoon(); renderAll();
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && S.token) poll(); });

  // ---------- events ----------
  function renderAll() { if (!S.me) return; renderList(); renderDetail(); $("#app")?.classList.toggle("has-detail", !!S.sel); }

  function readEditDraft() {
    if (!S.editing) return;
    S.editing.title = $("#e-title")?.value ?? S.editing.title;
    S.editing.description = $("#e-desc")?.value ?? S.editing.description;
    S.editing.due = $("#e-due")?.value ?? S.editing.due;
  }

  async function saveEdit() {
    readEditDraft();
    const ed = S.editing, it = S.detail;
    if (!ed.title.trim()) return toast("Title can't be empty.", "error");
    const fields = {};
    if (ed.title.trim() !== it.title) fields.title = ed.title.trim();
    if (ed.description !== (it.description || "")) fields.description = ed.description;
    const dueNow = it.due_at ? it.due_at.slice(0, 10) : "";
    if (ed.due !== dueNow) fields.due_at = ed.due || null;
    if (!Object.keys(fields).length) { S.editing = null; return renderDetail(); }
    try {
      await patchItem(it.id, fields, ed.base);            // pinned to the version the form was opened on
      S.editing = null; S.conflict = null; S.remoteChange = null; toast("Saved", "ok");
    } catch (e) {
      if (e.code === "version_conflict") {
        S.conflict = { who: e.details.last_change?.actor_name || "Someone", latest: e.details.current };
        toast("Someone else changed this item first. Nothing was overwritten.", "error");
      } else toast(e.message, "error");
    }
    renderDetail();
  }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-action],[data-open],[data-view],[data-team],[data-nf]");
    if (!t) { if (!e.target.closest("#notifs, #bell")) { const p = $("#notifs"); if (p) p.hidden = true; } return; }
    const d = t.dataset, id = S.sel;
    if (d.open) { e.preventDefault(); return openItem(Number(d.open)); }
    if (d.view) { e.preventDefault(); S.view = d.view; renderRail(); return loadList(true); }
    if (d.team) { S.filters.team_id = String(S.filters.team_id) === d.team ? "" : d.team; $("#f-team").value = S.filters.team_id; renderRail(); return loadList(true); }
    if (d.nf) {
      api("POST", "/notifications/read", { body: { ids: [Number(d.nf)] } }).then(loadNotifs).catch(() => {});
      $("#notifs").hidden = true;
      if (d.item) openItem(Number(d.item));
      return;
    }
    switch (d.action) {
      case "new": return openNew();
      case "new-cancel": return $("#newdlg").close();
      case "signout": return signOut(false);
      case "refresh": return Promise.all([loadList(true), loadDash()]);
      case "more": return loadList(false);
      case "clear":
        S.filters = { ...S.filters, q: "", team_id: "", status: "", priority: "" };
        $("#q").value = ""; ["team", "status", "priority"].forEach((k) => ($("#f-" + k).value = ""));
        renderRail(); return loadList(true);
      case "close": return closeDetail();
      case "claim": return claim(id);
      case "release": return release(id);
      case "read-all": await api("POST", "/notifications/read", { body: {} }); return loadNotifs();
      case "older": return loadEvents(false);
      case "move":
        if (d.needs === "1") { S.prompt = { kind: "move", to: d.to, text: "" }; return renderDetail(); }
        return transition(id, d.to);
      case "review": S.prompt = { kind: d.kind, text: "" }; return renderDetail();
      case "prompt-cancel": S.prompt = null; return renderDetail();
      case "prompt-go": {
        const P = S.prompt; P.text = $("#prompt-text").value.trim();
        if (P.kind === "move") { if (!P.text) return toast("Please write a short note first.", "error"); return transition(id, P.to, { resolution: P.text }); }
        if (P.kind === "reject" && !P.text) return toast("Please explain why it's being sent back.", "error");
        return act(id, P.kind === "approve" ? "approve" : "reject", { expected_version: ver(id), note: P.text || undefined },
          { status: P.kind === "approve" ? "resolved" : "in_progress" }).then(() => { S.prompt = null; renderAll(); }).catch(() => {});
      }
      case "edit":
        S.editing = { base: S.detail.version, title: S.detail.title, description: S.detail.description || "", due: S.detail.due_at ? S.detail.due_at.slice(0, 10) : "" };
        S.remoteChange = null; return renderDetail();
      case "cancel-edit": S.editing = null; S.conflict = null; S.remoteChange = null; return renderDetail();
      case "discard-edit": S.editing = null; S.conflict = null; S.remoteChange = null; return renderDetail();
      case "rebase": readEditDraft(); S.conflict = null; return saveEditAfterRebase();
      case "save-edit": return saveEdit();
      case "comment": {
        const text = ($("#c-text").value || "").trim(); if (!text) return;
        S.commentBusy = true; renderDetail();
        try { await api("POST", `/items/${id}/comments`, { body: { body: text }, idem: uuid() }); S.commentDraft = ""; await loadEvents(true); }
        catch (err) { toast(err.message, "error"); S.commentDraft = text; }
        finally { S.commentBusy = false; renderAll(); }
        return;
      }
    }
  });

  // After "keep my edits on top": adopt the latest server copy as the base, keep the user's draft, let them review and save again.
  async function saveEditAfterRebase() {
    const latest = await api("GET", `/items/${S.sel}`);
    S.detail = latest.item; putItem(latest.item);
    S.editing.base = latest.item.version;
    toast("Your draft now sits on top of the latest version. Review it and save.", "ok");
    renderDetail();
  }

  document.addEventListener("change", async (e) => {
    const t = e.target, id = S.sel;
    if (t.dataset.action === "assign" && t.value) {
      const uid = Number(t.value);
      const m = (S.members[S.detail.team_id] || []).find((x) => x.id === uid);
      act(id, "assign", { assignee_id: uid, expected_version: ver(id) }, { assignee_id: uid, assignee_name: m?.name }).catch(() => {});
    }
    if (t.dataset.action === "priority") {
      const prev = S.detail.priority, p = t.value;
      putItem({ ...S.detail, priority: p }); renderAll();
      try { await patchItem(id, { priority: p }); }
      catch (err) { toast(err.message, "error"); putItem({ ...S.detail, priority: prev }); }
      renderAll();
    }
  });
  document.addEventListener("input", (e) => {
    if (e.target.id === "c-text") S.commentDraft = e.target.value;
    if (S.editing && ["e-title", "e-desc", "e-due"].includes(e.target.id)) readEditDraft();
  });
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
    if (e.key === "/" && !typing && S.me) { e.preventDefault(); $("#q")?.focus(); }
    if (e.key === "Escape" && !typing && S.sel) closeDetail();
  });
  window.addEventListener("popstate", () => {
    const m = location.hash.match(/^#\/item\/(\d+)/);
    m ? openItem(Number(m[1]), false) : (S.sel && closeDetail());
  });

  // ---------- boot ----------
  async function boot() {
    try { S.me = S.me || (await api("GET", "/me")); }
    catch (_) { return renderLogin(); }
    renderShell();
    const c = await api("GET", "/changes");
    S.cursor = c.cursor;
    await Promise.all([loadDash(), loadList(true), loadNotifs()]);
    const m = location.hash.match(/^#\/item\/(\d+)/);
    if (m) openItem(Number(m[1]), false);
    startPolling();
  }
  if (S.token) boot(); else renderLogin();
})();
