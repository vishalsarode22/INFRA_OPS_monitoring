"""
Pseudonymise SAP usernames before a prompt leaves the machine, and put them
back in the answer.

WHY
Prompts to Gemini / Grok carry dump owners, lock holders, job owners and
"top users" straight from the client system. On free API tiers the provider
may keep and use what it receives, which is a problem under client contracts
and the DPDP Act. The provider never needs the real names: "USER_03 dumped
14 times in ZSD_REPORT" is exactly as useful to it as the real login.

HOW
1. Usernames are found in the prompt text itself, so nothing upstream has to
   change: any JSON field whose key is user-like ("user", "users", "owner",
   "username", "top_users", "by_user", "locked_users" ...) is harvested,
   whether the value is a string, a list of strings, or an object keyed by
   username. The ABAP row format "HH:MM:SS|UNAME" is handled too.
2. Every occurrence of each harvested name is replaced by USER_01, USER_02 ...
   (longest names first, whole-token match, case-insensitive) so a name that
   also appears in free text -- "lock held by BASIS2" -- is caught as well.
3. The provider's reply is mapped back, so reports, e-mails and the UI show
   real names exactly as before.

Names shorter than 3 characters are skipped; they are more likely to be
noise than logins, and replacing "AB" everywhere would mangle other text.
Emails (owner_email etc.) are pseudonymised as well; the local part before
the @ is usually a person's name.

Set AI_PSEUDONYMISE_USERS=false to switch this off.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, List, Tuple

_USER_KEY = re.compile(
    r'"(?:[a-z_]*_)?(?:user|users|username|user_names|owner|owners|uname|'
    r'step_user|program_user|top_users|by_user|unique_users|locked_users|'
    r'lock_users|user_logons|user_counts|top_users_by_total_ms|'
    r'top_users_by_memory|owner_email|owner_emails_resolved|owners_without_email)"'
    r'\s*:\s*', re.IGNORECASE)

# Values: a string, a JSON array, or a JSON object (usernames as keys).
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_ABAP_ROW = re.compile(r'^\d{2}:\d{2}:\d{2}\|(.+)$')

# Things that look like SAP users but are not people. Never rewrite these.
_SKIP = {
    "SAP*", "DDIC", "SAPSYS", "EARLYWATCH", "TMSADM", "SAPCPIC", "WF-BATCH",
    "N/A", "NONE", "NULL", "UNKNOWN", "SYSTEM", "TOTAL", "ALL", "USER",
    "YES", "NO", "TRUE", "FALSE",
}


def enabled() -> bool:
    return os.getenv("AI_PSEUDONYMISE_USERS", "true").strip().lower() not in (
        "0", "false", "no", "off")


def _candidates_from_value(value_text: str) -> Iterable[str]:
    """Yield username candidates from the JSON value that follows a user key."""
    value_text = value_text.lstrip()
    if not value_text:
        return
    if value_text[0] == '"':
        m = _STRING.match(value_text)
        if m:
            yield m.group(1)
        return
    if value_text[0] in "[{":
        # Take the matching bracket span, then every string inside it.
        depth, i = 0, 0
        while i < len(value_text):
            c = value_text[i]
            if c == '"':
                m = _STRING.match(value_text, i)
                i = m.end() if m else i + 1
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        span = value_text[:i + 1]
        if value_text[0] == "[":
            for m in _STRING.finditer(span):
                yield m.group(1)
        else:
            # Object: keys are the usernames ({"BASIS2": 14, ...}); values
            # may be nested objects that carry a "user" key -- those are
            # found by the outer scan anyway.
            for m in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*:', span):
                yield m.group(1)


def _clean(name: str) -> str | None:
    name = name.strip()
    if not name or len(name) < 3 or len(name) > 64:
        return None
    m = _ABAP_ROW.match(name)
    if m:
        name = m.group(1).strip()
    if name.upper() in _SKIP or " " in name and "@" not in name:
        return None
    if _EMAIL.fullmatch(name) or re.fullmatch(r"[A-Za-z0-9_.\-$*]+", name):
        return name
    return None


def harvest_usernames(text: str) -> List[str]:
    """Usernames that appear as values of user-like JSON keys in the text."""
    found: Dict[str, None] = {}
    for m in _USER_KEY.finditer(text):
        for raw in _candidates_from_value(text[m.end():m.end() + 20000]):
            for part in raw.split(","):          # "u1, u2" style values
                name = _clean(part)
                if name:
                    found.setdefault(name, None)
    return list(found)


class Pseudonymizer:
    """Two-way map real name <-> USER_nn for one prompt/response pair."""

    def __init__(self, names: Iterable[str] = ()):
        self.forward: Dict[str, str] = {}
        for n in names:
            self.add(n)

    def add(self, name: str) -> str:
        key = name.lower()
        if key not in self.forward:
            self.forward[key] = f"USER_{len(self.forward) + 1:02d}"
        return self.forward[key]

    @property
    def count(self) -> int:
        return len(self.forward)

    def _pattern(self) -> re.Pattern | None:
        if not self.forward:
            return None
        names = sorted(self.forward, key=len, reverse=True)
        return re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, names))
                          + r")(?![A-Za-z0-9_])", re.IGNORECASE)

    def apply(self, text: str) -> str:
        pat = self._pattern()
        if not pat:
            return text
        return pat.sub(lambda m: self.forward[m.group(1).lower()], text)

    def restore(self, text: str) -> str:
        if not self.forward or not text:
            return text
        back = {v: k for k, v in self.forward.items()}
        # Keep the original casing of the first sighting: SAP logins are
        # upper case and the reverse map holds the lowered key, so pick the
        # display form recorded in `display`.
        return re.sub(r"USER_\d{2}",
                      lambda m: self.display.get(m.group(0).upper(), m.group(0)),
                      text, flags=re.IGNORECASE)

    # Real-case spelling of each name, filled by `for_prompt`.
    display: Dict[str, str] = {}


def for_prompt(prompt: str) -> Tuple[str, Pseudonymizer]:
    """Return (pseudonymised prompt, map). Map is empty when nothing found."""
    p = Pseudonymizer()
    p.display = {}
    for name in harvest_usernames(prompt):
        alias = p.add(name)
        p.display.setdefault(alias, name)
    return p.apply(prompt), p


class PseudonymizingProvider:
    """
    Wraps any AIProvider. Everything except generate() is passed through, so
    .name, .status() and the failover chain keep working unchanged.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def generate(self, prompt: str) -> str:
        if not enabled():
            return self._inner.generate(prompt)
        safe_prompt, p = for_prompt(prompt)
        reply = self._inner.generate(safe_prompt)
        return p.restore(reply)
