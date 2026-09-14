/* =========================================================================
   InfraBeatOps — shared front-end helpers.

   One sidebar definition for every page, so adding a route means editing one
   file rather than five copies that slowly disagree.
   ========================================================================= */

const IB = {
  /* ---- formatting -------------------------------------------------- */

  esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  },

  /* A value that could not be read renders as "No data" in muted italics,
     NEVER as 0 and never as a healthy-looking figure. */
  val(v, suffix = "", digits = null) {
    if (v === null || v === undefined || v === "" || v === "N/A") {
      return '<span class="no-data">No data</span>';
    }
    const n = (digits !== null && typeof v === "number") ? v.toFixed(digits) : v;
    return IB.esc(n) + IB.esc(suffix);
  },

  statusClass(s) {
    return "c-" + String(s || "UNKNOWN").toLowerCase().replace(/[^a-z]/g, "");
  },

  /* Collector provenance badge. `stale` is its own state: a two-hour-old
     GUI reading shown as live would be a quiet lie. */
  srcBadge(source, ageMinutes) {
    if (!source) return '<span class="src src-none">no data</span>';
    const s = String(source).toLowerCase();
    let cls = "src-none", label = source;
    if (s.includes("rfc")) { cls = "src-rfc"; label = "RFC LIVE"; }
    else if (s.includes("ssh") || s.includes("linux")) { cls = "src-ssh"; label = "SSH"; }
    else if (s.includes("gui")) { cls = "src-gui"; label = "SAP GUI"; }
    if (ageMinutes !== null && ageMinutes !== undefined && ageMinutes > 15) {
      cls = "src-stale";
      label += ` · ${ageMinutes}m old`;
    }
    return `<span class="src ${cls}">${IB.esc(label)}</span>`;
  },

  pct(used, total) {
    if (!total) return null;
    return Math.round((used / total) * 100);
  },

  barClass(pct) {
    if (pct === null) return "";
    if (pct >= 90) return "c";
    if (pct >= 75) return "w";
    return "";
  },

  /* ---- shell ------------------------------------------------------- */

  NAV: [
    { section: "Command centre" },
    { id: "sky",      label: "Sky view",       icon: "◈", href: "/sky" },
    { id: "overview", label: "Overview",       icon: "▦", href: "/overview" },
    { id: "wall",     label: "Live wall",      icon: "◉", href: "/wall" },
    { id: "systems",  label: "Systems",        icon: "▤", href: "/systems-page" },
    { section: "Analysis" },
    { id: "reports",  label: "Health reports", icon: "◫", href: "/reports-page" },
    { id: "incidents", label: "Incidents",     icon: "⚠", href: "/incidents-page" },
    { id: "correlation", label: "Correlation", icon: "⇄", href: "/correlation-page" },
    { id: "rca",      label: "AI RCA",         icon: "✦", href: "/rca-page" },
    { section: "Operations" },
    { id: "profiles", label: "Profiles",       icon: "◧", href: "/profiles-page" },
    { id: "scheduler", label: "Scheduler",     icon: "◷", href: "/scheduler" },
    { id: "audit",    label: "Audit trail",    icon: "◌", soon: true },
    { id: "legacy",   label: "Classic view",   icon: "▣", href: "/" },
  ],

  renderSidebar(activeId) {
    const items = IB.NAV.map(n => {
      if (n.section) return `<div class="nav-sec">${IB.esc(n.section)}</div>`;
      const cls = "nav-item" + (n.id === activeId ? " active" : "") + (n.soon ? " soon" : "");
      const inner = `<span class="ico">${n.icon}</span><span class="lbl">${IB.esc(n.label)}</span>`;
      return n.soon
        ? `<div class="${cls}" data-label="${IB.esc(n.label)}" title="Backend exists; screen not built yet">${inner}</div>`
        : `<a class="${cls}" data-label="${IB.esc(n.label)}" href="${n.href}">${inner}</a>`;
    }).join("");

    return `
      <aside class="sidebar" aria-label="Navigation">
        <a class="brand" href="/sky" aria-label="IBOPS home">
          <div class="mark"><img src="/static/brand/ibops-mark-64.png" alt=""></div>
          <div class="word"><img src="/static/brand/ibops-word.png" alt="IBOPS"><small>Reach for the sky</small></div>
        </a>
        <nav>${items}</nav>
        <div class="side-foot">
          <div class="t">Collector paths</div>
          <div class="row"><span>RFC live</span><span id="sf-rfc">–</span></div>
          <div class="row"><span>SSH</span><span id="sf-ssh">–</span></div>
          <div class="row"><span>SAP GUI</span><span id="sf-gui">–</span></div>
        </div>
      </aside>`;
  },

  applyTheme() {
    let t = null;
    try { t = localStorage.getItem("ibo-theme"); } catch (e) {}
    document.documentElement.setAttribute("data-theme", t === "dark" ? "dark" : "light");
  },

  /* Display face. Falls back to Segoe UI if the network is closed. */
  loadFonts() {
    if (document.getElementById("ibo-fonts")) return;
    const l = document.createElement("link"); l.id = "ibo-fonts"; l.rel = "stylesheet";
    l.href = "https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&family=JetBrains+Mono:wght@500;700&display=swap";
    document.head.appendChild(l);
  },

  mountShell(activeId) {
    IB.applyTheme();
    IB.loadFonts();
    document.body.insertAdjacentHTML("afterbegin", IB.renderSidebar(activeId));
    IB.heartbeat.mount();
    IB.motion.stagger();
    IB.loader.boot();
  },

  themeToggle(btn) {
    const paint = () => {
      const dark = document.documentElement.getAttribute("data-theme") === "dark";
      btn.textContent = dark ? "◑ Light" : "◐ Dark";
    };
    paint();
    btn.onclick = () => {
      const dark = document.documentElement.getAttribute("data-theme") === "dark";
      const next = dark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("ibo-theme", next); } catch (e) {}
      paint();
    };
  },

  /* ---- motion ------------------------------------------------------
     One orchestrated entrance per page load, then nothing moves unless a
     person acts or data changes. */
  motion: {
    stagger(root) {
      const host = root || document.querySelector(".content");
      if (!host) return;
      Array.from(host.children).slice(0, 14).forEach((el, i) => {
        el.classList.add("rise"); el.style.animationDelay = (60 + i * 45) + "ms";
      });
    },
    /* Count a number up into an element. Used by pages when a value lands. */
    countUp(el, to, opts) {
      if (!el) return;
      const o = Object.assign({ ms: 700, digits: 0, suffix: "" }, opts || {});
      const from = parseFloat(String(el.dataset.v || "0")) || 0;
      const t0 = performance.now();
      const ease = x => 1 - Math.pow(1 - x, 3);
      const step = now => {
        const k = Math.min(1, (now - t0) / o.ms);
        const v = from + (to - from) * ease(k);
        el.textContent = v.toFixed(o.digits) + o.suffix;
        if (k < 1) requestAnimationFrame(step); else el.dataset.v = String(to);
      };
      requestAnimationFrame(step);
    },
  },

  /* ---- heartbeat ---------------------------------------------------
     A trace in the command bar that beats only while RFC reads are
     landing. Pages call IB.heartbeat.beat({live, total, critical}) after
     each poll; with no calls for 20s it goes idle and grey. */
  heartbeat: {
    _el: null, _timer: null,
    mount() {
      const bar = document.querySelector(".topbar");
      if (!bar || this._el) return;
      const actions = bar.querySelector(".top-actions");
      const html = `<div class="hb idle" id="ibo-hb" title="RFC reads landing">
        <svg viewBox="0 0 120 26" aria-hidden="true">
          <path d="M0 13 H18 L24 13 L28 4 L33 22 L38 13 H58 L64 13 L68 6 L73 20 L78 13 H98 L104 13 L108 8 L112 17 L116 13 H120"/>
        </svg><span><b id="ibo-hb-n">–</b> live</span></div>`;
      if (actions) actions.insertAdjacentHTML("beforebegin", html);
      else bar.insertAdjacentHTML("beforeend", html);
      this._el = document.getElementById("ibo-hb");
    },
    beat(info) {
      if (!this._el) this.mount();
      if (!this._el) return;
      const i = info || {};
      this._el.classList.remove("idle");
      this._el.classList.toggle("crit", !!i.critical);
      const n = document.getElementById("ibo-hb-n");
      if (n && i.total != null) n.textContent = `${i.live ?? 0}/${i.total}`;
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._el && this._el.classList.add("idle"), 20000);
    },
  },

  /* ---- poll --------------------------------------------------------
     A drop-in replacement for setInterval that pauses when the tab is
     hidden and cancels on pagehide. Long-open background tabs polling
     /api/live every 8s across five open pages was the biggest cause of
     the app getting slower the longer it stayed open.

     Use: const p = IB.poll(fn, ms);  p.stop();  p.setInterval(newMs); */
  poll(fn, ms) {
    if (typeof fn !== "function") throw new Error("IB.poll(fn, ms): fn required");
    let timer = null, interval = Math.max(500, ms | 0), stopped = false, running = false;
    const run = async () => {
      if (running || stopped || document.hidden) return;
      running = true;
      try { await fn(); }
      catch (e) { console.warn("IB.poll:", e); }
      finally { running = false; }
    };
    const arm = () => { if (timer) clearInterval(timer); timer = setInterval(run, interval); };
    const onVis = () => { if (document.hidden) { if (timer) { clearInterval(timer); timer = null; } }
                         else if (!stopped) { arm(); run(); } };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("pagehide", () => api.stop(), { once: true });
    const api = {
      stop() { stopped = true; if (timer) clearInterval(timer); timer = null;
               document.removeEventListener("visibilitychange", onVis); },
      setInterval(newMs) { interval = Math.max(500, newMs | 0); if (!document.hidden && !stopped) arm(); },
      trigger() { return run(); },
    };
    if (!document.hidden) arm();
    run();
    return api;
  },


  /* ---- attribution renderer -------------------------------------
     "Who and what" behind the counters. Same block on RCA, system page
     and classic view. Deterministic data; renders nothing for empty
     sections so a healthy system shows a short list, not empty tables. */
  renderAttribution(a) {
    if (!a) return "";
    const esc = IB.esc, blocks = [];
    const tbl = (head, rows) => rows.length
      ? `<table class="attr"><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr>${rows.join("")}</table>` : "";
    const td = (...c) => `<tr>${c.map(x => `<td>${x}</td>`).join("")}</tr>`;
    const mono = s => `<span class="mono">${esc(s ?? "—")}</span>`;
    const mail = e => e ? ` <a href="mailto:${esc(e)}" style="color:var(--sky-600)">${esc(e)}</a>` : "";
    const team = t => t ? `<span class="src ${/ABAP/.test(t) ? "src-gui" : "src-rfc"}">${esc(t)}</span>` : "";

    const d = a.dumps || {};
    if ((d.total_today || 0) > 0) {
      const users = (d.by_user || []).map(u => td(mono(u.user) + mail(u.email), mono(u.count)));
      const progs = (d.by_program || []).map(p => td(mono(p.program), mono(p.count), team(p.team)));
      const hosts = (d.by_host || []).map(h => td(mono(h.host), mono(h.count)));
      blocks.push(`<div class="attr-sec"><h5>Dumps today · ${esc(d.total_today)}</h5>
        <div class="attr-grid">
          <div><div class="attr-cap">By user</div>${tbl(["User", "Dumps"], users) || "<div class='empty-s'>no user breakdown</div>"}</div>
          <div><div class="attr-cap">By program · team</div>${tbl(["Program", "Dumps", "Team"], progs) ||
            "<div class='empty-s'>program names not readable over RFC — open ST22</div>"}</div>
          <div><div class="attr-cap">By host</div>${tbl(["Host", "Dumps"], hosts)}</div>
        </div></div>`);
    }

    const j = a.jobs || {};
    if ((j.cancelled || []).length || (j.long_running || []).length) {
      const canc = (j.cancelled || []).map(x => td(
        mono(x.job) + (x.is_backup ? ' <span class="chip c-critical">backup</span>' : ""),
        mono(x.owner) + mail(x.owner_email), mono(x.started_at) + " → " + mono(x.ended_at), esc(x.why)));
      const lr = (j.long_running || []).map(x => td(mono(x.job), mono(x.owner) + mail(x.owner_email),
        mono(x.started_at), mono(x.running_for), esc(x.why)));
      blocks.push(`<div class="attr-sec"><h5>Background jobs</h5>
        ${canc.length ? `<div class="attr-cap">Cancelled</div>${tbl(["Job", "Owner", "Ran", "Why"], canc)}` : ""}
        ${lr.length ? `<div class="attr-cap" style="margin-top:10px">Long-running</div>${tbl(["Job", "Owner", "Started", "Running for", "Why"], lr)}` : ""}
      </div>`);
    }

    const w = a.work_processes || {};
    if ((w.long_running || []).length || (w.priv_mode || []).length) {
      const lr = (w.long_running || []).map(x => td(mono("WP " + (x.wp ?? "?")), mono(x.user), mono(x.report), team(x.team), mono(x.elapsed), esc(x.why)));
      const pv = (w.priv_mode || []).map(x => td(mono("WP " + (x.wp ?? "?")), mono(x.user), mono(x.report), team(x.team), mono(x.elapsed), esc(x.why)));
      blocks.push(`<div class="attr-sec"><h5>Work processes · ${esc(w.in_use ?? "?")}/${esc(w.total ?? "?")} in use</h5>
        ${lr.length ? `<div class="attr-cap">Long-running</div>${tbl(["WP", "User", "Report", "Team", "Elapsed", "Why"], lr)}` : ""}
        ${pv.length ? `<div class="attr-cap" style="margin-top:10px">PRIV mode</div>${tbl(["WP", "User", "Report", "Team", "Elapsed", "Why"], pv)}` : ""}
      </div>`);
    }

    const r = a.response || {};
    if (r.avg_ms != null) {
      const inst = Object.entries(r.per_instance || {}).map(([k, v]) => td(mono(k), mono(v + " ms")));
      const users = (r.top_users || []).map(u => td(mono(u.user), mono(Math.round((u.total_ms || 0) / 1000) + " s total")));
      const rep = (r.top_reports_by_response || []).map(x => td(mono(x.report), mono((x.avg_ms ?? x.resp_ms ?? "") + " ms"), team(x.team)));
      const db = (r.top_reports_by_db_time || []).map(x => td(mono(x.report), mono((x.db_ms ?? x.total_db_ms ?? "") + " ms DB"), team(x.team)));
      blocks.push(`<div class="attr-sec"><h5>Dialog response · ${esc(r.avg_ms)} ms
          <span class="chip ${IB.statusClass(r.status || "UNKNOWN")}">${esc(r.status || "")}</span>
          <span class="src src-none">${esc(r.window || "")}${r.steps != null ? " · " + esc(r.steps) + " steps" : ""}</span></h5>
        ${r.low_sample ? `<div class="banner banner-warn" style="margin:8px 0">Only ${esc(r.steps)} dialog step(s) in the window — too thin to attribute.</div>` : ""}
        <div class="attr-grid">
          <div><div class="attr-cap">Per instance</div>${tbl(["Instance", "Avg"], inst)}</div>
          <div><div class="attr-cap">Who carries it</div>${tbl(["User", "Response"], users) || "<div class='empty-s'>no per-user breakdown in this window</div>"}</div>
          <div><div class="attr-cap">Which reports</div>${tbl(["Report", "Avg", "Team"], rep) || tbl(["Report", "DB time", "Team"], db) || "<div class='empty-s'>no per-report breakdown</div>"}</div>
        </div>
        ${r.db_share_pct != null ? `<div class="attr-note">${esc(r.db_share_pct)}% of response time is spent in the database${db.length ? " — DB-heavy reports above" : ""}.</div>` : ""}
      </div>`);
    }

    const l = a.locks || {};
    if (l.oldest_minutes != null && l.oldest_minutes > 0) {
      const tu = (l.top_users || []).map(x => td(mono(Array.isArray(x) ? x[0] : x.user), mono(Array.isArray(x) ? x[1] : x.count)));
      blocks.push(`<div class="attr-sec"><h5>Locks · oldest ${esc(l.oldest_minutes)} min</h5>
        <div class="attr-grid"><div><div class="attr-cap">Oldest lock</div><div class="mono" style="font-size:12px">${esc(l.oldest_detail || "")}</div>
        ${l.clock_offset_min ? `<div class="attr-note">App-server clock is ${esc(l.clock_offset_min)} min off SAP time — ages corrected; tell Basis.</div>` : ""}</div>
        <div><div class="attr-cap">Most locks</div>${tbl(["User", "Locks"], tu)}</div></div>
        <div class="attr-note">${esc(l.why || "")}</div></div>`);
    }

    const gaps = (a.gaps || []).filter(Boolean);
    const gapHtml = gaps.length ? `<div class="attr-gaps"><b>Not available over RFC:</b> ${gaps.map(esc).join(" · ")}</div>` : "";
    if (!blocks.length) return gapHtml ? `<div class="attr">${gapHtml}</div>` : "";
    return `<div class="attr">${blocks.join("")}${gapHtml}</div>`;
  },

  /* ---- loader (3D brand) ------------------------------------------
     boot(): splash on first load per tab session; hidden once the window
     has loaded and at least MIN_MS passed, never longer than MAX_MS.
     show(msg)/hide(): for pages to wrap long operations. */
  loader: {
    // MIN_MS used to be 1400: every first page load was held on a splash
    // for at least 1.4s regardless of how fast the server answered, which
    // is precisely what "pages take a long time to load" feels like. It
    // now hides as soon as the window has loaded -- MIN_MS only prevents a
    // sub-frame flash. MAX_MS is the safety ceiling.
    MIN_MS: 150, MAX_MS: 4000, _el: null, _t0: 0,
    _html(msg) {
      return `<div class="ibo-loader" role="status" aria-live="polite"><div>
          <div class="frame">
            <video autoplay muted loop playsinline preload="auto" poster="/static/brand/ibops-3d-poster.jpg">
              <source src="/static/brand/ibops-3d.mp4" type="video/mp4"></video>
            <img src="/static/brand/ibops-3d-poster.jpg" alt="" style="display:none">
          </div>
          <div class="msg" id="ibo-loader-msg">${IB.esc(msg || "Reading systems…")}</div>
          <div class="bar"><i></i></div></div></div>`;
    },
    show(msg) {
      if (this._el) { this.message(msg); this._el.classList.remove("hide"); return; }
      document.body.insertAdjacentHTML("beforeend", this._html(msg));
      this._el = document.body.lastElementChild; this._t0 = performance.now();
      if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
        const img = this._el.querySelector("img"); if (img) img.style.display = "block";
      }
    },
    message(msg) { const m = document.getElementById("ibo-loader-msg"); if (m && msg) m.textContent = msg; },
    hide() {
      const el = this._el; if (!el) return;
      const wait = Math.max(0, this.MIN_MS - (performance.now() - this._t0));
      setTimeout(() => { el.classList.add("hide");
        setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 450); this._el = null; }, wait);
    },
    boot() {
      let seen = false;
      try { seen = sessionStorage.getItem("ibo-booted") === "1"; } catch (e) {}
      if (seen) return;
      try { sessionStorage.setItem("ibo-booted", "1"); } catch (e) {}
      this.show("Reading systems…");
      const done = () => this.hide();
      if (document.readyState === "complete") done(); else window.addEventListener("load", done, { once: true });
      setTimeout(done, this.MAX_MS);
    },
  },

  /* ---- loading indicator -------------------------------------------- */

  /* One indicator for the whole app, driven by the number of requests in
     flight rather than by each page remembering to switch it on and off.

     Counted, not boolean: the wall fires several reads at once and a plain
     flag would be cleared by whichever finished first, hiding the badge
     while three requests were still running. The badge only appears after a
     short delay -- a cached 20 ms response should not produce a flash of
     spinner, which reads as jank rather than as feedback. Now that RFC runs
     on a 60s background clock most reads return instantly and the indicator
     stays out of the way; it shows up exactly when something is genuinely
     waiting, which is the case it exists for. */

  busy: {
    _inflight: 0,
    _timer: null,
    DELAY_MS: 220,

    _el() {
      let el = document.getElementById("ibo-loading");
      if (el) return el;
      el = document.createElement("div");
      el.id = "ibo-loading";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML =
        '<img src="/static/brand/ibops-mark-64.png" alt="">' +
        '<span class="ibo-loading-text">Reading live RFC…</span>';
      document.body.appendChild(el);
      return el;
    },

    start(label) {
      this._inflight += 1;
      if (label) this._label = label;
      if (this._timer !== null) return;
      this._timer = setTimeout(() => {
        this._timer = null;
        if (this._inflight <= 0) return;
        const el = this._el();
        const text = el.querySelector(".ibo-loading-text");
        if (text && this._label) text.textContent = this._label;
        el.classList.add("on");
      }, this.DELAY_MS);
    },

    stop() {
      this._inflight = Math.max(0, this._inflight - 1);
      if (this._inflight > 0) return;
      if (this._timer !== null) { clearTimeout(this._timer); this._timer = null; }
      const el = document.getElementById("ibo-loading");
      if (el) el.classList.remove("on");
    },

    /* Markup for a panel that has no data yet. Pages drop this into the
       container they are about to fill, so the first RFC pass shows the mark
       where the content will be instead of a bare "Reading live RFC…" line
       or, worse, an empty box that looks broken. */
    inline(label) {
      return '<div class="ibo-loading-inline">' +
             '<img src="/static/brand/ibops-mark-64.png" alt="">' +
             '<span>' + (label || "Reading live RFC…") + "</span></div>";
    },
  },

  /* ---- data -------------------------------------------------------- */

  async get(url) {
    this.busy.start();
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) throw new Error(`${url} → ${r.status}`);
      return r.json();
    } finally {
      this.busy.stop();
    }
  },

  /* State-changing endpoints require X-IBO-Token. The token is served by
     /api/auth-token, which is bound to 127.0.0.1 like the rest of the app --
     it is a same-host control, not a defence against someone who already has
     the machine.

     Every mutating call goes through here so the header cannot be forgotten
     on one button and silently break it. A 401 is reported as a 401 rather
     than swallowed: a Pause button that quietly does nothing is how
     monitoring stops without anyone noticing. */

  _token: null,

  async token() {
    if (this._token !== null) return this._token;
    try {
      const r = await fetch("/api/auth-token", { cache: "no-store" });
      this._token = r.ok ? (await r.json()).token || "" : "";
    } catch (e) {
      this._token = "";
    }
    return this._token;
  },

  async post(url, options) {
    // Mutating calls are the ones most likely to be slow -- an on-demand RCA
    // analyse waits on a live RFC read AND a model call -- so these get the
    // indicator as well, with a label that says what is being waited on.
    this.busy.start((options && options.label) || "Working…");
    try {
      const token = await this.token();
      const r = await fetch(url, Object.assign(
        { method: "POST", cache: "no-store" }, options || {},
        { headers: Object.assign({ "X-IBO-Token": token },
                                 (options || {}).headers || {}) }));
      if (r.status === 401 || r.status === 503) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail
          || "Not authorised. Check IBO_API_TOKEN in .env and restart.");
      }
      return r;
    } finally {
      this.busy.stop();
    }
  },

  async del(url) {
    return this.post(url, { method: "DELETE" });
  },

  /* Counts how many systems are configured for each collection path, so the
     sidebar reports configuration rather than guessing. */
  updatePathCounts(systems) {
    const n = systems.length;
    const has = key => systems.filter(s => (s.paths || []).includes(key)).length;
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set("sf-rfc", `${has("RFC")} / ${n}`);
    set("sf-ssh", `${has("SSH")} / ${n}`);
    set("sf-gui", `${has("GUI")} / ${n}`);
  },
};

/* ---- automatic monitoring control ---------------------------------------
   Lives outside the IB object literal (a function declaration inside one is
   a syntax error) and is attached to IB afterwards.

   Kept in the shell so every page shows the SAME state: a pause made on the
   wall must be visible on the systems page, or an operator will assume it
   did not take and pause it again.
   ------------------------------------------------------------------------ */

async function _refreshScheduler(el) {
  let s;
  try {
    s = await IB.get("/api/scheduler");
  } catch (e) {
    el.innerHTML = "";
    return;
  }

  const on = s.enabled;

  // Nothing runs unless a profile schedules it. "No profiles" is reported
  // rather than shown as "scheduled" -- a scheduler with nothing to run and
  // one that is broken look identical if the label says the same thing.
  const noProfiles = s.source === "profiles" && !s.enabled_profiles;

  const detail = s.running
    ? `running${s.current_system ? " · " + IB.esc(s.current_system) : ""}` +
      `${s.current_profile ? " · " + IB.esc(s.current_profile) : ""}`
    : noProfiles ? "no profiles scheduled"
    : (on ? (s.next_run_at ? "next " + IB.esc(String(s.next_run_at).slice(11, 16))
                           : "scheduled")
          : "paused");

  // Status only. Pause, Resume and Snooze are gone: scheduling now lives
  // entirely in profiles, and a global pause alongside per-profile enable
  // gave two switches for one decision -- the state anyone actually wants to
  // know is "which profiles will run", and that is on the Profiles page.
  el.innerHTML =
    `<a href="/profiles-page" class="chip ${noProfiles ? "c-unknown"
        : s.running ? "c-live" : "c-normal"}"` +
    ` title="Monitoring runs only when a profile schedules it. Open Profiles."` +
    ` style="text-decoration:none">${IB.esc(detail)}</a>`;
}

/* Run / Stop control, with schedule information underneath.

   The button alone answers "can I start one?" but not "did one just run?"
   or "when is the next?" -- which is what an operator actually needs before
   deciding whether to trigger anything. Both lines sit under the button. */

function _ago(stamp) {
  if (!stamp) return null;
  const t = new Date(String(stamp).replace(" ", "T"));
  if (isNaN(t)) return null;
  const mins = Math.max(0, Math.round((Date.now() - t.getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const h = Math.floor(mins / 60);
  return h < 24 ? `${h}h ${mins % 60}m ago` : `${Math.floor(h / 24)}d ago`;
}

async function _refreshRunControl(el) {
  let s;
  try {
    s = await IB.get("/api/scheduler");
  } catch (e) {
    return;
  }

  // "Run sweep" is gone. It swept every configured system on demand, which
  // is the same surprise the interval scheduler used to cause: minutes of
  // SAP GUI driving and an email, started by a button that did not say which
  // systems it would touch. Runs are started from a named profile, or
  // per-system from that system's card.
  //
  // Stop stays, and only while something is running -- a sweep in progress
  // is the one thing an operator genuinely needs to interrupt.
  if (!s.running) {
    const next = s.next_run_at
      ? "next " + IB.esc(String(s.next_run_at).slice(5, 16))
      : (s.enabled_profiles ? "nothing due" : "no profiles scheduled");
    const last = _ago(s.last_cycle_start);
    el.innerHTML = `
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
        <a href="/profiles-page" class="btn" style="text-decoration:none">Profiles</a>
        <div style="font-size:10.5px;color:var(--muted-dim);font-family:var(--mono)">
          ${last ? "last run " + IB.esc(last) : "no run yet"} · ${next}
        </div>
      </div>`;
    return;
  }

  el.innerHTML = `
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
      <button class="btn" id="run-btn"
              style="border-color:var(--red);color:var(--red)">
        ■ Stop${s.current_system ? " (" + IB.esc(s.current_system) + ")" : ""}</button>
      <div style="font-size:10.5px;color:var(--muted-dim);font-family:var(--mono)">
        ${s.current_profile ? IB.esc(s.current_profile) + " · " : ""}sweep in progress
      </div>
    </div>`;

  document.getElementById("run-btn").onclick = async () => {
    const b = document.getElementById("run-btn");
    b.disabled = true;
    b.textContent = "Stopping…";
    try {
      const r = await IB.post("/api/run-stop");
      const d = await r.json();
      if (d.note) console.info(d.note);
      // The system in flight finishes first -- aborting mid-sweep would
      // leave SAP Logon open and a half-written snapshot behind.
      b.textContent = "Finishing current system…";
    } catch (e) { console.error(e); }
    setTimeout(() => _refreshRunControl(el), 2000);
  };
}

IB.mountRunControl = function (containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  _refreshRunControl(el);
  // IB.poll (not setInterval): a hidden tab stops polling, so background
  // tabs no longer queue requests behind the single SAP GUI thread.
  IB.poll(() => _refreshRunControl(el), 5000);
};

IB.mountSchedulerControl = function (containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  _refreshScheduler(el);
  IB.poll(() => _refreshScheduler(el), 15000);
};

/* Shared "no data" styling, injected once so pages need not repeat it. */
document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent =
    ".no-data{color:var(--muted-dim);font-style:italic;font-weight:600;font-size:.92em}";
  document.head.appendChild(style);
});
