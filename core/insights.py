"""
Noise analytics and availability, from data already stored.

noise(days)          per system and alert kind: mails sent, repeats held,
                     held by snooze / acknowledge / maintenance, how often
                     people snoozed or ignored it, and how long it took
                     someone to acknowledge -- the list of thresholds and
                     schedules to retune.
availability(days)   per system: minutes not reachable (from the timeline's
                     state changes), number of outages, availability %.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core import ops_db


def _since(days: float) -> str:
    return ops_db.fmt(datetime.now() - timedelta(days=days))


def noise(days: float = 30, systems: list[str] | None = None) -> list[dict]:
    since = _since(days)
    stats: dict[tuple, dict] = {}

    def bucket(system, kind):
        return stats.setdefault((system, kind), {"system": system, "kind": kind, "sent": 0, "repeats_held": 0,
                                                 "held_by_people": 0, "held_maintenance": 0, "snoozed": 0,
                                                 "acknowledged": 0, "ignored": 0, "ack_minutes": []})
    for r in ops_db.rows("SELECT system, kind, suppressed, reason, sent_at FROM alerts WHERE sent_at>=?", (since,)):
        if r["kind"].startswith(("escalation_", "monitor_", "peer_", "recovered", "exception")):
            continue
        b = bucket(r["system"], r["kind"])
        reason = str(r["reason"] or "")
        if not r["suppressed"]:
            b["sent"] += 1
        elif reason.startswith(("same ", "daily cap")):
            b["repeats_held"] += 1
        elif reason.startswith("in maintenance"):
            b["held_maintenance"] += 1
        elif reason.startswith(("snoozed", "acknowledged", "ignored")):
            b["held_by_people"] += 1
    for s in ops_db.rows("SELECT system, kind, mode, created_at FROM silences WHERE created_at>=?", (since,)):
        kinds = [k for (sy, k) in stats if sy == s["system"]] if s["kind"] == "*" else [s["kind"]]
        for k in kinds or [s["kind"]]:
            b = bucket(s["system"], k)
            b[{"snooze": "snoozed", "ack": "acknowledged", "until_fixed": "ignored"}.get(s["mode"], "snoozed")] += 1
            first = ops_db.one("SELECT sent_at FROM alerts WHERE system=? AND kind=? AND suppressed=0 AND sent_at<=? "
                               "AND sent_at>=? ORDER BY sent_at DESC LIMIT 1",
                               (s["system"], k, s["created_at"], ops_db.fmt(ops_db.parse(s["created_at"])
                                                                            - timedelta(hours=24))))
            if first and s["mode"] in ("ack", "until_fixed"):
                b["ack_minutes"].append((ops_db.parse(s["created_at"]) - ops_db.parse(first["sent_at"]))
                                        .total_seconds() / 60)
    out = []
    for b in stats.values():
        if systems is not None and b["system"] not in systems:
            continue
        am = sorted(b.pop("ack_minutes"))
        b["median_ack_minutes"] = round(am[len(am) // 2], 1) if am else None
        silenced = b["snoozed"] + b["ignored"]
        tip = ""
        if b["sent"] >= 10 and silenced >= max(3, b["sent"] // 4):
            tip = "Often snoozed or ignored: the threshold or schedule probably needs changing."
        elif b["sent"] >= 20 and not (b["acknowledged"] or silenced):
            tip = "Many mails and nobody reacts: check who receives it, or whether it matters."
        elif b["median_ack_minutes"] and b["median_ack_minutes"] > 60:
            tip = "Slow to be acknowledged: check routing / on-call."
        b["tip"] = tip
        b["score"] = b["sent"] + b["repeats_held"] // 4 + 3 * silenced
        out.append(b)
    out.sort(key=lambda x: -x["score"])
    return out


def availability(days: float = 7, systems: list[str] | None = None) -> list[dict]:
    """Downtime from the timeline: a [SYSTEM DOWN]/[VPN ...] state line opens an outage, 'Reachable again' closes it."""
    start = datetime.now() - timedelta(days=days)
    from core.config_loader import get_all_systems
    names = systems if systems is not None else [c.get("name") for c in get_all_systems()]
    out = []
    for name in names:
        rows = ops_db.rows("SELECT at, category, title FROM timeline WHERE system=? AND category IN ('state','vpn') "
                           "AND at>=? ORDER BY at, id", (name, ops_db.fmt(start - timedelta(days=30))))
        down_since = None
        outages, down_s, unreach_s = 0, 0.0, 0.0
        kind = None
        for r in rows:
            t = ops_db.parse(r["at"])
            title = r["title"]
            if title.startswith("[") and title != "[OK]":
                if down_since is None:
                    down_since, kind = t, title
                    if t >= start:
                        outages += 1
                else:
                    kind = title if "SYSTEM DOWN" in title else kind
            elif title.startswith("Reachable again") and down_since is not None:
                a = max(down_since, start)
                if t > a:
                    secs = (t - a).total_seconds()
                    unreach_s += secs
                    if kind and "SYSTEM DOWN" in kind:
                        down_s += secs
                down_since = None
        if down_since is not None:
            a = max(down_since, start)
            secs = (datetime.now() - a).total_seconds()
            unreach_s += secs
            if kind and "SYSTEM DOWN" in kind:
                down_s += secs
        period = days * 86400
        out.append({"system": name, "outages": outages, "down_minutes": round(down_s / 60),
                    "unreachable_minutes": round(unreach_s / 60),
                    "availability_pct": round(100 * (1 - down_s / period), 2),
                    "open": down_since is not None})
    return out
