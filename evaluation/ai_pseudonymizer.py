"""
Keep hostnames and IP addresses out of the external model.

evaluation/ai_privacy.py already replaces SAP usernames and e-mail
addresses with USER_nn before a prompt leaves the machine. It leaves
infrastructure identifiers in: application-server hostnames
(sapxps4ci.sap.<client>.in), message-server names, IP addresses. Those
name the client and map its network, and on free-tier API keys the
provider may keep what it receives.

This is a second, outer layer with the same mask-then-restore design:

    prompt  "... on HOST_01 (IP_01) ..."   -> model
    reply   "... check HOST_01 ..."         -> "... check sapxps4ci... ..."

Recognised:
  - every host configured in systems.yaml (RFC ashost / mshost, SSH,
    SMTP host), matched exactly;
  - IPv4 addresses;
  - dotted hostnames ending in a common domain suffix. Metric names such as
    sap.icm.state and version numbers such as 7.50.2 are not touched, and
    neither is the domain part of an e-mail address (ai_privacy owns those).

Token prefixes are HOST_ and IP_, distinct from ai_privacy's USER_, so the
two layers never collide. Order: this layer masks first and restores last.

Switch off with IBO_AI_MASK_HOSTS=0.
"""
from __future__ import annotations

import os
import re

from utils.logger import get_logger

log = get_logger(__name__)

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_TLDS = ("in", "com", "net", "org", "local", "lan", "corp", "internal", "intra", "intranet",
         "io", "co", "biz", "info", "cloud", "ae", "sg", "au", "uk", "us", "de", "eu", "nl",
         "sa", "qa", "om", "bh", "kw", "lk", "my", "id", "jp", "cn", "hk", "edu", "gov", "priv")
# Not preceded by "@" (e-mail domain) or another name character.
_FQDN = re.compile(r"(?<![@\w.-])(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\.(?:"
                   + "|".join(_TLDS) + r")(?![.\w-])")


def enabled() -> bool:
    return str(os.getenv("IBO_AI_MASK_HOSTS", "1")).strip().lower() not in ("0", "false", "no", "off")


def _configured_hosts() -> list[str]:
    try:
        from core.config_loader import get_all_systems
        systems = get_all_systems()
    except Exception:
        try:
            from core.config_loader import get_systems
            systems = get_systems()
        except Exception:
            return []
    hosts = set()
    for s in systems or []:
        rfc = s.get("rfc") or {}
        for h in (s.get("ssh_host"), s.get("smtp_host"), rfc.get("ashost"), rfc.get("mshost")):
            h = str(h or "").strip()
            if len(h) >= 3:
                hosts.add(h)
    return sorted(hosts, key=len, reverse=True)


class HostMasker:
    """One mapping for one model call."""

    def __init__(self) -> None:
        self._fwd: dict[str, str] = {}
        self._rev: dict[str, str] = {}
        self._n = {"HOST": 0, "IP": 0}

    def _token(self, kind: str, real: str) -> str:
        if real in self._fwd:
            return self._fwd[real]
        self._n[kind] += 1
        tok = f"{kind}_{self._n[kind]:02d}"
        self._fwd[real] = tok
        self._rev[tok] = real
        return tok

    def mask(self, text: str) -> str:
        text = str(text or "")
        for host in _configured_hosts():
            pat = re.compile(r"(?<![@\w.-])" + re.escape(host) + r"(?![\w-])", re.IGNORECASE)
            if pat.search(text):
                tok = self._token("IP" if _IPV4.fullmatch(host) else "HOST", host)
                text = pat.sub(tok, text)
        text = _IPV4.sub(lambda m: self._token("IP", m.group(0)), text)
        text = _FQDN.sub(lambda m: self._token("HOST", m.group(0)), text)
        return text

    def unmask(self, text: str) -> str:
        text = str(text or "")
        for tok in sorted(self._rev, key=len, reverse=True):
            text = re.sub(r"\b" + re.escape(tok) + r"\b", self._rev[tok], text)
        return text

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self._rev)


class HostMaskingProvider:
    """Wraps an AIProvider; the inner provider never sees a hostname or IP."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def generate(self, prompt: str) -> str:
        if not enabled():
            return self._inner.generate(prompt)
        m = HostMasker()
        masked = m.mask(prompt)
        if m.mapping:
            log.info(f"AI prompt: {len(m.mapping)} host/IP identifier(s) masked before the model call.")
        return m.unmask(self._inner.generate(masked))

    def __getattr__(self, item):
        return getattr(self._inner, item)
