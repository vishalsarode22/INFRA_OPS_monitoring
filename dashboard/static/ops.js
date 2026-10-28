/* =========================================================================
   InfraBeatOps -- operations helpers shared by the wall and the Operations,
   Timeline, Health and Runbook pages: a small dialog, toasts, and the
   calls behind Snooze / Acknowledge / Ignore until fixed / Maintenance /
   Park / one-click actions. Needs shell.js (IB) first.
   ========================================================================= */
(function () {
  const css = `
  .ops-ov{position:fixed;inset:0;background:rgba(7,18,31,.55);display:flex;align-items:center;
    justify-content:center;z-index:1000;padding:16px}
  .ops-dlg{background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:16px;
    box-shadow:var(--shadow-lift);width:min(560px,100%);max-height:90vh;overflow:auto}
  .ops-dlg h3{font-size:15px;font-weight:800;padding:16px 18px;border-bottom:1px solid var(--border);margin:0}
  .ops-dlg .b{padding:16px 18px;font-size:13px;line-height:1.5}
  .ops-dlg .f{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px;border-top:1px solid var(--border)}
  .ops-dlg label{display:block;font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
    color:var(--muted-dim);margin:12px 0 5px}
  .ops-dlg .input,.ops-dlg textarea{width:100%;box-sizing:border-box}
  .ops-dlg textarea{min-height:70px;padding:9px 12px;border:1px solid var(--hair-2);border-radius:12px;
    background:var(--card);color:var(--text);font:inherit}
  .ops-dlg .pv{background:var(--info-bg);border:1px solid var(--info-br);border-radius:10px;padding:10px 12px;
    margin-top:8px;font-family:var(--mono);font-size:11.5px;white-space:pre-wrap}
  .ops-dlg .er{color:var(--red);margin-top:8px;font-size:12.5px}
  .ops-toast{position:fixed;right:18px;bottom:18px;z-index:1001;padding:11px 16px;border-radius:12px;
    font-size:12.5px;font-weight:600;box-shadow:var(--shadow-lift);max-width:420px}
  .ops-toast.ok{background:var(--green-bg);color:var(--green);border:1px solid var(--green-br)}
  .ops-toast.err{background:var(--red-bg);color:var(--red);border:1px solid var(--red-br)}
  .ops-strip{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px 16px;
    border-bottom:1px solid var(--border);font-size:11px}
  .ops-tag{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-weight:700;
    font-size:10.5px;border:1px solid var(--gray-br);background:var(--gray-bg);color:var(--gray)}
  .ops-tag.warn{border-color:var(--amber-br);background:var(--amber-bg);color:var(--amber)}
  .ops-tag.bad{border-color:var(--red-br);background:var(--red-bg);color:var(--red)}
  .ops-tag.info{border-color:var(--info-br);background:var(--info-bg);color:var(--info)}
  .ops-tag.ok{border-color:var(--green-br);background:var(--green-bg);color:var(--green)}
  .ops-btn{padding:3px 9px;font-size:10.5px;border-radius:8px}
  .ops-menu{position:relative;display:inline-block}
  .ops-menu .m{position:absolute;right:0;top:100%;margin-top:4px;background:var(--card);border:1px solid var(--border);
    border-radius:12px;box-shadow:var(--shadow-lift);z-index:50;min-width:220px;padding:6px;display:none}
  .ops-menu.open .m{display:block}
  .ops-menu .m button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--text);
    padding:8px 10px;border-radius:8px;font:inherit;font-size:12px;cursor:pointer}
  .ops-menu .m button:hover{background:var(--card-hover)}
  .ops-menu .m hr{border:0;border-top:1px solid var(--border);margin:4px 0}
  .sys.s-maint{border:1px solid var(--gray-br);border-top:3px solid var(--gray);filter:grayscale(.85);opacity:.82}
  .sys.s-maint .sh{background:var(--gray-bg)}`;
  const st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);
})();

const OPS = {
  esc: s => IB.esc(s).replace(/'/g, "&#39;"),

  async post(url, body, label) {
    const r = await IB.post(url, { headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body || {}), label: label || "Working…" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const det = d.detail;
      throw new Error(typeof det === "string" ? det : (det ? JSON.stringify(det) : `HTTP ${r.status}`));
    }
    return d;
  },

  toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "ops-toast " + (kind || "ok");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 7000 : 3500);
  },

  /* A dialog. `fields` is HTML for inputs; `onOk(form)` may throw to show an
     error inside the dialog and keep it open. Resolves with onOk's result,
     or null when cancelled. */
  dialog(title, bodyHtml, okLabel, onOk) {
    return new Promise(resolve => {
      const ov = document.createElement("div");
      ov.className = "ops-ov";
      ov.innerHTML = `<form class="ops-dlg"><h3>${OPS.esc(title)}</h3><div class="b">${bodyHtml}
        <div class="er"></div></div><div class="f"><button type="button" class="btn" data-x>Cancel</button>
        ${okLabel ? `<button type="submit" class="btn btn-brand">${OPS.esc(okLabel)}</button>` : ""}</div></form>`;
      document.body.appendChild(ov);
      const form = ov.querySelector("form");
      const close = v => { ov.remove(); resolve(v); };
      ov.querySelector("[data-x]").onclick = () => close(null);
      ov.addEventListener("click", e => { if (e.target === ov) close(null); });
      form.onsubmit = async e => {
        e.preventDefault();
        const err = form.querySelector(".er");
        err.textContent = "";
        const sub = form.querySelector("[type=submit]");
        if (sub) sub.disabled = true;
        try { close(onOk ? await onOk(new FormData(form), form) : true); }
        catch (ex) { err.textContent = ex.message || String(ex); if (sub) sub.disabled = false; }
      };
      const first = form.querySelector("input,select,textarea");
      if (first) first.focus();
    });
  },

  localStamp(d) {
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  },

  /* ---- alert control ------------------------------------------------ */

  async silence(system, kind, mode, minutes) {
    const what = kind && kind !== "*" ? kind.replace(/_/g, " ") + " alerts" : "all alerts";
    const label = mode === "snooze" ? `Snooze ${what} for ${minutes >= 60 ? minutes / 60 + " h" : minutes + " min"}`
                : mode === "ack" ? `Acknowledge ${what}` : `Ignore ${what} until fixed`;
    const extra = mode === "until_fixed"
      ? "<p>Scheduled sweeps of this system are also <b>parked</b> until someone clears the flag.</p>" : "";
    return OPS.dialog(label + ` — ${system}`,
      `${extra}<label>Note (optional, shown on the timeline)</label><input class="input" name="reason" maxlength="200">`,
      "Confirm", async f => {
        await OPS.post("/api/ops/silence", { system, kind: kind || "*", mode, minutes, reason: f.get("reason") });
        OPS.toast(label + " — done");
        return true;
      });
  },

  async clearSilence(id) {
    await OPS.post(`/api/ops/silence/${id}/clear`, {});
    OPS.toast("Cleared");
  },

  async park(system) {
    return OPS.dialog(`Park sweeps — ${system}`,
      `<p>Scheduled sweeps of ${OPS.esc(system)} are skipped until someone clears the flag. A manual
       "Monitor this system" still runs.</p><label>Why</label><input class="input" name="reason" required maxlength="200">`,
      "Park", async f => { await OPS.post("/api/ops/park", { system, reason: f.get("reason") }); OPS.toast("Parked"); return true; });
  },

  async unpark(system) {
    await OPS.post("/api/ops/unpark", { system });
    OPS.toast(`Sweeps resumed for ${system}`);
  },

  async maintenance(system) {
    const now = new Date();
    const end = new Date(now.getTime() + 2 * 3600 * 1000);
    return OPS.dialog(`Maintenance window — ${system === "*" ? "all systems" : system}`,
      `<p>No alerts and no scheduled sweeps for the window; the wall shows the system in grey.</p>
       <label>From</label><input class="input" type="datetime-local" name="starts_at" value="${OPS.localStamp(now)}" required>
       <label>Until</label><input class="input" type="datetime-local" name="ends_at" value="${OPS.localStamp(end)}" required>
       <label>Reason (shown on the wall and the timeline)</label>
       <input class="input" name="reason" required maxlength="200" placeholder="kernel upgrade, restart, DB maintenance…">`,
      "Set window", async f => {
        await OPS.post("/api/ops/maintenance", { system, starts_at: f.get("starts_at").replace("T", " "),
                                                 ends_at: f.get("ends_at").replace("T", " "), reason: f.get("reason") });
        OPS.toast("Maintenance window set");
        return true;
      });
  },

  async endMaintenance(id) {
    await OPS.post(`/api/ops/maintenance/${id}/cancel`, {});
    OPS.toast("Maintenance window ended");
  },

  /* ---- one-click SAP actions --------------------------------------- */

  async action(system, action, params, label) {
    let pv;
    try {
      pv = await OPS.post("/api/ops/actions/preview", { system, action, params }, "Checking in SAP…");
    } catch (e) {
      return OPS.dialog(label || "Action", `<p style="color:var(--red)">${OPS.esc(e.message)}</p>`, null);
    }
    const two = pv.mode === "two_person" && !pv.standing_approval;
    const note = pv.standing_approval
      ? `Runs as soon as you confirm, under the standing approval by ${OPS.esc(pv.standing_approval.by)} (until ${OPS.esc(String(pv.standing_approval.until).slice(11, 16))}).`
      : two ? "A second person must approve it on the Operations page (approval policy)." : "It runs as soon as you confirm.";
    return OPS.dialog(`${label || action} — ${system}`,
      `<p>This will run in SAP:</p><div class="pv">${OPS.esc(pv.preview.text)}</div>
       <label>Reason (goes into the action log and the timeline)</label>
       <input class="input" name="reason" required maxlength="300">
       <p style="margin-top:10px;color:var(--muted)">${note}</p>`,
      two ? "Request" : "Confirm and run", async f => {
        const d = await OPS.post("/api/ops/actions/request",
          { system, action, params, reason: f.get("reason"), approve_now: !two }, "Running in SAP…");
        OPS.toast(two ? "Requested — waiting for approval" : "Started — see the result on the Operations page");
        return d.action;
      });
  },

  async pick(system, action, label) {
    let list = [];
    try {
      list = (await IB.get(`/api/ops/actions/candidates?system=${encodeURIComponent(system)}&action=${encodeURIComponent(action)}`)).candidates || [];
    } catch (e) { list = []; }
    const manual = action === "restart_queue"
      ? `<label>Or type the queue name (SMQ2)</label><input class="input" name="queue" maxlength="60">` : "";
    if (!list.length && !manual) {
      return OPS.dialog(label, "<p>Nothing to act on right now (or the system could not be read).</p>", null);
    }
    const chosen = await OPS.dialog(`${label} — ${system}`,
      (list.length ? `<label>Choose</label><select class="input" name="i">${list.map((c, i) =>
        `<option value="${i}">${OPS.esc(c.label)}</option>`).join("")}</select>` : "<p>No candidates were read.</p>") + manual,
      "Next", async f => {
        if (manual && f.get("queue")) return { queue: f.get("queue").trim() };
        const c = list[Number(f.get("i"))];
        if (!c) throw new Error("Choose one.");
        const params = Object.assign({}, c); delete params.label; delete params.by;
        return params;
      });
    if (chosen) return OPS.action(system, action, chosen, label);
    return null;
  },

  actionsPanel(s, allowed) {
    if (!allowed || !allowed.length) return "";
    const esc = OPS.esc, sysArg = esc(JSON.stringify(s.system));
    const jobs = ((s.job_detail || {}).cancelled || []).slice(0, 8);
    const rows = allowed.includes("restart_job") ? jobs.map(j => `<tr><td class="mono">${esc(j.job)}</td>
      <td>${esc(j.ended_at || "")}</td><td>${esc(j.scheduled_by || "")}</td><td style="text-align:right">
      <button class="btn ops-btn" type="button" onclick='OPS.action(${sysArg},"restart_job",${esc(JSON.stringify({job: j.job, job_count: j.job_count}))},"Restart cancelled job")'>Restart</button></td></tr>`).join("") : "";
    const btns = [];
    if (allowed.includes("restart_job")) btns.push(`<button class="btn ops-btn" type="button" onclick='OPS.pick(${sysArg},"restart_job","Restart cancelled job")'>Restart a cancelled job…</button>`);
    if (allowed.includes("release_lock")) btns.push(`<button class="btn ops-btn" type="button" onclick='OPS.pick(${sysArg},"release_lock","Release old lock")'>Release an old lock…</button>`);
    if (allowed.includes("cancel_job")) btns.push(`<button class="btn ops-btn" type="button" onclick='OPS.pick(${sysArg},"cancel_job","Cancel running job")'>Cancel a running job…</button>`);
    if (allowed.includes("unlock_user")) btns.push(`<button class="btn ops-btn" type="button" onclick='OPS.pick(${sysArg},"unlock_user","Unlock user")'>Unlock a user…</button>`);
    if (allowed.includes("restart_queue")) btns.push(`<button class="btn ops-btn" type="button" onclick='OPS.pick(${sysArg},"restart_queue","Restart inbound queue")'>Restart a queue…</button>`);
    return `<div class="checks"><div class="checks-head"><span>One-click actions</span><span>logged · needs confirmation</span></div>
      ${rows ? `<table><tr><th>Cancelled job</th><th>Ended</th><th>Scheduled by</th><th></th></tr>${rows}</table>` : ""}
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${btns.join("")}</div></div>`;
  },

  /* ---- wall card strip ---------------------------------------------- */

  menu(btn) {
    const box = btn.closest(".ops-menu");
    document.querySelectorAll(".ops-menu.open").forEach(m => { if (m !== box) m.classList.remove("open"); });
    box.classList.toggle("open");
  },

  strip(system, st, onDone) {
    if (!st) return "";
    const esc = OPS.esc, tags = [];
    const sysArg = esc(JSON.stringify(system));
    const kind = st.alert_kind || "*";
    const kindArg = esc(JSON.stringify(kind));
    if (st.standby) tags.push(st.standby.active
      ? `<span class="ops-tag warn">FAILOVER: monitored here, ${esc(st.standby.peer)} is silent</span>`
      : `<span class="ops-tag">STANDBY — monitored by ${esc(st.standby.peer)}</span>`);
    if (st.maintenance) tags.push(`<span class="ops-tag" title="${esc(st.maintenance.reason)}">MAINTENANCE until
      ${esc(String(st.maintenance.ends_at).slice(11, 16))} · ${esc(st.maintenance.reason)}</span>`);
    else if (st.next_maintenance) tags.push(`<span class="ops-tag info">maintenance ${esc(String(st.next_maintenance.starts_at).slice(5, 16))}</span>`);
    for (const s of st.silences || []) {
      const one = String(s.kind).startsWith("metric:");
      const what = s.kind === "*" ? "all" : one ? (s.label || s.kind.slice(7)) : s.kind.replace(/_/g, " ");
      const txt = s.mode === "snooze" ? `snoozed (${what}) until ${String(s.until).slice(11, 16)}`
               : s.mode === "ack" ? `ack'd by ${s.created_by} (${what})`
               : one ? `ignored until back to normal (${what})` : `ignored until fixed (${what})`;
      tags.push(`<span class="ops-tag ${s.mode === "ack" ? "info" : "warn"}">${esc(txt)}
        <a href="#" onclick="OPS.clearSilence(${s.id}).then(${onDone});return false" title="Clear">✕</a></span>`);
    }
    if (st.parked) tags.push(`<span class="ops-tag warn" title="${esc(st.parked.reason)}">SWEEPS PARKED
      <a href="#" onclick="OPS.unpark(${sysArg}).then(${onDone});return false" title="Clear the flag">resume</a></span>`);
    const safeUrl = u => /^https?:\/\//i.test(u || "");
    for (const t of st.tickets || []) tags.push(`<span class="ops-tag info">ticket ${safeUrl(t.url)
      ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.external_id || "#" + t.id)}</a>`
      : esc(t.external_id || "#" + t.id)}${t.status === "opening" ? " (opening)" : ""}</span>`);
    for (const u of (st.unusual || []).slice(0, 3)) tags.push(`<span class="ops-tag warn" title="${esc(u.note)}">unusual: ${esc(u.metric)}</span>`);
    const menu = `<span class="ops-menu" style="margin-left:auto"><button class="btn ops-btn" type="button"
        onclick="OPS.menu(this)">Alert ▾</button><div class="m">
        <button type="button" onclick="OPS.silence(${sysArg},${kindArg},'snooze',60).then(${onDone})">Snooze 1 h</button>
        <button type="button" onclick="OPS.silence(${sysArg},${kindArg},'snooze',240).then(${onDone})">Snooze 4 h</button>
        <button type="button" onclick="OPS.silence(${sysArg},${kindArg},'ack').then(${onDone})">Acknowledge (I'm on it)</button>
        <button type="button" onclick="OPS.silence(${sysArg},${kindArg},'until_fixed').then(${onDone})">Ignore until fixed</button>
        <hr>
        <button type="button" onclick="OPS.maintenance(${sysArg}).then(${onDone})">Maintenance window…</button>
        ${st.parked ? `<button type="button" onclick="OPS.unpark(${sysArg}).then(${onDone})">Resume sweeps</button>`
                    : `<button type="button" onclick="OPS.park(${sysArg}).then(${onDone})">Park sweeps…</button>`}
        <hr>
        <button type="button" onclick="location.href='/timeline?system='+encodeURIComponent(${sysArg})">Timeline</button>
        <button type="button" onclick="location.href='/runbook?system='+encodeURIComponent(${sysArg})+'&kind='+encodeURIComponent(${kindArg})">Runbook${st.runbook ? ": " + esc(st.runbook.title).slice(0, 28) : ""}</button>
      </div></span>`;
    return `<div class="ops-strip">${tags.join("")}${menu}</div>`;
  },
};

document.addEventListener("click", e => {
  if (!e.target.closest(".ops-menu")) document.querySelectorAll(".ops-menu.open").forEach(m => m.classList.remove("open"));
});
