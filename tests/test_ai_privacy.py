"""SAP usernames must never reach an external AI provider."""

import json

from evaluation.ai_privacy import (
    PseudonymizingProvider, for_prompt, harvest_usernames)


class Echo:
    name = "echo"

    def __init__(self):
        self.seen = None

    def generate(self, prompt):
        self.seen = prompt
        return "Root cause: USER_01 ran ZSD_REPORT; USER_02 holds the lock. " \
               "Contact user_01 today."


CONTEXT = {
    "dumps": [{"user": "BASIS2", "program": "ZSD_REPORT"},
              {"user": "PS4_ADMIN", "program": "SAPLSTRD"}],
    "top_users": {"BASIS2": 14, "JSMITH": 2},
    "locked_users": ["JSMITH", "DDIC"],
    "et_short_dumps": ["10:22:11|BASIS2"],
    "owner_email": "j.smith@client.example",
    "note": "lock held by basis2 since 09:00",
}


def test_harvests_string_list_object_and_abap_rows():
    names = set(harvest_usernames(json.dumps(CONTEXT, indent=1)))
    assert {"BASIS2", "PS4_ADMIN", "JSMITH", "j.smith@client.example"} <= names
    assert "DDIC" not in names            # system user, never rewritten


def test_prompt_has_no_real_names_and_reply_is_restored():
    prompt = "Analyse:\n" + json.dumps(CONTEXT, indent=1)
    echo = Echo()
    reply = PseudonymizingProvider(echo).generate(prompt)

    for real in ("BASIS2", "PS4_ADMIN", "JSMITH", "j.smith@client.example", "basis2"):
        assert real not in echo.seen, f"{real} reached the provider"
    assert "USER_01" in echo.seen
    assert "lock held by USER_01" in echo.seen      # free-text mention, mixed case

    assert "BASIS2 ran ZSD_REPORT" in reply
    assert "USER_" not in reply.upper()
    assert "ZSD_REPORT" in echo.seen                # programs are not users


def test_passthrough_attributes():
    assert PseudonymizingProvider(Echo()).name == "echo"


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("AI_PSEUDONYMISE_USERS", "false")
    echo = Echo()
    PseudonymizingProvider(echo).generate('{"user": "BASIS2"}')
    assert "BASIS2" in echo.seen


def test_no_names_is_a_no_op():
    text, p = for_prompt('{"cpu": 91, "program": "ZSD_REPORT"}')
    assert p.count == 0 and "ZSD_REPORT" in text
