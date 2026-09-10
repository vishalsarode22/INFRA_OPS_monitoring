"""Offline deterministic provider used by tests and local development."""

from __future__ import annotations

import json
import re

from evaluation.providers.base import AIProvider


class MockAIProvider(AIProvider):
    name = "mock"

    # The prompt is instructions followed by an EVIDENCE section. The mock
    # must classify on the EVIDENCE, never on the instructions: the
    # instruction text legitimately mentions ST22, GETWA_NOT_ASSIGNED and a
    # "dumps" block as worked examples, so scanning the whole prompt made
    # every incident -- a lock-count alert, a response-time alert -- look
    # like a short-dump incident and return ABAP_RUNTIME_ERROR. That is
    # what broke the generic-path tests when the dump-attribution guidance
    # was added to the template.
    _EVIDENCE_MARKER = "EVIDENCE\n--------"

    @classmethod
    def _evidence_part(cls, prompt: str) -> str:
        text = prompt or ""
        idx = text.rfind(cls._EVIDENCE_MARKER)
        return text[idx + len(cls._EVIDENCE_MARKER):] if idx >= 0 else text

    def generate(self, prompt: str) -> str:
        evidence = self._evidence_part(prompt)
        evidence_upper = evidence.upper()

        # -------------------------------------------------------------
        # ST22-specific deterministic response
        # -------------------------------------------------------------
        # A dump INCIDENT, not merely a dump METRIC. sap.st22.dump_count is
        # part of the normal metric set and appears in the evidence of every
        # incident on a system that had any dumps today; its presence says
        # nothing about what the incident under analysis is about. The
        # signals that do are a named runtime error, the ST22 evidence
        # narrative, or the "dumps" attribution block that only a dump
        # incident carries.
        if (
            "ST22" in evidence_upper
            and (
                "GETWA_NOT_ASSIGNED" in evidence_upper
                or "ABAP SHORT DUMP" in evidence_upper
                or '"RUNTIME_ERROR"' in evidence_upper
                or '"DUMPS"' in evidence_upper
            )
        ):
            # Field extraction is scoped to the evidence for the same reason.
            prompt = evidence
            user = self._extract(
                prompt,
                r'"user"\s*:\s*"([^"]+)"',
                "unknown",
            )

            runtime_error = self._extract(
                prompt,
                r'"runtime_error"\s*:\s*"([^"]+)"',
                "ABAP_RUNTIME_ERROR",
            )

            program = self._extract(
                prompt,
                r'"program"\s*:\s*"([^"]+)"',
                "unknown",
            )

            host = self._extract(
                prompt,
                r'"host"\s*:\s*"([^"]+)"',
                "unknown",
            )

            transaction_id = self._extract(
                prompt,
                r'"transaction_id"\s*:\s*"([^"]+)"',
                "",
            )

            return json.dumps(
                {
                    "severity": "WARNING",
                    "root_cause": {
                        "category": "ABAP_RUNTIME_ERROR",
                        "description": (
                            f"ABAP short dump {runtime_error} was detected "
                            f"in program {program}. The supplied ST22 "
                            "evidence indicates an ABAP runtime failure "
                            "requiring technical investigation."
                        ),
                    },
                    "confidence": "MEDIUM",
                    "confidence_score": 0.75,
                    "supporting_evidence": [
                        (
                            "ST22 recorded an ABAP short dump with "
                            f"runtime error {runtime_error}."
                        ),
                        f"The affected program is {program}.",
                        f"The recorded application host is {host}.",
                        (
                            f"The dump is associated with user {user}; "
                            "this identifies execution context and does "
                            "not establish causality."
                        ),
                    ],
                    "contradicting_evidence": [
                        (
                            "The supplied ST22 evidence represents a "
                            "single occurrence and does not establish "
                            "recurrence."
                        )
                    ],
                    "recommended_actions": [
                        "Open the full ST22 dump and review Error Analysis.",
                        (
                            "Review the termination location and "
                            "ABAP source-code extract."
                        ),
                        (
                            "Review the call stack and identify the "
                            "failing ABAP statement."
                        ),
                        (
                            f"Investigate program {program} and the "
                            f"circumstances in which {runtime_error} occurred."
                        ),
                        (
                            "Correlate the dump timestamp with SM21, "
                            "ST03N and relevant jobs."
                        ),
                        (
                            f"Use transaction ID {transaction_id} as an "
                            "investigation reference."
                            if transaction_id
                            else "Use the ST22 dump timestamp as an investigation reference."
                        ),
                    ],
                    "limitations": [
                        (
                            "This is an offline mock response; no external "
                            "LLM was called."
                        ),
                        (
                            "ST22 list-level evidence does not establish "
                            "the exact failing ABAP statement."
                        ),
                    ],
                },
                ensure_ascii=False,
            )

        # -------------------------------------------------------------
        # Generic response
        #
        # Important:
        # Keep this generic so existing non-ST22 tests and scenarios
        # continue to behave as before.
        # -------------------------------------------------------------
        return json.dumps(
            {
                "severity": "WARNING",
                "root_cause": {
                    "category": "MONITORING_REVIEW",
                    "description": (
                        "Monitoring evidence indicates a condition "
                        "requiring review."
                    ),
                },
                "confidence": "MEDIUM",
                "confidence_score": 0.60,
                "supporting_evidence": [
                    "The monitoring engine supplied warning evidence."
                ],
                "contradicting_evidence": [],
                "recommended_actions": [
                    "Review the affected metrics and recent events."
                ],
                "limitations": [
                    (
                        "This is an offline mock response; no external "
                        "LLM was called."
                    )
                ],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _extract(
        prompt: str,
        pattern: str,
        default: str,
    ) -> str:
        match = re.search(
            pattern,
            prompt,
            flags=re.IGNORECASE,
        )

        return match.group(1) if match else default