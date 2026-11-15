/* Invoice Desk front end. No framework, no build step: one file that renders five views
   and replays a run's event stream, live or after the fact. */
(() => {
  "use strict";

  // ------------------------------------------------------------ helpers ---
  const view = document.getElementById("view");
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Money is shown in the invoice's own currency; purchase orders are always in rupees.
  let CUR = "INR";
  function inr(x, cur = CUR, places = 2) {
    if (x === null || x === undefined || Number.isNaN(Number(x))) return "—";
    const n = Number(x);
    if (cur && cur !== "INR") {
      return (n < 0 ? "-" : "") + cur + " " + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: places, maximumFractionDigits: places });
    }
    const [whole, frac] = Math.abs(n).toFixed(places).split(".");
    let s = whole;
    if (whole.length > 3) {
      const tail = whole.slice(-3);
      let head = whole.slice(0, -3);
      const parts = [];
      while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) parts.unshift(head);
      s = parts.join(",") + "," + tail;
    }
    return (n < 0 ? "-" : "") + "₹" + s + "." + frac;
  }
  const rupees = (x) => inr(x, "INR");
  // unit prices: two decimals, or four when the rate needs them (0.023)
  const rate = (x, cur = CUR) => (x === null || x === undefined ? "—" : inr(x, cur, Math.abs(x * 100 - Math.round(x * 100)) > 1e-6 ? 4 : 2));
  function inrShort(x) {
    const n = Number(x || 0);
    if (n >= 1e7) return "₹" + (n / 1e7).toFixed(2) + " Cr";
    if (n >= 1e5) return "₹" + (n / 1e5).toFixed(2) + " L";
    return rupees(n).replace(/\.00$/, "");
  }
  const qty = (x) => (x === null || x === undefined ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: 4 }));
  function fdate(iso) {
    if (!iso) return "—";
    const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }
  function ftime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  function fwhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + ", " + ftime(iso);
  }
  function dur(ms) {
    if (ms === null || ms === undefined) return "";
    if (ms < 1) return "<1 ms";
    if (ms < 1000) return Math.round(ms) + " ms";
    return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + " s";
  }
  const pct = (x) => (x === null || x === undefined ? "—" : Math.round(x * 100) + "%");

  const svg = (d, size = 14, sw = 2.2) =>
    `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICON = {
    check: (s) => svg('<path d="m5 12.500 4.500 4.500L19 7.500"/>', s),
    x: (s) => svg('<path d="M6 6l12 12M18 6 6 18"/>', s),
    alert: (s) => svg('<path d="M12 7v6"/><path d="M12 17h.01"/>', s, 2.6),
    info: (s) => svg('<path d="M12 11v6"/><path d="M12 7h.01"/>', s, 2.6),
    dash: (s) => svg('<path d="M7 12h10"/>', s),
    caret: (s) => svg('<path d="m9 6 6 6-6 6"/>', s),
    back: (s) => svg('<path d="M15 6l-6 6 6 6"/>', s),
    upload: (s) => svg('<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>', s, 2),
    file: (s) => svg('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>', s, 2),
    play: (s) => svg('<path d="M7 5v14l12-7z"/>', s, 2),
    copy: (s) => svg('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>', s, 2),
    ext: (s) => svg('<path d="M14 5h5v5"/><path d="M19 5 10 14"/><path d="M19 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h4"/>', s, 2),
    clock: (s) => svg('<circle cx="12" cy="12" r="8.500"/><path d="M12 7.500V12l3 2"/>', s, 2),
    user: (s) => svg('<circle cx="12" cy="8" r="3.500"/><path d="M5 20a7 7 0 0 1 14 0"/>', s, 2),
    mail: (s) => svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m4 7 8 6 8-6"/>', s, 2),
    ban: (s) => svg('<circle cx="12" cy="12" r="8.500"/><path d="m6 6 12 12"/>', s, 2),
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    let body = null;
    try { body = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const err = new Error((body && body.detail) || `Request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function toast(msg, isError = false) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " err" : "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), isError ? 6000 : 3200);
  }

  function confirmModal({ title, body, confirm, danger }) {
    return new Promise((resolve) => {
      const m = $("#modal");
      m.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <h2 id="modal-title">${esc(title)}</h2><p>${esc(body)}</p>
        <div class="modal-actions">
          <button class="btn" data-x="no" type="button">Cancel</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-x="yes" type="button">${esc(confirm)}</button>
        </div></div>`;
      m.hidden = false;
      const done = (v) => { m.hidden = true; m.innerHTML = ""; m.onclick = null; document.removeEventListener("keydown", onKey); resolve(v); };
      const onKey = (e) => { if (e.key === "Escape") done(false); };
      document.addEventListener("keydown", onKey);
      m.onclick = (e) => {
        if (e.target === m) return done(false);
        const b = e.target.closest("[data-x]");
        if (b) done(b.dataset.x === "yes");
      };
      $('[data-x="yes"]', m).focus();
    });
  }

  // --------------------------------------------------------------- meta ---
  let META = null;
  const LABELS = { APPROVED: "Approved for payment", NEEDS_REVIEW: "Held for review", RETURNED: "Returned to vendor", REJECTED: "Rejected" };
  const SHORT = { APPROVED: "Approved", NEEDS_REVIEW: "Held for review", RETURNED: "Returned", REJECTED: "Rejected" };
  const DCLS = { APPROVED: "approved", NEEDS_REVIEW: "review", RETURNED: "returned", REJECTED: "rejected" };
  const DICON = { APPROVED: ICON.check, NEEDS_REVIEW: ICON.user, RETURNED: ICON.mail, REJECTED: ICON.ban };

  async function loadMeta(force = false) {
    if (!META || force) META = await api("/api/meta");
    $("#foot-buyer").textContent = META.buyer.short_name;
    $("#foot-reader").innerHTML = META.ai.enabled
      ? `<span class="dot on"></span>Claude (${esc(META.ai.model)})`
      : `<span class="dot"></span>Built-in parser<br><span class="faint">Add an API key to read with Claude</span>`;
    return META;
  }

  async function refreshBadge() {
    try {
      const s = await api("/api/stats");
      const b = $("#nav-pending");
      b.hidden = !s.pending_review;
      b.textContent = s.pending_review;
      b.title = `${s.pending_review} waiting on a person`;
    } catch (_) { /* cosmetic */ }
  }

  const getPace = () => (localStorage.getItem("pace") === "0" ? 0 : 450);

  function outcomeChip(r) {
    if (r.status === "queued") return `<span class="chip running">Queued</span>`;
    if (r.status === "running") return `<span class="chip running"><span class="spinner" style="width:10px;height:10px;border-width:1.5px"></span>Processing</span>`;
    if (r.status === "error") return `<span class="chip rejected">Error</span>`;
    if (r.decision === "NEEDS_REVIEW" && r.review_status === "approved") return `<span class="chip approved">${ICON.check(12)}Approved by reviewer</span>`;
    if (r.decision === "NEEDS_REVIEW" && r.review_status === "rejected") return `<span class="chip rejected">${ICON.x(12)}Rejected by reviewer</span>`;
    return `<span class="chip ${DCLS[r.decision] || ""}">${(DICON[r.decision] || ICON.dash)(12)}${esc(SHORT[r.decision] || r.decision || "—")}</span>`;
  }

  // ------------------------------------------------------------- router ---
  let routeToken = 0;
  let cleanup = null;
  const ROUTES = [
    [/^#\/?$/, "process", viewProcess],
    [/^#\/runs\/?$/, "runs", viewRuns],
    [/^#\/runs\/(RUN-\d+)$/, "runs", viewRun],
    [/^#\/pos\/?$/, "pos", viewPOs],
    [/^#\/policy\/?$/, "policy", viewPolicy],
  ];

  async function route() {
    if (cleanup) { try { cleanup(); } catch (_) { /* ignore */ } cleanup = null; }
    const token = ++routeToken;
    const alive = () => token === routeToken;
    const hash = location.hash || "#/";
    const hit = ROUTES.map(([rx, nav, fn]) => ({ m: hash.match(rx), nav, fn })).find((r) => r.m);
    if (!hit) { location.hash = "#/"; return; }
    $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === hit.nav));
    refreshBadge();
    try {
      await loadMeta();
      const c = await hit.fn(alive, ...hit.m.slice(1));
      if (alive()) cleanup = c || null; else if (c) c();
    } catch (err) {
      if (!alive()) return;
      view.innerHTML = `<div class="page"><div class="card"><div class="empty"><div class="empty-title">Something went wrong</div><div>${esc(err.message)}</div><a class="btn btn-sm" href="#/">Back to the inbox</a></div></div></div>`;
    }
    window.scrollTo(0, 0);
  }

  // ======================================================== process view ===
  const AVATAR_COLORS = ["#3451e0", "#0b7a4b", "#c2410c", "#5a45c9", "#0f766e", "#b3261e", "#475569"];
  function avatar(name) {
    const initials = name.split(/\s+/).filter((w) => /^[A-Za-z]/.test(w)).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return `<span class="avatar" style="background:${AVATAR_COLORS[h % AVATAR_COLORS.length]}">${esc(initials)}</span>`;
  }

  async function startRun(form) {
    form.append("pace_ms", String(getPace()));
    const res = await api("/api/runs", { method: "POST", body: form });
    location.hash = `#/runs/${res.run_id}`;
  }

  async function viewProcess(alive) {
    const meta = await loadMeta(true);
    if (!alive()) return;
    const pace = getPace();
    const rows = meta.samples.map((s) => {
      const last = s.last_run;
      const lastChip = last
        ? `<a href="#/runs/${esc(last.run_id)}" title="Open the last run of this invoice">${outcomeChip(last)}</a>`
        : "";
      return `<div class="inbox-row">
        ${avatar(s.sender)}
        <div class="inbox-main">
          <div class="inbox-line1"><span class="inbox-sender">${esc(s.sender)}</span><span class="inbox-time">${esc(s.received)}</span></div>
          <div class="inbox-subject">${esc(s.subject)}</div>
          <div class="inbox-tests"><b>${esc(s.title)}.</b> ${esc(s.tests)}</div>
          <div class="inbox-meta">
            <a class="attach" href="/api/samples/${esc(s.key)}/file" target="_blank" rel="noopener">${ICON.file(13)}${esc(s.file)}</a>
            <span class="chip ${esc(s.kind)}">${esc(s.tag)}</span>
            ${lastChip}
          </div>
        </div>
        <div class="inbox-actions"><button class="btn btn-primary btn-sm" data-sample="${esc(s.key)}" type="button">${ICON.play(12)}Process</button></div>
      </div>`;
    }).join("");
    const steps = meta.stages.map((s, i) => `<div class="howto-step"><span class="howto-n">${i + 1}</span><div><div class="howto-t">${esc(s.title)}</div><div class="howto-d">${esc(s.blurb)}</div></div></div>`).join("");

    view.innerHTML = `<div class="page">
      <div class="page-head">
        <div>
          <h1 class="page-title">Process an invoice</h1>
          <p class="page-sub">Give the desk a vendor invoice. It reads the PDF, checks it against the vendor master and the purchase order register, and returns one of four decisions with its reasoning.</p>
        </div>
      </div>
      <div class="process-top">
        <label class="dropzone" id="dropzone" tabindex="0">
          <input type="file" id="file-input" accept="application/pdf,.pdf" hidden>
          <span class="dropzone-icon">${ICON.upload(22)}</span>
          <span class="dropzone-title">Drop an invoice PDF here</span>
          <span class="dropzone-sub">or click to choose a file. Digital PDFs and scans both work, up to 15 MB.</span>
        </label>
        <div class="card howto">
          <h2 class="section-title">What happens to it</h2>
          <div class="howto-grid">${steps}</div>
          <div class="howto-note">The reader extracts fields. Every decision after that is made by fixed rules you can see and change on the <a href="#/policy">Policy</a> page, so the same invoice always gets the same answer.</div>
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <div><div class="card-title">AP inbox</div><div class="muted" style="font-size:12.5px">${meta.samples.length} invoices waiting, prepared to exercise the happy path and each edge case</div></div>
          <div class="page-actions">
            <span class="muted" style="font-size:12.5px">Speed</span>
            <div class="seg" id="pace-seg" role="group" aria-label="Run speed">
              <button type="button" data-pace="450" class="${pace ? "on" : ""}" title="Each stage is held on screen for about half a second so you can follow it">Watchable</button>
              <button type="button" data-pace="0" class="${pace ? "" : "on"}" title="No pauses: stages finish as fast as they compute">Full speed</button>
            </div>
            <button class="btn btn-sm" id="batch-btn" type="button">Process whole inbox</button>
          </div>
        </div>
        <div>${rows}</div>
      </div>
    </div>`;

    const dz = $("#dropzone");
    const input = $("#file-input");
    const upload = async (file) => {
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      try { await startRun(form); } catch (err) { toast(err.message, true); }
    };
    input.addEventListener("change", () => upload(input.files[0]));
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
    dz.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));

    $("#pace-seg").addEventListener("click", (e) => {
      const b = e.target.closest("[data-pace]");
      if (!b) return;
      localStorage.setItem("pace", b.dataset.pace);
      $$("#pace-seg button").forEach((x) => x.classList.toggle("on", x === b));
    });
    $(".page", view).addEventListener("click", async (e) => {
      const b = e.target.closest("[data-sample]");
      if (!b || b.disabled) return;
      b.disabled = true;
      const form = new FormData();
      form.append("sample_key", b.dataset.sample);
      try { await startRun(form); } catch (err) { toast(err.message, true); b.disabled = false; }
    });
    $("#batch-btn").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      try {
        const form = new FormData();
        form.append("pace_ms", "120");
        await api("/api/runs/batch", { method: "POST", body: form });
        location.hash = "#/runs";
      } catch (err) { toast(err.message, true); e.currentTarget.disabled = false; }
    });
  }

  // ============================================================ run view ===
  const FIELD_BOX = {
    vendor_name: ["Vendor", "id"], vendor_gstin: ["Vendor tax ID", "id"], invoice_number: ["Invoice number", "id"],
    invoice_date: ["Invoice date", "id"], due_date: ["Due date", "id"], po_number: ["PO reference", "id"],
    subtotal: ["Subtotal", "amt"], tax_amount: ["Tax", "amt"], total: ["Total", "amt"],
    amount_in_words: ["Amount in words", "amt"], bank_account: ["Bank account", "bank"],
  };
  const ACTION_TAG = { REVIEW: "Holds the invoice for a person", RETURN: "Sends the invoice back to the vendor", REJECT: "Rejects the invoice" };
  const ROUTE_NAME = { treasury: "Treasury & Controls", procurement: "Vendor Master desk", buyer: "the PO owner", ap: "the AP lead" };

  function applyEvent(st, e) {
    st.lastSeq = Math.max(st.lastSeq, e.seq);
    const p = e.payload || {};
    if (e.type === "run_started") {
      st.startedAt = Date.parse(e.ts);
      st.info = p;
    } else if (e.type === "stage_started") {
      st.stages[e.stage] = { status: "running", logs: [], done: false };
      st.current = e.stage;
    } else if (e.type === "stage_log") {
      if (st.stages[e.stage]) st.stages[e.stage].logs.push(p.text);
    } else if (e.type === "stage_completed") {
      st.stages[e.stage] = Object.assign(st.stages[e.stage] || { logs: [] }, p, { done: true });
      st.fresh = e.stage;
      if (st.live && !st.userTab) {
        if (e.stage === "read") st.tab = "document";
        if (e.stage === "po_match") st.tab = "matching";
      }
    } else if (e.type === "run_completed") {
      st.decision = p.decision;
      st.durationMs = p.duration_ms;
      st.computeMs = p.compute_ms;
      st.finished = true;
      st.current = null;
      if (st.live && !st.userTab) st.tab = "outcome";
    } else if (e.type === "run_failed") {
      st.error = p.error;
      st.finished = true;
      st.current = null;
    } else if (e.type === "review") {
      st.review = Object.assign({ ts: e.ts }, p);
    }
  }

  function checkHtml(c) {
    const ic = { pass: ICON.check(11), info: ICON.info(11), warn: ICON.alert(11), fail: ICON.x(10) }[c.status] || ICON.dash(11);
    const tag = c.action ? `<span class="check-tag ${esc(c.action)}">${esc(ACTION_TAG[c.action])}${c.action === "REVIEW" && c.route ? ": " + esc(ROUTE_NAME[c.route] || c.route) : ""}</span>` : "";
    return `<li class="check ${esc(c.status)}"><span class="check-ic">${ic}</span><div><div class="check-title">${esc(c.title)}</div>${c.detail ? `<div class="check-detail">${esc(c.detail)}</div>` : ""}${tag}</div></li>`;
  }

  function pipelineHtml(st) {
    return META.stages.map((def, i) => {
      const s = st.stages[def.id];
      const status = !s ? "pending" : s.done ? s.status : "running";
      const defaultOpen = status === "running" || status === "warn" || status === "fail";
      const open = st.open[def.id] === undefined ? defaultOpen : st.open[def.id];
      const icon = { pending: String(i + 1), running: '<span class="spinner"></span>', ok: ICON.check(13), warn: ICON.alert(13), fail: ICON.x(12), skipped: ICON.dash(13) }[status];
      let summary = def.blurb;
      if (status === "running") summary = (s.logs.length ? s.logs[s.logs.length - 1] : def.blurb + "…");
      else if (s && s.done) summary = s.summary;
      let body = "";
      if (s && open) {
        const logs = (s.logs || []).map((l) => `<div class="stage-log">${esc(l)}</div>`).join("");
        const checks = s.done && s.checks && s.checks.length ? `<ul class="checks">${s.checks.map(checkHtml).join("")}</ul>` : "";
        if (logs || checks) body = `<div class="stage-body">${logs ? `<div style="margin-bottom:${checks ? 8 : 0}px">${logs}</div>` : ""}${checks}</div>`;
      }
      const fresh = st.fresh === def.id ? " fresh" : "";
      return `<div class="stage ${status}${open && s ? " open" : ""}${fresh}" data-stage="${esc(def.id)}">
        <button class="stage-head" type="button" ${s ? "" : "disabled"} aria-expanded="${open && !!s}">
          <span class="stage-icon">${icon}</span>
          <span><span class="stage-name">${esc(def.title)}</span><span class="stage-summary" style="display:block">${esc(summary)}</span></span>
          <span class="stage-ms num">${s && s.done && status !== "skipped" ? esc(dur(s.ms)) : ""}</span>
          <span class="stage-caret">${s ? ICON.caret(13) : ""}</span>
        </button>${body}</div>`;
    }).join("");
  }

  function bannerHtml(st) {
    const ex = (st.stages.extract && st.stages.extract.data) || null;
    const f = ex ? ex.fields : {};
    if (st.error) {
      return `<div class="banner rejected"><div class="banner-row"><span class="banner-icon">${ICON.x(20)}</span><div class="banner-main"><div class="banner-label">Run failed</div><div class="banner-headline">${esc(st.error)}</div></div></div></div>`;
    }
    if (!st.finished) {
      const doneCount = META.stages.filter((d) => st.stages[d.id] && st.stages[d.id].done).length;
      const cur = META.stages.find((d) => d.id === st.current);
      const queued = !st.startedAt;
      return `<div class="banner"><div class="banner-row">
        <span class="banner-icon" style="background:var(--accent-soft);color:var(--accent)"><span class="spinner" style="width:18px;height:18px"></span></span>
        <div class="banner-main">
          <div class="banner-label">${queued ? "Queued" : `Stage ${Math.min(doneCount + 1, META.stages.length)} of ${META.stages.length}`}</div>
          <div class="banner-headline">${queued ? "Waiting for the run ahead of it to finish…" : esc(cur ? cur.title + ": " + cur.blurb.toLowerCase() : "Working…")}</div>
          <div class="progress"><i style="width:${(doneCount / META.stages.length) * 100}%"></i></div>
        </div>
        <div class="banner-amount"><div class="v num" id="elapsed">0.0 s</div><div class="l">elapsed</div></div>
      </div></div>`;
    }
    const d = st.decision;
    const cls = DCLS[d.decision];
    const total = st.run.total ?? (st.stages.validate && st.stages.validate.data ? st.stages.validate.data.total : null);
    const facts = [];
    const vendorName = (st.stages.vendor && st.stages.vendor.data && st.stages.vendor.data.vendor && st.stages.vendor.data.vendor.name) || f.vendor_name;
    if (vendorName) facts.push(`<span class="chip plain">${esc(vendorName)}</span>`);
    if (f.invoice_number) facts.push(`<span class="chip plain mono">${esc(f.invoice_number)}</span>`);
    const pm = st.stages.po_match && st.stages.po_match.data;
    if (pm && pm.po) facts.push(`<span class="chip plain">${esc(pm.po.po_number)} · ${pm.method === "inferred" ? "inferred " + pct(pm.confidence) : "quoted on invoice"}</span>`);
    facts.push(`<span class="chip plain">${d.passed_count} checks passed</span>`);
    if (ex) facts.push(`<span class="chip plain">Read by ${ex.method === "ai" ? "Claude" : "built-in parser"}${ex.confidence != null ? " · " + pct(ex.confidence) : ""}</span>`);
    let timing = `Decided in ${dur(st.computeMs)} of compute`;
    if (st.info && st.info.pace_ms && st.durationMs > st.computeMs + 300) timing += ` · shown at watchable pace (${dur(st.durationMs)})`;
    facts.push(`<span class="chip plain" title="Compute time is the work itself. Watchable pace adds a short pause per stage so a viewer can follow.">${ICON.clock(12)}${esc(timing)}</span>`);
    return `<div class="banner ${cls}"><div class="banner-row">
      <span class="banner-icon">${DICON[d.decision](20)}</span>
      <div class="banner-main">
        <div class="banner-label">${esc(d.label)}</div>
        <div class="banner-headline">${esc(d.headline)}</div>
        <div class="banner-facts">${facts.join("")}</div>
        ${reviewedHtml(st)}
      </div>
      ${total != null ? `<div class="banner-amount"><div class="v num">${inr(total)}</div><div class="l">invoice total</div></div>` : ""}
    </div></div>`;
  }

  function reviewedHtml(st) {
    const r = st.review;
    if (!r) return "";
    if (r.action === "approve") {
      const posted = r.posted
        ? `Posted to the ledger; ${inr(r.amount)} scheduled for ${fdate(r.pay_date)}. ${esc(r.po_progress || "")}`
        : esc(r.note_system || "Not posted.");
      return `<div class="reviewed ok"><b>Approved by ${esc(r.by)}</b>${r.role ? ` <span class="muted">(${esc(r.role)})</span>` : ""} · ${esc(fwhen(r.ts))}. ${posted}${r.note ? `<div class="muted" style="margin-top:4px">Note: “${esc(r.note)}”</div>` : ""}</div>`;
    }
    return `<div class="reviewed no"><b>Rejected by ${esc(r.by)}</b>${r.role ? ` <span class="muted">(${esc(r.role)})</span>` : ""} · ${esc(fwhen(r.ts))}. Nothing was posted.${r.note ? `<div class="muted" style="margin-top:4px">Note: “${esc(r.note)}”</div>` : ""}</div>`;
  }

  // ---- tab: document
  function documentTab(st) {
    const rd = st.stages.read && st.stages.read.done ? st.stages.read.data : null;
    if (!rd || !rd.pages || !rd.pages.length) {
      if (st.stages.read && st.stages.read.done) return `<div class="empty"><div class="empty-title">No preview</div><div>The file could not be opened as a PDF, so there is no page to show.</div></div>`;
      return `<div class="empty"><span class="spinner" style="width:20px;height:20px"></span><div>Opening the document…</div></div>`;
    }
    const page = Math.min(st.page, rd.pages.length);
    const pinfo = rd.pages[page - 1];
    const ex = st.stages.extract && st.stages.extract.done ? st.stages.extract.data : null;
    const boxes = [];
    if (ex) {
      Object.entries(FIELD_BOX).forEach(([key, [label, kind]]) => {
        const m = ex.meta[key];
        if (m && m.bbox && (m.page || 1) === page) boxes.push({ key, label, kind, bbox: m.bbox });
      });
      ex.line_items.forEach((li, i) => {
        if (li.bbox && (li.page || 1) === page) boxes.push({ key: "line" + i, label: `Line item ${i + 1}`, kind: "line", bbox: li.bbox });
      });
    }
    const how = pinfo.method === "ocr"
      ? `<span class="pill warn">Scanned image</span> <span class="muted">read by OCR at ${esc(pinfo.ocr_confidence)}% confidence</span>`
      : `<span class="pill ok">Digital PDF</span> <span class="muted">text read directly, ${pinfo.words} words</span>`;
    const pager = rd.pages.length > 1
      ? `<span class="seg"><button type="button" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>‹</button><button type="button" class="on">Page ${page} of ${rd.pages.length}</button><button type="button" data-page="${page + 1}" ${page >= rd.pages.length ? "disabled" : ""}>›</button></span>`
      : "";
    const legend = boxes.length
      ? `<div class="legend">${boxes.map((b) => `<span class="legend-chip" data-hl="${esc(b.key)}"><i class="i-${b.kind}"></i>${esc(b.label)}</span>`).join("")}</div>`
      : (ex ? "" : `<div class="muted" style="margin-bottom:12px;font-size:12.5px">Fields will be outlined on the page as soon as they are read.</div>`);
    const hl = boxes.map((b, i) => {
      const [x0, y0, x1, y1] = b.bbox;
      return `<div class="hl k-${b.kind}" data-hl="${esc(b.key)}" data-label="${esc(b.label)}" style="left:${x0 * 100}%;top:${y0 * 100}%;width:${(x1 - x0) * 100}%;height:${(y1 - y0) * 100}%;animation-delay:${i * 45}ms"></div>`;
    }).join("");
    return `<div class="doc-bar"><div>${how}</div><div class="page-actions">${pager}<a class="btn btn-sm" href="/api/runs/${esc(st.id)}/file" target="_blank" rel="noopener">${ICON.ext(13)}Open PDF</a></div></div>
      ${legend}
      <div class="doc-frame${st.live ? " animate" : ""}" style="aspect-ratio:${pinfo.width} / ${pinfo.height}"><img src="/api/runs/${esc(st.id)}/pages/${page}?v=${encodeURIComponent(st.run.created_at)}" alt="Page ${page} of ${esc(st.run.filename)}">${hl}</div>`;
  }

  // ---- tab: extracted
  function confBadge(m) {
    if (!m || m.confidence == null || m.confidence >= 0.985) return "";
    return `<span class="conf ${m.confidence < 0.8 ? "low" : ""}" title="Reader confidence in this value">${Math.round(m.confidence * 100)}%</span>`;
  }
  function extractedTab(st) {
    const s = st.stages.extract;
    if (!s || !s.done) return `<div class="empty"><span class="spinner" style="width:20px;height:20px"></span><div>Waiting for the fields to be read…</div></div>`;
    if (s.status === "skipped" || !s.data || !s.data.fields) return `<div class="empty"><div class="empty-title">Nothing was extracted</div><div>${esc(s.summary)}</div></div>`;
    const ex = s.data;
    const f = ex.fields;
    const m = ex.meta;
    const row = (k, v, key, opts = {}) => {
      const meta = key ? m[key] : null;
      const missing = v === null || v === undefined || v === "";
      const val = missing ? `<span class="kv-missing">${esc(opts.missing || "Not on the document")}</span>` : `${opts.mono ? `<span class="mono">${esc(v)}</span>` : esc(v)}${confBadge(meta)}`;
      const note = opts.note || (meta && meta.note) || "";
      return `<div class="kv-row"><span class="kv-k">${esc(k)}</span><span class="kv-v">${val}${note && !missing ? `<span class="kv-note">${esc(note)}</span>` : ""}</span></div>`;
    };
    const taxText = ex.tax_lines.length
      ? ex.tax_lines.map((t) => `${t.label}${t.rate != null ? " " + t.rate + "%" : ""} ${inr(t.amount)}`).join(" + ")
      : (f.tax_inclusive ? null : (f.tax_amount != null ? inr(f.tax_amount) : null));
    const rows = [
      row("Vendor", f.vendor_name, "vendor_name"),
      row("Vendor tax ID", f.vendor_gstin, "vendor_gstin", { mono: true }),
      row("Invoice number", f.invoice_number, "invoice_number", { mono: true }),
      row("Invoice date", f.invoice_date ? fdate(f.invoice_date) : null, "invoice_date"),
      row("Due date", f.due_date ? fdate(f.due_date) : null, "due_date", { missing: "Not stated" }),
      row("PO reference", f.po_number, "po_number", { mono: true, missing: "None on the document", note: f.po_reference_style === "in running text" ? "mentioned inside a sentence, not in a PO field" : "" }),
      row("Subtotal", f.subtotal != null ? inr(f.subtotal) : null, "subtotal", { missing: "Not shown separately" }),
      row("Tax", taxText, "tax_amount", { missing: f.tax_inclusive ? `Included in the total${f.tax_rate != null ? " at " + f.tax_rate + "%" : ""}` : "Not shown" }),
      row("Total", f.total != null ? inr(f.total) : null, "total"),
      row("Amount in words", f.amount_in_words, "amount_in_words", { missing: "Not stated" }),
      row("Bank account", f.bank_account ? "••••" + f.bank_account.slice(-4) + (f.ifsc ? "  ·  " + f.ifsc : "") : null, "bank_account", { missing: "Not stated" }),
      row("Service period", f.service_periods && f.service_periods.length ? f.service_periods.join(", ") : null, null, { missing: "Not stated" }),
    ].join("");
    const lines = ex.line_items.length
      ? `<div class="table-wrap"><table class="grid"><thead><tr><th>#</th><th>Description</th><th class="r">Qty</th><th>Unit</th><th class="r">Rate</th><th class="r">Amount</th></tr></thead><tbody>${ex.line_items.map((li, i) => `<tr><td class="num">${i + 1}</td><td>${esc(li.description)}</td><td class="r num">${li.qty != null ? qty(li.qty) : '<span class="faint">lump sum</span>'}</td><td>${esc(li.unit || "")}</td><td class="r num">${li.unit_price != null ? rate(li.unit_price) : "—"}</td><td class="r num td-strong">${inr(li.amount)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="muted">No line-item table was found on the document.</div>`;
    const v = st.stages.validate && st.stages.validate.done && st.stages.validate.data && st.stages.validate.data.total != null ? st.stages.validate.data : null;
    const norm = v ? `<div class="subhead"><h3>Figures used for matching</h3>${v.tax_inclusive ? '<span class="pill info">Tax split out of an inclusive total</span>' : ""}</div>
      <div class="stat-strip" style="margin-top:0">
        <div class="stat"><div class="stat-l">Before tax</div><div class="stat-v num">${inr(v.subtotal)}</div></div>
        <div class="stat"><div class="stat-l">Tax${v.tax_rate != null ? " at " + v.tax_rate + "%" : ""}</div><div class="stat-v num">${inr(v.tax)}</div></div>
        <div class="stat"><div class="stat-l">Total</div><div class="stat-v num">${inr(v.total)}</div></div>
      </div>` : "";
    const reader = ex.method === "ai" ? `Claude (${esc(ex.model || "")})` : "Built-in parser";
    return `<div class="subhead"><h3>Fields read from the document</h3><span><span class="pill">${reader}</span>${ex.confidence != null ? ` <span class="pill ${ex.confidence < 0.8 ? "warn" : "ok"}">${pct(ex.confidence)} confidence</span>` : ""}</span></div>
      <div class="kv">${rows}</div>
      <div class="subhead"><h3>Line items</h3><span class="muted" style="font-size:12.5px">${ex.line_items.length} read</span></div>
      ${lines}${norm}
      ${ex.notes && ex.notes.length ? `<div class="subhead"><h3>Reader notes</h3></div><ul class="options">${ex.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`;
  }

  // ---- tab: matching
  function poMeter(before, added, value) {
    const b = Math.max(0, Math.min(1, value ? before / value : 0));
    const a = Math.max(0, value ? added / value : 0);
    const over = b + a > 1.0001;
    return `<div class="meter" title="Grey: billed before this invoice. Colour: this invoice."><i style="width:${b * 100}%"></i><i class="${over ? "over" : "new"}" style="width:${Math.min(a, 1 - b) * 100}%"></i></div>`;
  }
  function resultPills(r) {
    const out = [];
    const good = r.price_status !== "fail" && r.price_status !== "low";
    if (r.price_status === "fail" && r.po_line_no == null) out.push('<span class="pill bad">Not on the PO</span>');
    else if (r.price_status === "fail") out.push('<span class="pill bad">Price too high</span>');
    else if (r.price_status === "low") out.push('<span class="pill bad">Price far below PO</span>');
    if (r.qty_status === "over") out.push('<span class="pill bad">Over quantity</span>');
    if (r.qty_status === "partial" && good) out.push('<span class="pill info">Part-billed</span>');
    if (r.qty_status === "final" && good) out.push('<span class="pill ok">Matches</span>');
    if (r.qty_status === "lump") out.push('<span class="pill ok">Covered</span>');
    if (r.qty_status === "unknown" && r.po_line_no != null) out.push('<span class="pill warn">Cannot allocate</span>');
    const fullyBilled = r.qty_status === "not_billed" && r.note === "already fully billed";
    if (r.qty_status === "not_billed") out.push(`<span class="pill">${fullyBilled ? "Already fully billed" : "Not on this invoice"}</span>`);
    return out.join(" ") + (r.note && !fullyBilled && r.note !== "not on this invoice" ? `<span class="cell-sub">${esc(r.note)}</span>` : "");
  }
  function matchingTab(st) {
    const pmS = st.stages.po_match;
    if (!pmS || !pmS.done) return `<div class="empty"><span class="spinner" style="width:20px;height:20px"></span><div>Waiting for the purchase-order step…</div></div>`;
    if (pmS.status === "skipped") return `<div class="empty"><div class="empty-title">Matching did not run</div><div>${esc(pmS.summary)}</div></div>`;
    const pm = pmS.data;
    const rec = st.stages.reconcile && st.stages.reconcile.done ? st.stages.reconcile : null;
    const dupS = st.stages.duplicate && st.stages.duplicate.done ? st.stages.duplicate : null;
    let html = "";

    // purchase order
    if (pm.po) {
      const po = pm.po;
      const added = rec && rec.data && rec.data.invoice_subtotal ? Math.min(rec.data.value_at_po_prices ?? rec.data.invoice_subtotal, po.value) : 0;
      const before = po.value - po.remaining_value;
      const how = pm.method === "inferred"
        ? `<span class="pill warn">Inferred · ${pct(pm.confidence)} confidence</span>`
        : `<span class="pill ok">Quoted on the invoice</span>`;
      html += `<div class="subhead"><h3>Purchase order</h3>${how}</div>
        <div class="po-card">
          <div class="po-card-top">
            <div><div class="po-no mono">${esc(po.po_number)}</div><div>${esc(po.description)}</div>
              <div class="muted" style="font-size:12.5px;margin-top:2px">Raised ${fdate(po.po_date)} by ${esc(po.buyer_name)}, ${esc(po.buyer_role)}</div></div>
            <div style="text-align:right"><div class="stat-l">PO value before tax</div><div class="stat-v num">${rupees(po.value)}</div></div>
          </div>
          <div class="po-meter-row">${poMeter(before, added, po.value)}<span class="muted num" style="font-size:12.5px">${rupees(before)} supplied before · ${rupees(po.remaining_value)} open</span></div>
        </div>`;
    } else {
      const why = (pmS.checks || []).filter((c) => c.status === "fail" || c.status === "warn" || c.status === "info");
      html += `<div class="subhead"><h3>Purchase order</h3><span class="pill bad">No match</span></div>${why.map((c) => `<div class="reason ${esc(c.status)}"><div class="reason-title">${esc(c.title)}</div><div class="reason-detail">${esc(c.detail)}</div></div>`).join("")}`;
    }
    if (pm.candidates && pm.candidates.length) {
      const chosen = pm.po ? pm.po.po_number : null;
      html += `<div class="subhead"><h3>How the PO was chosen</h3><span class="muted" style="font-size:12.5px">score = 45% amount + 35% description + 20% service period</span></div>
        <div class="table-wrap"><table class="grid"><thead><tr><th>Open PO</th><th>Amount fit</th><th>Description fit</th><th>Service period</th><th class="r">Score</th></tr></thead><tbody>
        ${pm.candidates.map((c) => `<tr class="${c.po_number === chosen ? "pick" : ""}"><td><span class="mono td-strong">${esc(c.po_number)}</span>${c.po_number === chosen ? ' <span class="pill ok">chosen</span>' : ""}<span class="cell-sub">${esc(c.description)}</span><span class="cell-sub num">${rupees(c.remaining_value)} open</span></td><td class="num">${pct(c.amount_fit)}<span class="cell-sub">${esc(c.amount_short)}</span></td><td class="num">${pct(c.text_fit)}</td><td>${c.period_fit === 1 ? '<span class="pill ok">Same</span>' : c.period_fit === 0 ? '<span class="pill bad">Different</span>' : '<span class="pill">n/a</span>'}<span class="cell-sub">${esc(c.period_short)}</span></td><td class="r num td-strong">${pct(c.score)}</td></tr>`).join("")}
        </tbody></table></div>`;
    }

    // duplicate
    if (dupS) {
      const match = dupS.data && dupS.data.match;
      if (match) {
        const o = match.original;
        html += `<div class="subhead"><h3>Duplicate check</h3><span class="pill bad">Matches an invoice already in the ledger</span></div>
          <div class="table-wrap"><table class="grid"><thead><tr><th>Compared</th><th>This invoice</th><th>${esc(o.invoice_number)} (${o.status === "paid" ? "paid " + fdate(o.pay_date) : "scheduled " + fdate(o.pay_date)})</th><th>Result</th></tr></thead><tbody>
          ${match.signals.map((sg) => `<tr><td class="td-strong">${esc(sg.label)}</td><td class="${sg.label === "Invoice number" ? "mono" : ""}">${esc(sg.this)}</td><td class="${sg.label === "Invoice number" ? "mono" : ""}">${esc(sg.original)}</td><td><span class="pill ${sg.match ? "bad" : ""}">${sg.match ? "Same" : "Different"}</span>${["same", "different", "identical"].includes(sg.note) ? "" : `<span class="cell-sub">${esc(sg.note)}</span>`}</td></tr>`).join("")}
          </tbody></table></div>`;
      } else {
        html += `<div class="subhead"><h3>Duplicate check</h3><span class="pill ok">No duplicate</span></div><div class="muted" style="font-size:13px">${esc(dupS.summary)}.</div>`;
      }
    }

    // reconciliation
    if (rec && rec.status !== "skipped" && rec.data && rec.data.rows) {
      const d = rec.data;
      const fails = (rec.checks || []).filter((c) => c.status === "fail").length;
      html += `<div class="subhead"><h3>Line-by-line against the PO</h3><span class="pill ${fails ? "bad" : "ok"}">${fails ? fails + (fails === 1 ? " discrepancy" : " discrepancies") : "Everything matches"}</span></div>
        <div class="table-wrap"><table class="grid dense"><thead><tr><th>PO line</th><th class="r">Open</th><th class="r">Billed now</th><th class="r">PO price</th><th class="r">Inv. price</th><th>Result</th></tr></thead><tbody>
        ${d.rows.map((r) => {
          const priceBad = (r.price_status === "fail" || r.price_status === "low") && r.po_line_no != null;
          const qtyBad = r.qty_status === "over";
          const dim = r.qty_status === "not_billed";
          const name = r.po_line_no != null ? `<span class="td-strong">${r.po_line_no}.</span> ${esc(r.po_description)}` : `<span class="td-strong">${esc(r.description)}</span>`;
          const variance = r.price_var_pct != null && r.price_var_pct !== 0 ? `<span class="cell-sub">${r.price_var_pct > 0 ? "+" : ""}${r.price_var_pct.toFixed(1)}%</span>` : "";
          const openCell = r.po_line_no != null
            ? `${qty(r.remaining_before)}<span class="cell-sub">of ${qty(r.po_qty)} ${esc(r.unit || "")}${r.billed_before ? "; " + qty(r.billed_before) + " billed earlier" : ""}</span>`
            : "—";
          return `<tr class="${dim ? "dim" : ""}"><td>${name}</td><td class="r num">${openCell}</td><td class="r num ${qtyBad ? "bad" : ""}">${r.inv_qty != null ? qty(r.inv_qty) : (r.qty_status === "lump" ? "all" : "—")}</td><td class="r num">${r.po_price != null ? rate(r.po_price, "INR") : "—"}</td><td class="r num ${priceBad ? "bad" : ""}">${r.inv_price != null ? rate(r.inv_price) : "—"}${variance}</td><td>${resultPills(r)}</td></tr>`;
        }).join("")}
        </tbody></table></div>
        <div class="stat-strip">
          <div class="stat"><div class="stat-l">Invoice value before tax</div><div class="stat-v num">${inr(d.invoice_subtotal)}</div></div>
          <div class="stat"><div class="stat-l">Open on the PO before this</div><div class="stat-v num">${rupees(d.po_remaining_before)}</div></div>
          ${d.mode === "line" ? `<div class="stat"><div class="stat-l">Same quantities at PO prices</div><div class="stat-v num">${inr(d.value_at_po_prices)}</div></div>` : ""}
          <div class="stat"><div class="stat-l">PO tax rate</div><div class="stat-v num">${d.tax_rate}%</div></div>
        </div>`;
    } else if (rec && rec.status === "skipped") {
      html += `<div class="subhead"><h3>Line-by-line against the PO</h3><span class="pill">Not run</span></div><div class="muted" style="font-size:13px">${esc(rec.summary)}</div>`;
    }
    return html;
  }

  // ---- tab: outcome
  function outcomeTab(st) {
    if (st.error) return `<div class="empty"><div class="empty-title">The run failed</div><div>${esc(st.error)}</div></div>`;
    if (!st.finished) return `<div class="empty"><span class="spinner" style="width:20px;height:20px"></span><div>The decision appears here when the run finishes.</div></div>`;
    const d = st.decision;
    let html = `<div class="subhead"><h3>Why</h3></div>`;
    if (d.decision === "APPROVED") {
      html += `<div class="reason pass"><div class="reason-title">Every check passed</div><div class="reason-detail">${d.passed_count} checks across validation, vendor, purchase order, duplicates and line reconciliation. Nothing needed a person.</div></div>`;
    }
    html += d.reasons.map((r) => `<div class="reason ${esc(r.status)}"><div class="reason-title">${esc(r.title)}</div>${r.detail ? `<div class="reason-detail">${esc(r.detail)}</div>` : ""}</div>`).join("");

    if (d.decision === "NEEDS_REVIEW") {
      const pending = st.run.review_status === "pending" && !st.review;
      if (pending) {
        html += `<div class="subhead"><h3>Decision needed</h3><span class="pill warn">Waiting on ${esc(d.route.name)}</span></div>
          <div class="review-box">
            <div><b>${esc(d.route.name)}</b> <span class="muted">${esc(d.route.role)} · ${esc(d.route.email)}</span></div>
            <ol class="options" type="a">${d.options.map((o) => `<li>${esc(o)}</li>`).join("")}</ol>
            <textarea id="review-note" placeholder="Note for the audit trail (optional)" maxlength="500"></textarea>
            <div class="review-actions">
              <button class="btn btn-success" id="review-approve" type="button">${ICON.check(14)}Approve as billed and post</button>
              <button class="btn btn-danger" id="review-reject" type="button">${ICON.x(13)}Reject</button>
            </div>
            <div class="muted" style="font-size:12.5px;margin-top:10px">In production this lands in the reviewer's own queue. Here you can act as the reviewer to see the loop close.</div>
          </div>`;
      } else {
        html += `<div class="subhead"><h3>Review</h3></div>${reviewedHtml(st) || '<div class="muted">Reviewed.</div>'}`;
      }
    }

    let steps = d.next_steps;
    let stepsTitle = "What happens next";
    if (d.decision === "NEEDS_REVIEW" && st.review) {
      const r = st.review;
      stepsTitle = "What happened";
      steps = [`Routed to ${d.route.name} (${d.route.role}) with the question and the options.`];
      if (r.action === "approve") {
        steps.push(`${r.by} approved it as billed${r.note ? `, noting: “${r.note}”` : ""}.`);
        if (r.posted) {
          steps.push(`Posted to the ledger. Payment of ${inr(r.amount)} scheduled for ${fdate(r.pay_date)} to the account on file.`);
          if (r.po_progress) steps.push(r.po_progress);
        } else if (r.note_system) steps.push(r.note_system);
      } else {
        steps.push(`${r.by} rejected it${r.note ? `, noting: “${r.note}”` : ""}.`);
        steps.push("Nothing was posted or scheduled. The vendor is notified.");
      }
    }
    html += `<div class="subhead"><h3>${stepsTitle}</h3></div><ol class="steps">${steps.map((s, i) => `<li><i>${i + 1}</i><span>${esc(s)}</span></li>`).join("")}</ol>`;

    if (d.payment) {
      const p = d.payment;
      html += `<div class="subhead"><h3>Payment instruction</h3><span class="pill ok">Scheduled</span></div>
        <div class="stat-strip" style="margin-top:0">
          <div class="stat"><div class="stat-l">Pay</div><div class="stat-v num">${inr(p.amount)}</div></div>
          <div class="stat"><div class="stat-l">To</div><div class="stat-v" style="font-size:13.5px">${esc(p.payee)}</div></div>
          <div class="stat"><div class="stat-l">On</div><div class="stat-v">${fdate(p.pay_date)}</div><div class="stat-l">${esc(p.terms)}</div></div>
          <div class="stat"><div class="stat-l">Account on file</div><div class="stat-v mono">${esc(p.bank_account_masked)}</div><div class="stat-l">${esc(p.bank_name)}</div></div>
          <div class="stat"><div class="stat-l">Reference</div><div class="stat-v mono" style="font-size:12.5px">${esc(p.reference)}</div><div class="stat-l">${esc(p.po_number || "")}</div></div>
        </div>`;
    }
    const after = d.po_after || (st.review && st.review.po_after);
    if (after) {
      html += `<div class="subhead"><h3>${esc(after.po_number)} after this invoice</h3><span class="pill ${after.status === "fully_billed" ? "ok" : "info"}">${after.status === "fully_billed" ? "Fully billed" : "Still open: " + rupees(after.remaining_value)}</span></div>
        <div class="table-wrap"><table class="grid"><thead><tr><th>PO line</th><th class="r">Ordered</th><th class="r">Billed</th><th class="r">Open</th><th style="width:150px">Progress</th></tr></thead><tbody>
        ${after.lines.map((l) => `<tr><td><span class="td-strong">${l.line_no}.</span> ${esc(l.description)}</td><td class="r num">${qty(l.qty)}</td><td class="r num">${qty(l.billed_qty)}</td><td class="r num">${qty(Math.max(0, l.remaining_qty))}</td><td><div class="meter"><i class="${l.billed_qty > l.qty + 1e-6 ? "over" : "new"}" style="width:${Math.min(100, (l.billed_qty / l.qty) * 100)}%"></i></div></td></tr>`).join("")}
        </tbody></table></div>`;
    }
    if (d.message) {
      const mKind = { review_request: "Review request", vendor_email: "Reply to the vendor", vendor_notice: "Notice to the vendor" }[d.message.kind] || "Message";
      html += `<div class="subhead"><h3>${mKind} <span class="muted" style="font-weight:500">· drafted, not sent</span></h3><button class="btn btn-sm" id="copy-msg" type="button">${ICON.copy(13)}Copy</button></div>
        <div class="mail"><div class="mail-head"><div><b>To</b>${esc(d.message.to_name)} &lt;${esc(d.message.to)}&gt;</div><div><b>Subject</b>${esc(d.message.subject)}</div></div><pre class="mail-body">${esc(d.message.body)}</pre></div>`;
    }
    html += `<div style="margin-top:22px"><details class="passed"><summary>${d.passed_count} checks passed</summary><ul>${d.passed.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details></div>`;
    return html;
  }

  const TABS = [
    ["outcome", "Outcome", (st) => st.finished],
    ["document", "Document", (st) => !!(st.stages.read)],
    ["extracted", "Extracted data", (st) => !!(st.stages.extract && st.stages.extract.done)],
    ["matching", "PO and matching", (st) => !!(st.stages.po_match && st.stages.po_match.done)],
  ];
  const TAB_RENDER = { outcome: outcomeTab, document: documentTab, extracted: extractedTab, matching: matchingTab };

  function tabSignature(st) {
    const done = (id) => (st.stages[id] && st.stages[id].done ? 1 : 0);
    const k = {
      document: [done("read"), done("extract"), st.page],
      extracted: [done("extract"), done("validate")],
      matching: [done("po_match"), done("duplicate"), done("reconcile")],
      outcome: [st.finished ? 1 : 0, st.review ? st.review.action : "", st.run.review_status || ""],
    }[st.tab];
    return st.tab + ":" + k.join(",");
  }

  async function viewRun(alive, id) {
    view.innerHTML = `<div class="page"><div class="card"><div class="empty"><span class="spinner" style="width:20px;height:20px"></span><div>Loading ${esc(id)}…</div></div></div></div>`;
    let data;
    try { data = await api(`/api/runs/${id}`); } catch (err) {
      if (!alive()) return;
      view.innerHTML = `<div class="page"><div class="card"><div class="empty"><div class="empty-title">${esc(id)} was not found</div><div>It may have been cleared by a demo reset.</div><a class="btn btn-sm" href="#/runs">All runs</a></div></div></div>`;
      return;
    }
    if (!alive()) return;
    const st = { id, run: data.run, sample: data.sample, stages: {}, open: {}, lastSeq: 0, tab: "document", userTab: false, page: 1, live: false, finished: false, startedAt: null, sig: null };
    data.events.forEach((e) => applyEvent(st, e));
    const finishedAtLoad = ["done", "error"].includes(st.run.status);
    st.live = !finishedAtLoad;
    st.fresh = null;
    if (st.run.status === "error" && !st.error) st.error = st.run.error || "The run did not finish.";
    // A finished run always carries its decision; if it somehow does not, keep following it live.
    if (finishedAtLoad && !st.decision && !st.error) st.live = true;
    st.finished = !!(st.decision || st.error);
    if (st.finished) st.tab = st.error ? "document" : "outcome";

    const source = st.sample
      ? `Inbox: ${esc(st.sample.tag)} · ${esc(st.sample.title)}`
      : "Uploaded file";
    view.innerHTML = `<div class="page">
      <div class="crumbs"><a href="#/runs">${ICON.back(13)}All runs</a><span>/</span><span class="mono">${esc(id)}</span></div>
      <div class="run-title"><h1>${esc(st.run.filename)}</h1></div>
      <div class="run-sub">${source} · started ${esc(fwhen(st.run.created_at))} · <a href="/api/runs/${esc(id)}" target="_blank" rel="noopener" title="The full machine-readable record of this run: every event, check and the decision">Audit record (JSON)</a></div>
      <div id="banner"></div>
      <div class="run-grid">
        <div class="card"><div class="card-head"><div class="card-title">Pipeline</div><button class="btn btn-ghost btn-sm" id="toggle-all" type="button">Expand all</button></div><div id="pipeline"></div></div>
        <div class="card"><div class="tabs" id="tabs" role="tablist"></div><div class="tab-body" id="tab-body"></div></div>
      </div></div>`;

    const render = (force = false) => {
      const exData = st.stages.extract && st.stages.extract.data;
      CUR = st.run.currency || (exData && exData.fields && exData.fields.currency) || "INR";
      $("#banner").innerHTML = bannerHtml(st);
      $("#pipeline").innerHTML = pipelineHtml(st);
      $("#tabs").innerHTML = TABS.map(([key, label, ready]) => `<button class="tab ${st.tab === key ? "on" : ""}" type="button" role="tab" data-tab="${key}" ${ready(st) ? "" : "disabled"}>${label}</button>`).join("");
      const sig = tabSignature(st);
      if (force || sig !== st.sig) {
        st.sig = sig;
        $("#tab-body").innerHTML = TAB_RENDER[st.tab](st);
      }
      tick();
    };
    const tick = () => {
      const el = $("#elapsed");
      if (el && st.startedAt) el.textContent = ((Date.now() - st.startedAt) / 1000).toFixed(1) + " s";
    };
    const timer = setInterval(tick, 100);
    render(true);

    // interactions (delegated, so re-rendering never loses a handler)
    const onClick = async (e) => {
      const head = e.target.closest(".stage-head");
      if (head && !head.disabled) {
        const sid = head.parentElement.dataset.stage;
        const isOpen = head.parentElement.classList.contains("open");
        st.open[sid] = !isOpen;
        st.fresh = null;
        $("#pipeline").innerHTML = pipelineHtml(st);
        return;
      }
      const tab = e.target.closest("[data-tab]");
      if (tab && !tab.disabled) { st.tab = tab.dataset.tab; st.userTab = true; st.fresh = null; render(); return; }
      const pg = e.target.closest("[data-page]");
      if (pg && !pg.disabled) { st.page = Number(pg.dataset.page); render(); return; }
      if (e.target.closest("#toggle-all")) {
        const anyClosed = META.stages.some((d) => st.stages[d.id] && !$(`.stage[data-stage="${d.id}"]`).classList.contains("open"));
        META.stages.forEach((d) => { if (st.stages[d.id]) st.open[d.id] = anyClosed; });
        st.fresh = null;
        $("#pipeline").innerHTML = pipelineHtml(st);
        $("#toggle-all").textContent = anyClosed ? "Collapse all" : "Expand all";
        return;
      }
      if (e.target.closest("#copy-msg")) {
        const m = st.decision.message;
        const text = `To: ${m.to_name} <${m.to}>\nSubject: ${m.subject}\n\n${m.body}`;
        try { await navigator.clipboard.writeText(text); toast("Message copied"); } catch (_) { toast("Copy is not available in this browser", true); }
        return;
      }
      const rv = e.target.closest("#review-approve, #review-reject");
      if (rv) {
        const action = rv.id === "review-approve" ? "approve" : "reject";
        const note = ($("#review-note") || {}).value || "";
        $$("#review-approve, #review-reject").forEach((b) => (b.disabled = true));
        try {
          await api(`/api/runs/${id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, note }) });
          const fresh = await api(`/api/runs/${id}`);
          if (!alive()) return;
          st.run = fresh.run;
          fresh.events.filter((ev) => ev.seq > st.lastSeq).forEach((ev) => applyEvent(st, ev));
          const posted = st.review && st.review.posted;
          toast(action === "reject" ? "Rejected. Nothing was posted" : posted ? "Approved and posted to the ledger" : "Approved, but not posted. See the review note");
          render(true);
          refreshBadge();
        } catch (err) {
          toast(err.message, true);
          $$("#review-approve, #review-reject").forEach((b) => (b.disabled = false));
        }
      }
    };
    const onHover = (e) => {
      const el = e.target.closest("[data-hl]");
      $$(".hot").forEach((x) => x.classList.remove("hot"));
      if (el) $$(`[data-hl="${el.dataset.hl}"]`).forEach((x) => x.classList.add("hot"));
    };
    view.addEventListener("click", onClick);
    view.addEventListener("mouseover", onHover);

    let es = null;
    if (st.live) {
      es = new EventSource(`/api/runs/${id}/events?after=${st.lastSeq}`);
      es.onmessage = (msg) => {
        const e = JSON.parse(msg.data);
        if (e.seq <= st.lastSeq) return;
        applyEvent(st, e);
        if (e.type === "run_completed" || e.type === "run_failed") {
          api(`/api/runs/${id}`).then((fresh) => { if (alive()) { st.run = fresh.run; render(true); refreshBadge(); } }).catch(() => {});
        }
        render();
      };
      es.addEventListener("end", () => { es.close(); });
      es.onerror = () => { if (st.finished) es.close(); };
    }
    return () => {
      CUR = "INR";
      clearInterval(timer);
      if (es) es.close();
      view.removeEventListener("click", onClick);
      view.removeEventListener("mouseover", onHover);
    };
  }

  // ============================================================ dashboard ===
  async function viewRuns(alive) {
    let filter = "all";
    let query = "";
    let runs = [];
    let lastJson = "";
    let timer = null;
    view.innerHTML = `<div class="page">
      <div class="page-head">
        <div><h1 class="page-title">Dashboard</h1><p class="page-sub">Every invoice the desk has handled: what was decided, why, and what is waiting on a person.</p></div>
        <div class="page-actions"><a class="btn btn-primary" href="#/">${ICON.play(12)}Process an invoice</a></div>
      </div>
      <div class="kpis" id="kpis"></div>
      <div class="card" style="margin-bottom:18px"><div class="card-body" id="mix"></div></div>
      <div class="card">
        <div class="card-head">
          <div class="seg" id="filter-seg" role="group" aria-label="Filter by outcome">
            <button type="button" data-f="all" class="on">All</button>
            <button type="button" data-f="APPROVED">Approved</button>
            <button type="button" data-f="NEEDS_REVIEW">Held for review</button>
            <button type="button" data-f="RETURNED">Returned</button>
            <button type="button" data-f="REJECTED">Rejected</button>
          </div>
          <input class="search" id="search" type="search" placeholder="Search vendor, invoice, PO…" aria-label="Search runs">
        </div>
        <div id="runs-table"></div>
      </div></div>`;

    const kpi = (label, value, sub) => `<div class="card kpi"><div class="kpi-l">${label}</div><div class="kpi-v num">${value}</div><div class="kpi-s">${sub}</div></div>`;
    const renderStats = (s) => {
      const b = s.by_decision;
      $("#kpis").innerHTML = [
        kpi("Invoices processed", s.runs, s.in_progress ? `${s.in_progress} in progress` : "since the last reset"),
        kpi("Straight-through", s.straight_through_rate == null ? "—" : Math.round(s.straight_through_rate * 100) + "%", "approved with no one touching them"),
        kpi("Waiting on a person", s.pending_review, s.pending_review ? inrShort(s.pending_review_value) + " held" : "review queue is clear"),
        kpi("Approved for payment", inrShort(s.approved_value), `${b.APPROVED} straight-through` + (s.reviewer_approved ? `, ${s.reviewer_approved} by a reviewer` : "")),
        kpi("Stopped before payment", inrShort(s.stopped_value), "rejected, returned or held"),
      ].join("");
      const n = s.runs || 1;
      const seg = (k) => `<i class="${DCLS[k]}" style="width:${(b[k] / n) * 100}%" title="${LABELS[k]}: ${b[k]}"></i>`;
      $("#mix").innerHTML = s.runs
        ? `<div class="toolbar" style="margin-bottom:10px"><div class="card-title">Outcome mix</div><div class="muted" style="font-size:12.5px">Median compute time per invoice: ${dur(s.median_compute_ms)}</div></div>
           <div class="mix">${["APPROVED", "NEEDS_REVIEW", "RETURNED", "REJECTED"].map(seg).join("")}</div>
           <div class="mix-legend">${["APPROVED", "NEEDS_REVIEW", "RETURNED", "REJECTED"].map((k) => `<span><i style="background:${{ APPROVED: "var(--green)", NEEDS_REVIEW: "#d28a16", RETURNED: "var(--violet)", REJECTED: "var(--red)" }[k]}"></i>${LABELS[k]} <b class="num">${b[k]}</b> <span class="muted num">${inrShort(s.value_by_decision[k])}</span></span>`).join("")}</div>`
        : `<div class="muted">Outcome mix appears once the first invoice has been processed.</div>`;
      const badge = $("#nav-pending");
      badge.hidden = !s.pending_review;
      badge.textContent = s.pending_review;
    };
    const renderTable = () => {
      const q = query.trim().toLowerCase();
      const rows = runs.filter((r) => (filter === "all" || r.decision === filter) && (!q || [r.id, r.vendor_name, r.invoice_number, r.po_number, r.filename, r.top_reason].some((v) => (v || "").toLowerCase().includes(q))));
      if (!runs.length) {
        $("#runs-table").innerHTML = `<div class="empty"><div class="empty-title">No runs yet</div><div>Process an invoice from the inbox and it will appear here.</div><a class="btn btn-sm btn-primary" href="#/">Open the inbox</a></div>`;
        return;
      }
      if (!rows.length) { $("#runs-table").innerHTML = `<div class="empty"><div class="empty-title">Nothing matches</div><div>Try a different filter or search.</div></div>`; return; }
      const tagOf = (r) => { const s = META.samples.find((x) => x.key === r.sample_key); return s ? s.tag : "Upload"; };
      $("#runs-table").innerHTML = `<div style="overflow-x:auto"><table class="grid runs"><thead><tr><th>Run</th><th>Vendor and invoice</th><th>PO</th><th class="r">Amount</th><th>Outcome</th><th>Why</th><th class="r">Compute</th></tr></thead><tbody>
        ${rows.map((r) => `<tr data-run="${esc(r.id)}" tabindex="0">
          <td><span class="mono td-strong">${esc(r.id)}</span><span class="cell-sub">${esc(fwhen(r.created_at))} · ${esc(tagOf(r))}</span></td>
          <td><span class="td-strong">${esc(r.vendor_name || r.filename)}</span><span class="cell-sub mono">${esc(r.invoice_number || (r.status === "done" ? "no invoice number" : r.filename))}</span></td>
          <td class="mono">${esc(r.po_number || "—")}</td>
          <td class="r num td-strong">${r.total != null ? inr(r.total, r.currency || "INR") : "—"}</td>
          <td>${outcomeChip(r)}${r.decision === "NEEDS_REVIEW" && r.review_status === "pending" && r.route_to ? `<span class="cell-sub">with ${esc(r.route_to)}</span>` : ""}</td>
          <td class="why">${esc(r.status === "error" ? r.error : r.top_reason || "")}</td>
          <td class="r num muted">${r.compute_ms != null ? dur(r.compute_ms) : ""}</td>
        </tr>`).join("")}</tbody></table></div>`;
    };
    const refresh = async () => {
      try {
        const [s, r] = await Promise.all([api("/api/stats"), api("/api/runs")]);
        if (!alive()) return;
        const json = JSON.stringify([s, r]);
        if (json !== lastJson) { lastJson = json; runs = r.runs; renderStats(s); renderTable(); }
        timer = setTimeout(refresh, s.in_progress ? 900 : 5000);
      } catch (err) {
        if (alive()) timer = setTimeout(refresh, 5000);
      }
    };
    await refresh();
    if (!alive()) { clearTimeout(timer); return; }
    $("#filter-seg").addEventListener("click", (e) => {
      const b = e.target.closest("[data-f]");
      if (!b) return;
      filter = b.dataset.f;
      $$("#filter-seg button").forEach((x) => x.classList.toggle("on", x === b));
      renderTable();
    });
    $("#search").addEventListener("input", (e) => { query = e.target.value; renderTable(); });
    const open = (e) => {
      if (e.type === "keydown" && e.key !== "Enter") return;
      const tr = e.target.closest("[data-run]");
      if (tr) location.hash = `#/runs/${tr.dataset.run}`;
    };
    $("#runs-table").addEventListener("click", open);
    $("#runs-table").addEventListener("keydown", open);
    return () => clearTimeout(timer);
  }

  // ====================================================== purchase orders ===
  async function viewPOs(alive) {
    const data = await api("/api/pos");
    if (!alive()) return;
    const open = {};
    const statusPill = (s) => ({ open: '<span class="pill info">Open</span>', partially_billed: '<span class="pill warn">Part-billed</span>', fully_billed: '<span class="pill ok">Fully billed</span>', closed: '<span class="pill">Closed</span>' }[s] || s);
    const detail = (po) => `<tr class="po-row-detail"><td colspan="7"><div class="two-col">
      <div><div class="section-title">Lines</div><div class="table-wrap"><table class="grid"><thead><tr><th>Line</th><th class="r">Ordered</th><th class="r">Billed</th><th class="r">Open</th><th class="r">Unit price</th></tr></thead><tbody>
        ${po.lines.map((l) => `<tr><td><span class="td-strong">${l.line_no}.</span> ${esc(l.description)}</td><td class="r num">${qty(l.qty)} <span class="faint">${esc(l.unit || "")}</span></td><td class="r num">${qty(l.billed_qty)}</td><td class="r num ${l.remaining_qty < 0 ? "bad" : ""}">${qty(l.remaining_qty)}</td><td class="r num">${inr(l.unit_price)}</td></tr>`).join("")}
      </tbody></table></div></div>
      <div><div class="section-title">Invoices accepted against it</div>${po.invoices.length ? `<div class="table-wrap"><table class="grid"><thead><tr><th>Invoice</th><th>Date</th><th class="r">Total</th><th>Status</th></tr></thead><tbody>
        ${po.invoices.map((i) => `<tr><td class="mono">${esc(i.invoice_number)}${i.run_id ? `<span class="cell-sub"><a href="#/runs/${esc(i.run_id)}">${esc(i.run_id)}</a></span>` : '<span class="cell-sub" style="font-family:var(--font)">payment history</span>'}</td><td>${fdate(i.invoice_date)}</td><td class="r num">${inr(i.total)}</td><td>${i.status === "paid" ? '<span class="pill ok">Paid</span>' : '<span class="pill info">Scheduled</span>'}<span class="cell-sub">${fdate(i.pay_date)}</span></td></tr>`).join("")}
      </tbody></table></div>` : '<div class="muted">None yet.</div>'}
      <div class="muted" style="font-size:12.5px;margin-top:10px">Buyer: ${esc(po.buyer_name)}, ${esc(po.buyer_role)}</div></div>
    </div></td></tr>`;
    const draw = () => {
      $("#po-body").innerHTML = data.purchase_orders.map((po) => {
        const supplied = po.value - po.remaining_value;
        return `<tr data-po="${esc(po.po_number)}" tabindex="0" style="cursor:pointer">
          <td><span class="mono td-strong">${esc(po.po_number)}</span><span class="cell-sub">${fdate(po.po_date)}</span></td>
          <td><span class="td-strong">${esc(po.vendor_name)}</span><span class="cell-sub">${esc(po.description)}</span></td>
          <td class="r num">${inr(po.value)}</td>
          <td class="r num">${inr(po.billed_value)}</td>
          <td class="r num td-strong">${inr(po.remaining_value)}</td>
          <td style="min-width:120px"><div class="meter"><i class="new" style="width:${po.value ? Math.min(100, (supplied / po.value) * 100) : 0}%"></i></div></td>
          <td>${statusPill(po.status)}</td>
        </tr>${open[po.po_number] ? detail(po) : ""}`;
      }).join("");
    };
    view.innerHTML = `<div class="page">
      <div class="page-head"><div><h1 class="page-title">Purchase orders</h1><p class="page-sub">The procurement side of the match: what was ordered, at what price, and how much of each PO has already been billed. Approved invoices update these figures; click a row for lines and invoices.</p></div></div>
      <div class="card"><div style="overflow-x:auto"><table class="grid runs"><thead><tr><th>PO</th><th>Vendor</th><th class="r">Value</th><th class="r">Billed</th><th class="r">Open</th><th>Supplied</th><th>Status</th></tr></thead><tbody id="po-body"></tbody></table></div></div>
      <p class="muted" style="margin-top:12px;font-size:12.5px">Values are before tax. “Open” is the quantity still to be supplied, valued at the PO price.</p>
    </div>`;
    draw();
    const toggle = (e) => {
      if (e.type === "keydown" && e.key !== "Enter") return;
      if (e.target.closest("a")) return;
      const tr = e.target.closest("[data-po]");
      if (!tr) return;
      open[tr.dataset.po] = !open[tr.dataset.po];
      draw();
    };
    $("#po-body").addEventListener("click", toggle);
    $("#po-body").addEventListener("keydown", toggle);
  }

  // =============================================================== policy ===
  const RULEBOOK = [
    ["var(--red)", "Reject", "The invoice duplicates one already accepted: the same file, the same number (ignoring punctuation, leading zeros and revision suffixes), or a re-issue with identical content. Or the vendor is <b>blocked</b> in the vendor master."],
    ["var(--violet)", "Return to the vendor", "A required field is missing (<b>invoice number, invoice date, total, vendor</b>) or the file cannot be read. The process never guesses a missing value; it drafts a reply saying exactly what is needed."],
    ["#d28a16", "Hold for a named person", "<b>Treasury:</b> the bank account on the invoice differs from the one on file. <b>Vendor Master desk:</b> unknown vendor, or a tax ID that does not match. <b>The PO's buyer:</b> price above tolerance, more quantity or value than is open on the PO, a line that is not on the PO, a PO that is already fully billed. <b>AP lead:</b> arithmetic that does not tie, words that disagree with figures, a PO that cannot be chosen with confidence, a possible duplicate, a tax mismatch, a poor scan, or an amount above the straight-through limit."],
    ["var(--green)", "Approve for payment", "Everything above passes. The invoice is posted against the PO, the PO's open quantities are reduced, and payment is scheduled on the agreed terms to the bank account on file."],
  ];
  async function viewPolicy(alive) {
    const data = await api("/api/policy");
    if (!alive()) return;
    let policy = data.policy;
    const draw = () => {
      const groups = [];
      policy.forEach((p) => { if (!groups.includes(p.group)) groups.push(p.group); });
      $("#policy-form").innerHTML = groups.map((g) => `<div class="section-title" style="margin-top:${g === groups[0] ? 0 : 18}px">${esc(g)}</div>` + policy.filter((p) => p.group === g).map((p) => `<div class="field">
          <div><div class="field-label">${esc(p.label)}${Number(p.value) !== Number(p.default) ? ' <span class="pill warn">changed</span>' : ""}</div><div class="field-help">${esc(p.help)} <span class="faint">Default ${p.unit === "₹" ? "₹" + Number(p.default).toLocaleString("en-IN") : esc(p.default) + (p.unit === "%" ? "%" : " " + esc(p.unit))}.</span></div></div>
          <div class="input-unit ${Number(p.value) !== Number(p.default) ? "changed" : ""}"><input type="number" inputmode="decimal" step="any" min="${p.min}" max="${p.max}" value="${esc(p.value)}" data-key="${esc(p.key)}" aria-label="${esc(p.label)}"><span>${esc(p.unit)}</span></div>
        </div>`).join("")).join("");
    };
    view.innerHTML = `<div class="page">
      <div class="page-head"><div><h1 class="page-title">Policy</h1><p class="page-sub">The thresholds the rules use and the order in which a decision is reached. Change a number, process an invoice again, and the outcome changes accordingly. Nothing here is hidden inside a model.</p></div>
        <div class="page-actions"><button class="btn" id="policy-reset" type="button">Restore defaults</button><button class="btn btn-primary" id="policy-save" type="button">Save changes</button></div></div>
      <div class="policy-grid">
        <div class="card"><div class="card-head"><div class="card-title">Thresholds</div><span class="muted" style="font-size:12.5px">Apply from the next run</span></div><div class="card-body" id="policy-form"></div></div>
        <div class="card"><div class="card-head"><div class="card-title">How a decision is reached</div><span class="muted" style="font-size:12.5px">First rule that applies wins</span></div><div class="card-body">
          ${RULEBOOK.map(([color, title, text], i) => `<div class="rule"><span class="rule-n" style="background:${color}">${i + 1}</span><div><div class="rule-title">${title}</div><div class="rule-text">${text}</div></div></div>`).join("")}
          <div class="howto-note">The reader (Claude, or the built-in parser) only turns the document into fields. It has no say in the decision.</div>
        </div></div>
      </div></div>`;
    draw();
    $("#policy-save").addEventListener("click", async () => {
      const values = {};
      $$("#policy-form input").forEach((i) => { values[i.dataset.key] = i.value; });
      try {
        const res = await api("/api/policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }) });
        policy = res.policy; draw(); toast("Policy saved. It applies from the next run");
      } catch (err) { toast(err.message, true); }
    });
    $("#policy-reset").addEventListener("click", async () => {
      try { const res = await api("/api/policy/reset", { method: "POST" }); policy = res.policy; draw(); toast("Defaults restored"); } catch (err) { toast(err.message, true); }
    });
  }

  // ----------------------------------------------------------------- boot ---
  $("#reset-btn").addEventListener("click", async () => {
    const ok = await confirmModal({
      title: "Reset demo data?",
      body: "This clears every run and restores purchase orders, payment history and policy to their starting state.",
      confirm: "Reset", danger: true,
    });
    if (!ok) return;
    try {
      await api("/api/reset", { method: "POST" });
      META = null;
      toast("Demo data reset");
      if ((location.hash || "#/") === "#/") route(); else location.hash = "#/";
    } catch (err) { toast(err.message, true); }
  });
  window.addEventListener("hashchange", route);
  route();
})();
