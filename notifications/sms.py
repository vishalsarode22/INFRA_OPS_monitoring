"""
SMS and voice calls through MSG91 (India), for time-based escalation
(core/escalation_time.py).

SMS -- MSG91 Flow API (documented: https://api.msg91.com/apidoc/textsms/send-sms-flow.php)
    POST https://api.msg91.com/api/v5/flow/
    header authkey: <IBO_MSG91_AUTHKEY>
    {"flow_id": ..., "template_id": ..., "sender": ..., "recipients": [{"mobiles": "91...", "VAR1": ...}]}
    In India every SMS must match a DLT-registered template. Create a flow in
    the MSG91 panel whose text uses ##VAR1## ##VAR2## ##VAR3##, for example:
        IBOPS: ##VAR1## is ##VAR2## for ##VAR3## min, not acknowledged. Check mail.
    .env:
        IBO_MSG91_AUTHKEY=...
        IBO_MSG91_FLOW_ID=...          (the flow / template id)
        IBO_MSG91_SENDER=IBOPS         (6-letter DLT sender id)
        IBO_MSG91_SMS_VARS=VAR1=system,VAR2=state,VAR3=minutes   (optional)

VOICE -- MSG91 publishes its voice API only inside the panel, so the request
is configured, not guessed. Copy the "voice call with template / TTS" request
from the MSG91 panel into:
        IBO_MSG91_VOICE_URL=https://...
        IBO_MSG91_VOICE_BODY={"template": "...", "client_number": "{mobile}", "variables": {"system": "{system}"}}
Placeholders {mobile} {system} {state} {minutes} {message} are filled in;
the authkey header is added. Without IBO_MSG91_VOICE_URL the voice step is
skipped and the timeline says so.

Every send returns (ok, detail) and never raises.
"""
from __future__ import annotations

import json
import os
import re

from utils.logger import get_logger

log = get_logger(__name__)

FLOW_URL_DEFAULT = "https://api.msg91.com/api/v5/flow/"


def normalise(phone: str) -> str | None:
    """'+91 98xxx xxxxx' / '98xxxxxxxx' / '098xxxxxxxx' / '0091 98xx...' -> '9198xxxxxxxx'.
    None when not a phone number."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("00"):              # international prefix written as 00
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):   # Indian trunk prefix 0
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if 11 <= len(digits) <= 15:
        return digits
    return None


def sms_configured() -> bool:
    return bool(os.getenv("IBO_MSG91_AUTHKEY") and os.getenv("IBO_MSG91_FLOW_ID"))


def voice_configured() -> bool:
    return bool(os.getenv("IBO_MSG91_AUTHKEY") and os.getenv("IBO_MSG91_VOICE_URL"))


def _vars(fields: dict) -> dict:
    spec = os.getenv("IBO_MSG91_SMS_VARS", "VAR1=system,VAR2=state,VAR3=minutes")
    out = {}
    for part in spec.split(","):
        if "=" in part:
            var, field = (x.strip() for x in part.split("=", 1))
            if var:
                out[var] = str(fields.get(field, ""))[:30]
    return out


def send_sms(phones: list[str], fields: dict) -> tuple[bool, str]:
    if not sms_configured():
        return False, "MSG91 SMS not configured (IBO_MSG91_AUTHKEY, IBO_MSG91_FLOW_ID)"
    nums = [n for n in (normalise(p) for p in phones) if n]
    if not nums:
        return False, "no valid phone numbers"
    flow = os.getenv("IBO_MSG91_FLOW_ID", "").strip()
    body = {"flow_id": flow, "template_id": flow, "sender": os.getenv("IBO_MSG91_SENDER", "").strip(),
            "short_url": "0", "recipients": [dict({"mobiles": n}, **_vars(fields)) for n in nums]}
    try:
        import requests
        r = requests.post(os.getenv("IBO_MSG91_FLOW_URL", FLOW_URL_DEFAULT), json=body, timeout=20,
                          headers={"authkey": os.getenv("IBO_MSG91_AUTHKEY", ""), "content-type": "application/json",
                                   "accept": "application/json"})
        text = r.text[:300]
        ok = r.status_code < 300 and '"error"' not in text.lower()
        try:
            ok = ok and str(r.json().get("type", "success")).lower() == "success"
        except ValueError:
            pass
        if not ok:
            log.warning(f"MSG91 SMS failed: HTTP {r.status_code} {text}")
        return ok, f"HTTP {r.status_code} {text}"
    except Exception as exc:  # noqa: BLE001
        log.warning(f"MSG91 SMS error: {type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"


def _fill(obj, values: dict):
    if isinstance(obj, dict):
        return {k: _fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("{" + k + "}", str(v))
        return obj
    return obj


def call(phones: list[str], fields: dict) -> tuple[bool, str]:
    if not voice_configured():
        return False, "voice call not configured (IBO_MSG91_VOICE_URL, IBO_MSG91_VOICE_BODY)"
    try:
        template = json.loads(os.getenv("IBO_MSG91_VOICE_BODY") or "{}")
    except ValueError as exc:
        return False, f"IBO_MSG91_VOICE_BODY is not valid JSON: {exc}"
    nums = [n for n in (normalise(p) for p in phones) if n]
    if not nums:
        return False, "no valid phone numbers"
    import requests
    results = []
    ok_any = False
    for n in nums[:5]:
        body = _fill(template, dict(fields, mobile=n))
        try:
            r = requests.post(os.getenv("IBO_MSG91_VOICE_URL", ""), json=body, timeout=20,
                              headers={"authkey": os.getenv("IBO_MSG91_AUTHKEY", ""),
                                       "content-type": "application/json", "accept": "application/json"})
            good = r.status_code < 300
            ok_any = ok_any or good
            results.append(f"{n[-4:]}: HTTP {r.status_code}")
            if not good:
                log.warning(f"MSG91 voice call failed: HTTP {r.status_code} {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{n[-4:]}: {type(exc).__name__}")
    return ok_any, "; ".join(results)
