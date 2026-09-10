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


def _send_image(media_id: str, caption: str, recipient: str,
                phone_number_id: str, token: str) -> bool:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": recipient,
                  "type": "image",
                  "image": {"id": media_id, "caption": caption[:1024]}},
            timeout=60,
        )
        if response.status_code >= 400:
            log.warning(f"WhatsApp send to {recipient} failed: "
                        f"HTTP {response.status_code} {response.text[:200]}")
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

    delivered, failed = 0, 0
    for index, image in enumerate(images, start=1):
        media_id = _upload_media(image["path"], phone_number_id, token)
        if not media_id:
            failed += 1
            continue
        caption = f"{header}\n{index}/{len(images)} · {image['tcode']}"
        for recipient in recipients:
            if _send_image(media_id, caption, recipient, phone_number_id, token):
                delivered += 1
            else:
                failed += 1

    log.info(f"WhatsApp: {delivered} delivered, {failed} failed "
             f"({len(images)} images x {len(recipients)} recipients)")

    return {"sent": delivered > 0, "mode": "cloud_api",
            "images": len(images), "delivered": delivered, "failed": failed,
            "tcodes": [i["tcode"] for i in images]}
