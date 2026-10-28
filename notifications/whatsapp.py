"""
Send selected T-code screenshots to WhatsApp.

WHAT THIS CAN AND CANNOT DO
The official WhatsApp Business Cloud API sends to individual phone numbers.
It has no group-posting endpoint, and neither does Twilio. Posting into a
group is only possible through unofficial libraries that drive WhatsApp Web,
which breach WhatsApp's terms and get numbers banned -- not a foundation for
production monitoring. So this sends to a recipient list, and a generic
webhook mode is provided for organisations that route through their own
gateway.

BEFORE ENABLING THIS, READ THE NEXT PARAGRAPH
These screenshots are not neutral. SM12 shows lock owners by username. SM66
and SM51 show what every logged-on user is running. SM37 shows job names.
SMLG shows instance topology. Sending them to WhatsApp moves production
operational detail onto personal phones, through Meta's servers, into a group
whose membership changes without anyone telling this system. Once a message
is delivered it cannot be recalled, and a person removed from the group keeps
every image already on their device.

That is a decision for whoever owns the data, not a technical setting. It is
worth being deliberate about it, because the same project already has an open
finding about reports/ being browsable.

Configuration in .env:
    WHATSAPP_ENABLED=true
    WHATSAPP_MODE=cloud_api          # or: webhook
    WHATSAPP_PHONE_NUMBER_ID=...     # cloud_api: from Meta
    WHATSAPP_ACCESS_TOKEN=...        # cloud_api: from Meta
    WHATSAPP_RECIPIENTS=9198...,9199...
    WHATSAPP_WEBHOOK_URL=...         # webhook mode only
    WHATSAPP_MAX_IMAGES=8
    WHATSAPP_TEMPLATE_NAME=ibops_alert   # cloud_api: REQUIRED in practice, see below
    WHATSAPP_TEMPLATE_LANG=en

WHY A TEMPLATE
Meta only delivers a free-form message (type "image", "text") inside the
24-hour window that opens when the RECIPIENT last messaged the business
number. A monitoring alert almost never lands in that window, so without a
template the API answers 131047 "re-engagement message" and nothing arrives.
Business-initiated messages must be pre-approved templates, and Meta bills
them per 24-hour conversation (see Meta's WhatsApp pricing) -- this is not
a free channel.

Create the template once in Meta Business Manager -> WhatsApp -> Message
templates, category Utility, with an IMAGE header and this body:

    {{1}} · {{2}}
    {{3}}

The three body parameters are: system + time, T-code label, summary. Once
approved, set WHATSAPP_TEMPLATE_NAME and every alert goes out as that
template with the screenshot as its header image.
"""

from __future__ import annotations

import mimetypes
import os
import time

import requests
from dotenv import load_dotenv

from utils.logger import get_logger

load_dotenv()

log = get_logger(__name__, "whatsapp")

GRAPH_VERSION = "v21.0"


def _enabled() -> bool:
    return os.getenv("WHATSAPP_ENABLED", "false").strip().lower() == "true"


def _recipients() -> list[str]:
    raw = os.getenv("WHATSAPP_RECIPIENTS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def selected_tcodes() -> list[str]:
    """
    Which T-codes get sent. Read from config/monitoring_tasks.yaml, where a
    task carrying `whatsapp: true` is included.

    Deliberately a per-task flag rather than a separate list: a second list
    would drift out of step with the tasks, and a T-code that stopped being
    captured would go on being "sent" as a silently missing image.
    """
    try:
        from core.config_loader import CONFIG_DIR
        import yaml

        path = os.path.join(CONFIG_DIR, "monitoring_tasks.yaml")
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return [str(t.get("tcode", "")).strip().upper()
                for t in data.get("tasks", [])
                if t.get("whatsapp") and t.get("tcode")]
    except Exception as exc:
        log.warning(f"WhatsApp: could not read T-code selection: {exc}")
        return []


def select_screenshots(evidence: list[dict]) -> list[dict]:
    """
    Filter captured evidence down to the selected T-codes.

    Order follows the configuration, not the capture order, so the images
    arrive in the same sequence every time -- a reader scrolling a phone
    should not have to hunt for which screen is which.
    """
    wanted = selected_tcodes()
    if not wanted:
        return []

    by_tcode: dict[str, dict] = {}
    for item in evidence or []:
        tcode = str(item.get("tcode") or "").strip().upper()
        path = item.get("screenshot") or item.get("path")
        if tcode and path and os.path.isfile(path) and tcode not in by_tcode:
            by_tcode[tcode] = {"tcode": tcode, "path": path,
                               "label": item.get("label") or tcode}

    return [by_tcode[t] for t in wanted if t in by_tcode]


def _upload_media(path: str, phone_number_id: str, token: str) -> str | None:
    """Upload one image to Meta and return its media id."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/media"
    mime = mimetypes.guess_type(path)[0] or "image/png"
    try:
        with open(path, "rb") as handle:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (os.path.basename(path), handle, mime)},
                data={"messaging_product": "whatsapp", "type": mime},
                timeout=60,
            )
        if response.status_code >= 400:
            log.warning(f"WhatsApp upload failed for {os.path.basename(path)}: "
                        f"HTTP {response.status_code} {response.text[:200]}")
            return None
        return response.json().get("id")
    except requests.RequestException as exc:
        log.warning(f"WhatsApp upload error for {os.path.basename(path)}: {exc}")
        return None


def _message_payload(media_id: str, caption: str, recipient: str,
                     template: str, lang: str, params: list[str]) -> dict:
    """Template message when one is configured, free-form image otherwise."""
    if template:
        return {
            "messaging_product": "whatsapp", "to": recipient, "type": "template",
            "template": {
                "name": template,
                "language": {"code": lang or "en"},
                "components": [
                    {"type": "header",
                     "parameters": [{"type": "image", "image": {"id": media_id}}]},
                    {"type": "body",
                     "parameters": [{"type": "text", "text": p[:1024]} for p in params]},
                ],
            },
        }
    return {"messaging_product": "whatsapp", "to": recipient, "type": "image",
            "image": {"id": media_id, "caption": caption[:1024]}}


def _send_image(media_id: str, caption: str, recipient: str,
                phone_number_id: str, token: str,
                template: str = "", lang: str = "en",
                params: list[str] | None = None) -> bool:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=_message_payload(media_id, caption, recipient, template, lang,
                                  params or []),
            timeout=60,
        )
        if response.status_code >= 400:
            text = response.text[:200]
            if "131047" in text or "re-engagement" in text.lower():
                text += (" -- outside the 24-hour window; set "
                         "WHATSAPP_TEMPLATE_NAME to an approved template")
            log.warning(f"WhatsApp send to {recipient} failed: "
                        f"HTTP {response.status_code} {text}")
            return False
        return True
    except requests.RequestException as exc:
        log.warning(f"WhatsApp send error to {recipient}: {exc}")
        return False


def _send_webhook(images: list[dict], system: str, summary: str) -> bool:
    """
    Post to a gateway of the organisation's own choosing.

    Exists because the official API cannot post to groups. Whatever bridge is
    used, the delivery decision stays outside this project rather than being
    hidden in it.
    """
    url = os.getenv("WHATSAPP_WEBHOOK_URL", "").strip()
    if not url:
        log.warning("WhatsApp: webhook mode selected but WHATSAPP_WEBHOOK_URL is unset")
        return False

    files = []
    try:
        handles = []
        for image in images:
            handle = open(image["path"], "rb")
            handles.append(handle)
            files.append(("images", (f"{image['tcode']}.png", handle, "image/png")))

        response = requests.post(
            url,
            data={"system": system, "summary": summary,
                  "tcodes": ",".join(i["tcode"] for i in images)},
            files=files, timeout=120,
        )
        for handle in handles:
            handle.close()

        if response.status_code >= 400:
            log.warning(f"WhatsApp webhook failed: HTTP {response.status_code} "
                        f"{response.text[:200]}")
            return False
        return True
    except (OSError, requests.RequestException) as exc:
        log.warning(f"WhatsApp webhook error: {exc}")
        return False


def send_screenshots(system: str, evidence: list[dict],
                     summary: str = "") -> dict:
    """
    Send the selected screenshots. Returns a result dict; never raises.

    Failure here must not fail a monitoring sweep -- the metrics, the snapshot
    and the report are the product, and a messaging problem is not a reason
    to lose them.
    """
    if not _enabled():
        return {"sent": False, "reason": "disabled"}

    images = select_screenshots(evidence)
    if not images:
        return {"sent": False, "reason": "no screenshots matched the selected T-codes",
                "selected": selected_tcodes()}

    limit = int(os.getenv("WHATSAPP_MAX_IMAGES", "8"))
    if len(images) > limit:
        # A phone notification per image: more than a handful and people mute
        # the group, which is the same as not sending at all.
        log.info(f"WhatsApp: {len(images)} images, sending the first {limit}")
        images = images[:limit]

    stamp = time.strftime("%Y-%m-%d %H:%M")
    header = f"{system} · {stamp}" + (f" · {summary}" if summary else "")

    mode = os.getenv("WHATSAPP_MODE", "cloud_api").strip().lower()

    if mode == "webhook":
        ok = _send_webhook(images, system, header)
        return {"sent": ok, "mode": "webhook", "images": len(images),
                "tcodes": [i["tcode"] for i in images]}

    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    recipients = _recipients()

    if not (phone_number_id and token and recipients):
        return {"sent": False,
                "reason": "WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN and "
                          "WHATSAPP_RECIPIENTS must all be set"}

    template = os.getenv("WHATSAPP_TEMPLATE_NAME", "").strip()
    lang = os.getenv("WHATSAPP_TEMPLATE_LANG", "en").strip() or "en"
    if not template:
        log.warning("WhatsApp: no WHATSAPP_TEMPLATE_NAME set. Free-form images are "
                    "only delivered to people who messaged this number in the last "
                    "24 hours; most alerts will be rejected (error 131047).")

    delivered, failed = 0, 0
    for index, image in enumerate(images, start=1):
        media_id = _upload_media(image["path"], phone_number_id, token)
        if not media_id:
            failed += 1
            continue
        caption = f"{header}\n{index}/{len(images)} · {image['tcode']}"
        params = [f"{system} · {stamp}", f"{index}/{len(images)} · {image['label']}",
                  summary or "see attached screenshot"]
        for recipient in recipients:
            if _send_image(media_id, caption, recipient, phone_number_id, token,
                           template, lang, params):
                delivered += 1
            else:
                failed += 1

    log.info(f"WhatsApp: {delivered} delivered, {failed} failed "
             f"({len(images)} images x {len(recipients)} recipients)")

    return {"sent": delivered > 0, "mode": "cloud_api",
            "images": len(images), "delivered": delivered, "failed": failed,
            "tcodes": [i["tcode"] for i in images]}


# ---------------------------------------------------------------------------
# Alerts to WhatsApp (text template with an Acknowledge link)
# ---------------------------------------------------------------------------
#
# Every alert kind listed in IBO_WHATSAPP_ALERT_KINDS (default: system_down,
# vpn_down, credentials, critical, monitor_down, peer_silent, failover,
# recovered) also goes to WhatsApp -- and goes even when the mail failed, so
# a broken SMTP server does not silence a SYSTEM DOWN.
#
# cloud_api mode needs an approved UTILITY template with a text body of
# three parameters (WHATSAPP_ALERT_TEMPLATE), for example:
#     IBOPS {{1}}
#     {{2}}
#     Acknowledge: {{3}}
# webhook mode POSTs JSON {"title", "text", "ack_url", "system", "kind"}.
# Recipients: WHATSAPP_ALERT_RECIPIENTS, else whoever is on call (phone in
# config/oncall.yaml), else WHATSAPP_RECIPIENTS.

def _alert_kinds() -> set[str]:
    raw = os.getenv("IBO_WHATSAPP_ALERT_KINDS",
                    "system_down,vpn_down,credentials,critical,monitor_down,peer_silent,failover,recovered")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _param(text: str, limit: int) -> str:
    # Meta rejects template parameters with new lines, tabs or 4+ spaces.
    text = " · ".join(part.strip() for part in str(text or "").splitlines() if part.strip())
    text = " ".join(text.replace("\t", " ").split())
    return text[:limit] or "-"


def _alert_recipients() -> list[str]:
    raw = os.getenv("WHATSAPP_ALERT_RECIPIENTS", "")
    nums = [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]
    if not nums:
        try:
            from core import oncall
            nums = oncall.phones_for(oncall.current().get("emails") or [])
        except Exception:
            nums = []
    if not nums:
        nums = _recipients()
    out = []
    from notifications.sms import normalise
    for n in nums:
        digits = normalise(n)
        if digits and digits not in out:
            out.append(digits)
    return out


def wants_alert(kind: str) -> bool:
    if not _enabled():
        return False
    kinds = _alert_kinds()
    return kind in kinds or any(k.endswith("*") and kind.startswith(k[:-1]) for k in kinds)


def send_alert(system: str, kind: str, title: str, text: str = "") -> dict:
    """One alert to WhatsApp. Never raises."""
    if not wants_alert(kind):
        return {"sent": False, "reason": "kind not selected or WhatsApp disabled"}
    ack = ""
    try:
        from core.action_links import make, public_url
        if public_url() and kind not in ("recovered", "failover") and system:
            ack = f"{public_url()}/access/action?t={make('ack', system, kind)}"
    except Exception:
        ack = ""
    mode = os.getenv("WHATSAPP_MODE", "cloud_api").strip().lower()
    if mode == "webhook":
        url = os.getenv("WHATSAPP_WEBHOOK_URL", "").strip()
        if not url:
            return {"sent": False, "reason": "WHATSAPP_WEBHOOK_URL is unset"}
        try:
            r = requests.post(url, json={"title": title, "text": text, "ack_url": ack, "system": system,
                                         "kind": kind}, timeout=30)
            return {"sent": r.status_code < 300, "status": r.status_code}
        except requests.RequestException as exc:
            log.warning(f"WhatsApp alert webhook error: {exc}")
            return {"sent": False, "reason": str(exc)}
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    template = os.getenv("WHATSAPP_ALERT_TEMPLATE", "").strip()
    lang = os.getenv("WHATSAPP_TEMPLATE_LANG", "en").strip() or "en"
    to = _alert_recipients()
    if not (phone_number_id and token and template and to):
        return {"sent": False, "reason": "WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_ALERT_TEMPLATE "
                                         "and a recipient are all needed"}
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"
    params = [_param(title, 200), _param(text, 700), _param(ack or "open the dashboard", 400)]
    ok = 0
    for n in to[:10]:
        body = {"messaging_product": "whatsapp", "to": n, "type": "template",
                "template": {"name": template, "language": {"code": lang},
                             "components": [{"type": "body",
                                             "parameters": [{"type": "text", "text": p} for p in params]}]}}
        try:
            r = requests.post(url, json=body, timeout=30,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            if r.status_code < 300:
                ok += 1
            else:
                log.warning(f"WhatsApp alert to ...{n[-4:]} failed: HTTP {r.status_code} {r.text[:200]}")
        except requests.RequestException as exc:
            log.warning(f"WhatsApp alert to ...{n[-4:]} error: {exc}")
    return {"sent": ok > 0, "delivered": ok, "recipients": len(to)}


# ---------------------------------------------------------------------------
# Value alerts (WARNING / CRITICAL / back to normal) per system
# ---------------------------------------------------------------------------
#
# Sent by core/alert_engine.py when "WhatsApp" is ticked for a value on the
# Thresholds page, to that system's WhatsApp numbers (same page, "Who gets
# the alerts"). Same moments as the mail: first breach, change of level,
# repeat (CRITICAL every 60 min, WARNING every 8 h), back to normal; a value
# snoozed or acknowledged from the mail is held here too.
#
# It uses the same approved template as the other alerts
# (WHATSAPP_ALERT_TEMPLATE, three text parameters):
#     {{1}}  "CRITICAL · CEAT S4P"
#     {{2}}  "HANA memory used = 91% (warning 80, critical 90) · 14.10 11:40 · <details>"
#     {{3}}  link to the system on the dashboard (IBO_PUBLIC_URL), else "open the InfraBeatOps dashboard"
#
# WHATSAPP_MODE:
#     cloud_api  Meta WhatsApp Cloud API (WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN)
#     webhook    POST JSON to WHATSAPP_WEBHOOK_URL (a provider or gateway of your own)
#     test       nothing is sent: the message is written to logs/whatsapp.log, to try
#                the whole flow before the WhatsApp account exists

_META_ERRORS = {
    "131047": "outside the 24-hour window: an approved template is needed (WHATSAPP_ALERT_TEMPLATE)",
    "132001": "template not found or not approved yet for this language (WHATSAPP_ALERT_TEMPLATE / "
              "WHATSAPP_TEMPLATE_LANG)",
    "132000": "the template expects a different number of parameters (it must have three)",
    "131030": "this number is not in the test number's allowed list (Meta: add it, or use a live number)",
    "131026": "the number cannot receive WhatsApp messages",
    "190": "the access token has expired or is wrong (WHATSAPP_ACCESS_TOKEN)",
    "100": "invalid request: check WHATSAPP_PHONE_NUMBER_ID",
    "131056": "too many messages to this number in a short time (Meta rate limit)",
}


def _mode() -> str:
    return os.getenv("WHATSAPP_MODE", "cloud_api").strip().lower() or "cloud_api"


def value_alert_state() -> tuple[bool, str]:
    """(ready, why not) for value alerts. Never shows a secret."""
    if not _enabled():
        return False, "switched off (WHATSAPP_ENABLED=true in .env to switch on)"
    mode = _mode()
    if mode == "test":
        return True, "test mode: messages are written to logs/whatsapp.log, nothing is sent"
    if mode == "webhook":
        return (True, "") if os.getenv("WHATSAPP_WEBHOOK_URL", "").strip() else \
            (False, "WHATSAPP_WEBHOOK_URL is not set in .env")
    if mode != "cloud_api":
        return False, f"unknown WHATSAPP_MODE '{mode}' (cloud_api, webhook or test)"
    missing = [k for k in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_ALERT_TEMPLATE")
               if not os.getenv(k, "").strip()]
    if missing:
        return False, "not set in .env: " + ", ".join(missing)
    return True, ""


def value_alert_texts(job: dict) -> list[str]:
    """The three template parameters for one value alert."""
    from datetime import datetime as _dt
    try:
        from core.alert_engine import where_text, shown_label
        where = where_text(job)
        job = {**job, "label": shown_label(job)}
    except Exception:  # noqa: BLE001
        where = f" ({job['instance']})" if job.get("instance") else ""
    kind = job.get("kind")
    head = "RECOVERED" if kind == "recovery" else "TEST" if kind == "test" else \
        f"{job.get('level')}{' (still)' if kind == 'repeat' else ''}"
    title = f"{head} · {job.get('system')}"
    if kind == "recovery":
        line = f"{job.get('label')}{where} back to normal: {job.get('value')}"
    elif kind == "test":
        line = "Test message from InfraBeatOps: WhatsApp alerts for this system reach this number"
    else:
        line = f"{job.get('label')}{where} = {job.get('value')}"
        if job.get("limits"):
            line += f" ({job['limits']})"
    parts = [line, _dt.now().strftime("%d.%m %H:%M")]
    if job.get("detail") and kind != "test":
        parts.append(str(job["detail"]))
    for k, v in (job.get("breakdown") or [])[:3]:
        parts.append(f"{k}: {v}")
    link = "open the InfraBeatOps dashboard"
    try:
        from core.action_links import public_url
        from urllib.parse import quote
        if public_url():
            link = f"{public_url()}/system?name={quote(str(job.get('system') or ''))}"
    except Exception:  # noqa: BLE001
        pass
    return [_param(title, 200), _param(" · ".join(parts), 700), _param(link, 300)]


def _meta_error(resp) -> str:
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    code = str(err.get("code") or "")
    sub = str((err.get("error_data") or {}).get("details") or err.get("message") or "")[:160]
    return _META_ERRORS.get(code) or f"Meta answered HTTP {resp.status_code}" + (f" ({code}: {sub})" if code else "")


def send_value_alert(job: dict, numbers: list[str]) -> tuple[bool, str, str]:
    """
    One value alert to these numbers. (sent to at least one, who, note). Never raises.
    `note` says what went wrong for the numbers it did not reach.
    """
    from notifications.sms import normalise
    to = []
    for n in numbers or []:
        d = normalise(n)
        if d and d not in to:
            to.append(d)
    who = ", ".join("+" + d for d in to)
    if not to:
        return False, "", "no WhatsApp number for this system (Thresholds page: Who gets the alerts)"
    ready, why = value_alert_state()
    if not ready:
        return False, who, why
    texts = value_alert_texts(job)
    mode = _mode()
    if mode == "test":
        for d in to:
            log.info(f"[TEST MODE, not sent] WhatsApp to +{d}: {texts[0]} | {texts[1]} | {texts[2]}")
        return False, who, "test mode: message written to logs/whatsapp.log, nothing sent"
    if mode == "webhook":
        try:
            r = requests.post(os.getenv("WHATSAPP_WEBHOOK_URL", "").strip(), timeout=30,
                              json={"to": to, "title": texts[0], "text": texts[1], "link": texts[2],
                                    "system": job.get("system"), "metric": job.get("metric"),
                                    "level": job.get("level"), "kind": job.get("kind")})
            return (r.status_code < 300), who, "" if r.status_code < 300 else f"webhook answered HTTP {r.status_code}"
        except requests.RequestException as exc:
            return False, who, f"webhook not reached: {type(exc).__name__}"
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    template = os.getenv("WHATSAPP_ALERT_TEMPLATE", "").strip()
    lang = os.getenv("WHATSAPP_TEMPLATE_LANG", "en").strip() or "en"
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"
    ok, problems = [], []
    for d in to[:10]:
        body = {"messaging_product": "whatsapp", "to": d, "type": "template",
                "template": {"name": template, "language": {"code": lang},
                             "components": [{"type": "body",
                                             "parameters": [{"type": "text", "text": p} for p in texts]}]}}
        try:
            r = requests.post(url, json=body, timeout=30,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            if r.status_code < 300:
                ok.append(d)
            else:
                why = _meta_error(r)
                problems.append(f"+{d}: {why}")
                log.warning(f"WhatsApp value alert to ...{d[-4:]} failed: {why}")
        except requests.RequestException as exc:
            problems.append(f"+{d}: {type(exc).__name__}")
            log.warning(f"WhatsApp value alert to ...{d[-4:]} error: {type(exc).__name__}")
    if len(to) > 10:
        problems.append(f"only the first 10 of {len(to)} numbers are used")
    return bool(ok), who, "; ".join(problems)
