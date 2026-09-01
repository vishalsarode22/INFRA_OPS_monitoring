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
    { id: "overview", label: "Overview",       icon: "▦", href: "/overview" },
    { id: "wall",     label: "Live wall",      icon: "◉", href: "/wall" },
    { id: "systems",  label: "Systems",        icon: "▤", href: "/systems-page" },
    { section: "Analysis" },
    { id: "reports",  label: "Health reports", icon: "◫", href: "/reports-page" },
    { id: "incidents", label: "Incidents",     icon: "⚠", soon: true },
    { id: "correlation", label: "Correlation", icon: "⇄", soon: true },
    { id: "rca",      label: "AI RCA",         icon: "✦", soon: true },
    { section: "Operations" },
    { id: "scheduler", label: "Scheduler",     icon: "◷", href: "/scheduler" },
    { id: "audit",    label: "Audit trail",    icon: "◌", soon: true },
    { id: "legacy",   label: "Classic view",   icon: "▣", href: "/" },
  ],

  renderSidebar(activeId) {
    const items = IB.NAV.map(n => {
      if (n.section) return `<div class="nav-sec">${IB.esc(n.section)}</div>`;
      const cls = "nav-item" + (n.id === activeId ? " active" : "") + (n.soon ? " soon" : "");
      const inner = `<span class="ico">${n.icon}</span> ${IB.esc(n.label)}`;
      // Unbuilt sections are shown greyed rather than linked to a dead page.
      return n.soon
        ? `<div class="${cls}" title="Backend exists; screen not built yet">${inner}</div>`
        : `<a class="${cls}" href="${n.href}">${inner}</a>`;
    }).join("");

    return `
      <aside class="sidebar">
        <div class="brand">
          <div class="logo">IB</div>
          <div><h1>InfraBeatOps</h1><small>SAP Basis Intelligence</small></div>
        </div>
        <nav>${items}</nav>
        <div class="side-foot">
          <div class="t">Collector paths</div>
          <div class="row"><span>RFC live</span><span id="sf-rfc">–</span></div>
          <div class="row"><span>SSH</span><span id="sf-ssh">–</span></div>
          <div class="row"><span>SAP GUI</span><span id="sf-gui">–</span></div>
        </div>
      </aside>`;
  },

  mountShell(activeId) {
    document.body.insertAdjacentHTML("afterbegin", IB.renderSidebar(activeId));
  },

  themeToggle(btn) {
    btn.onclick = () => {
      const dark = document.documentElement.getAttribute("data-theme") !== "light";
      document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
      btn.textContent = dark ? "◑ Light" : "◐ Dark";
    };
  },

  /* ---- data -------------------------------------------------------- */

  async get(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    return r.json();
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
  const detail = s.running
    ? `running${s.current_system ? " · " + IB.esc(s.current_system) : ""}`
    : (on ? (s.next_run_at ? "next " + IB.esc(String(s.next_run_at).slice(11, 16))
                           : "scheduled")
          : "paused");

  el.innerHTML =
    `<span class="chip ${on ? "c-normal" : "c-unknown"}" title="Automatic monitoring">` +
    `${on ? "AUTO ON" : "AUTO PAUSED"} · ${detail}</span>` +
    `<button class="btn" id="sched-btn">${on ? "Pause auto" : "Resume auto"}</button>`;

  document.getElementById("sched-btn").onclick = async () => {
    const btn = document.getElementById("sched-btn");
    btn.disabled = true;
    btn.textContent = on ? "Pausing…" : "Resuming…";
    try {
      const r = await fetch("/api/scheduler/" + (on ? "pause" : "resume"),
                            { method: "POST" });
      const d = await r.json();
      // A sweep already under way is allowed to finish: aborting mid-run
      // would leave SAP Logon open and a half-written snapshot behind.
      if (d.note) console.info(d.note);
    } catch (e) {}
    _refreshScheduler(el);
  };
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

  const btn = s.running
    ? `<button class="btn" id="run-btn" style="border-color:var(--red);color:var(--red)">` +
      `■ Stop${s.current_system ? " (" + IB.esc(s.current_system) + ")" : ""}</button>`
    : `<button class="btn btn-primary" id="run-btn">Run sweep</button>`;

  // Next-run wording states WHY nothing is scheduled, rather than an
  // ambiguous dash that could mean "never" or "not configured".
  let next;
  if (s.running)        next = "sweep in progress";
  else if (s.snoozed)   next = `snoozed until ${IB.esc(s.snooze_until.slice(11))}`;
  else if (s.paused)    next = "paused — nothing scheduled";
  else if (s.next_run_at) next = "next " + IB.esc(s.next_run_at.slice(5, 16));
  else                  next = "no system due";

  const last = _ago(s.last_cycle_start);

  el.innerHTML = `
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
      <div style="display:flex;gap:8px;align-items:center">
        ${btn}
        <select class="input" id="snooze-sel" title="Suppress automatic monitoring"
                style="padding:6px 8px;font-size:11.5px">
          <option value="">Snooze…</option>
          <option value="30">30 minutes</option>
          <option value="60">1 hour</option>
          <option value="240">4 hours</option>
          <option value="480">8 hours</option>
          <option value="1440">1 day</option>
          <option value="4320">3 days</option>
          <option value="10080">7 days</option>
        </select>
      </div>
      <div style="font-size:10.5px;color:var(--muted-dim);font-family:var(--mono)">
        ${last ? "last sweep " + IB.esc(last) : "no sweep yet"} · ${next}
      </div>
    </div>`;

  document.getElementById("run-btn").onclick = async () => {
    const b = document.getElementById("run-btn");
    b.disabled = true;
    if (s.running) {
      b.textContent = "Stopping…";
      try {
        const r = await fetch("/api/run-stop", { method: "POST" });
        const d = await r.json();
        if (d.note) console.info(d.note);
        // The system in flight finishes first; say so rather than looking hung.
        b.textContent = "Finishing current system…";
      } catch (e) {}
    } else {
      b.textContent = "Starting…";
      try { await fetch("/api/run-now", { method: "POST" }); } catch (e) {}
    }
    setTimeout(() => _refreshRunControl(el), 2000);
  };

  document.getElementById("snooze-sel").onchange = async (ev) => {
    const mins = ev.target.value;
    if (!mins) return;
    try {
      const r = await fetch("/api/scheduler/snooze?minutes=" + encodeURIComponent(mins),
                            { method: "POST" });
      const d = await r.json();
      if (d.until) console.info("Automatic monitoring resumes at " + d.until);
    } catch (e) {}
    _refreshRunControl(el);
  };
}

IB.mountRunControl = function (containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  _refreshRunControl(el);
  setInterval(() => _refreshRunControl(el), 5000);
};

IB.mountSchedulerControl = function (containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  _refreshScheduler(el);
  setInterval(() => _refreshScheduler(el), 15000);
};

/* Shared "no data" styling, injected once so pages need not repeat it. */
document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent =
    ".no-data{color:var(--muted-dim);font-style:italic;font-weight:600;font-size:.92em}";
  document.head.appendChild(style);
});
