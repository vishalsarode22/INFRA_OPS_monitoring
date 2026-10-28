"""
Deterministic ST22 analysis.

Analyzes structured ST22 dump evidence and produces:

- dump volume
- per-dump diagnostic records
- repeated runtime errors
- repeated programs
- repeated users
- repeated user/program/error combinations
- stable pattern keys
- latest dump
- deterministic findings
- investigation candidates for later AI/RCA analysis

This module does not use AI.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _clean(value: Any) -> str:
    """Return a normalized string value."""
    if value is None:
        return ""

    return str(value).strip()


def _make_pattern_key(
    user: str,
    program: str,
    runtime_error: str,
) -> str:
    """Create a stable correlation key."""

    return "|".join(
        [
            user or "<unknown-user>",
            runtime_error or "<unknown-error>",
            program or "<unknown-program>",
        ]
    )


def _normalize_dump(dump: dict) -> dict:
    """Normalize one raw ST22 dump record."""

    user = _clean(dump.get("user"))
    program = _clean(dump.get("program"))
    runtime_error = _clean(dump.get("runtime_error"))

    return {
        "date": _clean(dump.get("date")),
        "time": _clean(dump.get("time")),
        "host": _clean(dump.get("host")),
        "user": user,
        "client": _clean(dump.get("client")),
        "hold_status": _clean(dump.get("hold_status")),
        "runtime_error": runtime_error,
        "exception": _clean(dump.get("exception")),
        "program": program,
        "module": _clean(dump.get("module")),
        "transaction_id": _clean(dump.get("transaction_id")),
        "pattern_key": _make_pattern_key(
            user,
            program,
            runtime_error,
        ),
    }


def _build_dump_diagnostics(
    normalized: list[dict],
) -> list[dict]:
    """
    Add occurrence and repetition information
    to every dump.
    """

    user_counts = Counter(
        d["user"]
        for d in normalized
        if d["user"]
    )

    program_counts = Counter(
        d["program"]
        for d in normalized
        if d["program"]
    )

    error_counts = Counter(
        d["runtime_error"]
        for d in normalized
        if d["runtime_error"]
    )

    combination_counts = Counter(
        d["pattern_key"]
        for d in normalized
        if d["user"]
        and d["program"]
        and d["runtime_error"]
    )

    diagnostics = []

    for index, dump in enumerate(
        normalized,
        start=1,
    ):
        user = dump["user"]
        program = dump["program"]
        runtime_error = dump["runtime_error"]
        pattern_key = dump["pattern_key"]

        user_count = (
            user_counts.get(user, 0)
            if user
            else 0
        )

        program_count = (
            program_counts.get(program, 0)
            if program
            else 0
        )

        error_count = (
            error_counts.get(runtime_error, 0)
            if runtime_error
            else 0
        )

        combination_count = (
            combination_counts.get(pattern_key, 0)
            if (
                user
                and program
                and runtime_error
            )
            else 0
        )

        diagnostic = dict(dump)

        diagnostic.update(
            {
                "dump_index": index,
                "user_occurrence_count": user_count,
                "program_occurrence_count": program_count,
                "runtime_error_occurrence_count": error_count,
                "combination_occurrence_count": combination_count,
                "user_repeated": user_count > 1,
                "program_repeated": program_count > 1,
                "runtime_error_repeated": error_count > 1,
                "combination_repeated": combination_count > 1,
            }
        )

        diagnostics.append(diagnostic)

    return diagnostics


def _top_counts(
    counter: Counter,
    key_name: str,
) -> list[dict]:
    """Convert Counter values to JSON-friendly records."""

    return [
        {
            key_name: value,
            "count": count,
        }
        for value, count in counter.most_common()
    ]


def analyze_st22(extracted_data: dict) -> dict:
    """
    Analyze structured ST22 evidence.

    Expected input:

        {
            "dump_count": int,
            "dumps": [
                {
                    "date": "...",
                    "time": "...",
                    "host": "...",
                    "user": "...",
                    "client": "...",
                    "hold_status": "...",
                    "runtime_error": "...",
                    "exception": "...",
                    "program": "...",
                    "module": "...",
                    "transaction_id": "..."
                }
            ]
        }
    """

    if not isinstance(extracted_data, dict):
        extracted_data = {}

    raw_dumps = extracted_data.get("dumps") or []

    normalized = []

    for dump in raw_dumps:
        if not isinstance(dump, dict):
            continue

        normalized.append(
            _normalize_dump(dump)
        )

    dump_count = len(normalized)

    # ---------------------------------------------------------
    # Counters
    # ---------------------------------------------------------

    user_counts = Counter(
        d["user"]
        for d in normalized
        if d["user"]
    )

    program_counts = Counter(
        d["program"]
        for d in normalized
        if d["program"]
    )

    error_counts = Counter(
        d["runtime_error"]
        for d in normalized
        if d["runtime_error"]
    )

    combination_counts = Counter(
        (
            d["user"],
            d["program"],
            d["runtime_error"],
        )
        for d in normalized
        if (
            d["user"]
            and d["program"]
            and d["runtime_error"]
        )
    )

    # ---------------------------------------------------------
    # Per-dump diagnostics
    # ---------------------------------------------------------

    dump_diagnostics = _build_dump_diagnostics(
        normalized
    )

    # ---------------------------------------------------------
    # Repeated users
    # ---------------------------------------------------------

    repeated_users = [
        {
            "user": user,
            "count": count,
        }
        for user, count in user_counts.most_common()
        if count > 1
    ]

    # ---------------------------------------------------------
    # Repeated programs
    # ---------------------------------------------------------

    repeated_programs = [
        {
            "program": program,
            "count": count,
        }
        for program, count in program_counts.most_common()
        if count > 1
    ]

    # ---------------------------------------------------------
    # Repeated runtime errors
    # ---------------------------------------------------------

    repeated_errors = [
        {
            "runtime_error": error,
            "count": count,
        }
        for error, count in error_counts.most_common()
        if count > 1
    ]

    # ---------------------------------------------------------
    # Repeated user/program/error combinations
    # ---------------------------------------------------------

    repeated_combinations = []

    for (
        user,
        program,
        error,
    ), count in combination_counts.most_common():

        if count <= 1:
            continue

        repeated_combinations.append(
            {
                "user": user,
                "program": program,
                "runtime_error": error,
                "count": count,
                "pattern_key": _make_pattern_key(
                    user,
                    program,
                    error,
                ),
            }
        )

    # ---------------------------------------------------------
    # Health
    #
    # 0 dumps       HEALTHY
    # 1-4 dumps     WARNING
    # 5+ dumps      CRITICAL
    # ---------------------------------------------------------

    if dump_count == 0:
        health = "HEALTHY"

    elif dump_count >= 5:
        health = "CRITICAL"

    else:
        health = "WARNING"

    # ---------------------------------------------------------
    # Findings
    # ---------------------------------------------------------

    findings = []

    if dump_count == 0:

        findings.append(
            {
                "severity": "HEALTHY",
                "category": "no_abap_runtime_errors",
                "reason": (
                    "No ABAP short dumps were recorded "
                    "for today's ST22 selection."
                ),
                "dump_count": 0,
            }
        )

    elif dump_count < 5:

        findings.append(
            {
                "severity": "WARNING",
                "category": "abap_runtime_errors",
                "reason": (
                    f"{dump_count} ABAP dump(s) were "
                    "recorded today."
                ),
                "dump_count": dump_count,
            }
        )

    else:

        findings.append(
            {
                "severity": "CRITICAL",
                "category": "high_dump_volume",
                "reason": (
                    f"{dump_count} ABAP dumps were recorded "
                    "today, reaching or exceeding the "
                    "critical threshold of 5."
                ),
                "dump_count": dump_count,
            }
        )

    # ---------------------------------------------------------
    # Repeated runtime error findings
    # ---------------------------------------------------------

    for item in repeated_errors:

        findings.append(
            {
                "severity": "WARNING",
                "category": "repeated_runtime_error",
                "reason": (
                    f"Runtime error "
                    f"'{item['runtime_error']}' occurred "
                    f"{item['count']} times today."
                ),
                **item,
            }
        )

    # ---------------------------------------------------------
    # Repeated program findings
    # ---------------------------------------------------------

    for item in repeated_programs:

        findings.append(
            {
                "severity": "WARNING",
                "category": "repeated_dump_program",
                "reason": (
                    f"Program '{item['program']}' generated "
                    f"{item['count']} dumps today."
                ),
                **item,
            }
        )

    # ---------------------------------------------------------
    # Repeated user findings
    # ---------------------------------------------------------

    for item in repeated_users:

        findings.append(
            {
                "severity": "WARNING",
                "category": "repeated_dump_user",
                "reason": (
                    f"User '{item['user']}' was associated "
                    f"with {item['count']} dumps today."
                ),
                **item,
            }
        )

    # ---------------------------------------------------------
    # Repeated combination findings
    # ---------------------------------------------------------

    for item in repeated_combinations:

        findings.append(
            {
                "severity": "WARNING",
                "category": "repeated_user_program_error",
                "reason": (
                    f"User '{item['user']}' repeatedly "
                    f"triggered runtime error "
                    f"'{item['runtime_error']}' in program "
                    f"'{item['program']}'."
                ),
                **item,
            }
        )

    # ---------------------------------------------------------
    # Investigation candidates
    #
    # Every dump is an investigation candidate.
    # Repetition adds additional reasons.
    # ---------------------------------------------------------

    investigation_candidates = []

    for diagnostic in dump_diagnostics:

        reasons = [
            "ABAP short dump requires technical investigation"
        ]

        if diagnostic["user_repeated"]:
            reasons.append(
                "same user has multiple dumps"
            )

        if diagnostic["program_repeated"]:
            reasons.append(
                "same program has multiple dumps"
            )

        if diagnostic["runtime_error_repeated"]:
            reasons.append(
                "same runtime error has multiple occurrences"
            )

        if diagnostic["combination_repeated"]:
            reasons.append(
                "same user/program/runtime-error combination repeats"
            )

        investigation_candidates.append(
            {
                "dump_index": diagnostic["dump_index"],
                "pattern_key": diagnostic["pattern_key"],
                "date": diagnostic["date"],
                "time": diagnostic["time"],
                "host": diagnostic["host"],
                "user": diagnostic["user"],
                "client": diagnostic["client"],
                "runtime_error": diagnostic["runtime_error"],
                "exception": diagnostic["exception"],
                "program": diagnostic["program"],
                "module": diagnostic["module"],
                "transaction_id": diagnostic[
                    "transaction_id"
                ],
                "occurrence_count": diagnostic[
                    "combination_occurrence_count"
                ],
                "repeated": diagnostic[
                    "combination_repeated"
                ],
                "reasons": reasons,
            }
        )

    # ---------------------------------------------------------
    # Latest dump
    # ---------------------------------------------------------

    latest_dump = (
        dump_diagnostics[-1]
        if dump_diagnostics
        else None
    )

    # ---------------------------------------------------------
    # Final result
    # ---------------------------------------------------------

    return {
        "health": health,

        "summary": {
            "dump_count": dump_count,
            "unique_users": len(user_counts),
            "unique_programs": len(program_counts),
            "unique_runtime_errors": len(error_counts),
            "repeated_user_count": len(
                repeated_users
            ),
            "repeated_program_count": len(
                repeated_programs
            ),
            "repeated_runtime_error_count": len(
                repeated_errors
            ),
            "repeated_combination_count": len(
                repeated_combinations
            ),
        },

        "top_users": _top_counts(
            user_counts,
            "user",
        ),

        "top_programs": _top_counts(
            program_counts,
            "program",
        ),

        "top_runtime_errors": _top_counts(
            error_counts,
            "runtime_error",
        ),

        "repeated_users": repeated_users,

        "repeated_programs": repeated_programs,

        "repeated_runtime_errors": repeated_errors,

        "repeated_combinations": repeated_combinations,

        "dumps": dump_diagnostics,

        "investigation_candidates": investigation_candidates,

        "latest_dump": latest_dump,

        "findings": findings,
    }