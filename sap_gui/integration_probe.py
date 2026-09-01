"""Controlled SAP GUI integration probe.

This intentionally does NOT launch SAP Logon or perform login. The operator
must already be logged into the intended system. The probe verifies the
actual SAP system/client identity and then executes exactly one configured
read-only monitoring T-code through the production collector.

A system can have:
    - a logical monitoring name, e.g. FUJI_QAS
    - an actual SAP SID, e.g. CEQ

If ``sap_system_id`` is configured in systems.yaml, the actual SAP SID is
used for identity validation while the logical system name is retained for
evidence/reporting.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from sap_gui.scripting_connection import get_scripting_session
from collectors.sap_gui_collector import collect_tcode_evidence
from core.config_loader import get_monitoring_tasks, get_systems


@dataclass(frozen=True)
class GuiIdentity:
    system: str
    client: str
    user: str


def inspect_active_session() -> GuiIdentity:
    """Read identity information from the currently active SAP GUI session."""
    session = get_scripting_session()

    return GuiIdentity(
        system=str(session.Info.SystemName or ""),
        client=str(session.Info.Client or ""),
        user=str(session.Info.User or ""),
    )


def validate_identity(
    expected_system: str,
    expected_client: str,
) -> GuiIdentity:
    """Verify that SAP GUI is connected to the intended SAP system/client.

    ``expected_system`` is the actual SAP SID expected from SAP GUI.
    """
    identity = inspect_active_session()

    if identity.system.upper() != expected_system.upper():
        raise RuntimeError(
            f"Active SAP system mismatch: expected {expected_system}, "
            f"found {identity.system}"
        )

    if str(identity.client) != str(expected_client):
        raise RuntimeError(
            f"Active SAP client mismatch: expected {expected_client}, "
            f"found {identity.client}"
        )

    return identity


def resolve_expected_sap_system(logical_system: str) -> str:
    """Resolve a logical monitoring name to its actual SAP SID.

    Example:

        FUJI_QAS -> CEQ

    If ``sap_system_id`` is not configured, the logical system name is used
    for backward compatibility with existing systems such as TST.
    """
    logical_system = str(logical_system or "").strip()

    for system in get_systems():
        configured_name = str(system.get("name", "")).strip()

        if configured_name.upper() == logical_system.upper():
            sap_system_id = str(
                system.get("sap_system_id")
                or configured_name
                or logical_system
            ).strip()

            return sap_system_id

    # Backward-compatible fallback.
    return logical_system


def run_single_tcode(
    system: str,
    client: str,
    tcode: str,
):
    """Execute one configured monitoring T-code."""
    tasks = get_monitoring_tasks()

    matches = [
        task
        for task in tasks
        if str(task.get("tcode", "")).upper() == tcode.upper()
    ]

    if not matches:
        raise RuntimeError(
            f"T-code {tcode} is not configured in monitoring_tasks.yaml"
        )

    return collect_tcode_evidence(
        [matches[0]],
        system=system,
        client=client,
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run one controlled read-only SAP GUI monitoring T-code."
        )
    )

    parser.add_argument("--system", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--tcode", default="AL08")

    args = parser.parse_args()

    # ``args.system`` is the logical monitoring name.
    #
    # Example:
    #   FUJI_QAS -> CEQ
    #
    # The actual SAP SID is used only for identity validation.
    # The logical name is still passed to the collector for evidence/reporting.
    expected_sap_system = resolve_expected_sap_system(args.system)

    identity = validate_identity(
        expected_sap_system,
        args.client,
    )

    print(
        f"SAP GUI identity OK: "
        f"logical_system={args.system}, "
        f"sap_system={identity.system}, "
        f"client={identity.client}, "
        f"user={identity.user}"
    )

    results = run_single_tcode(
        args.system,
        args.client,
        args.tcode,
    )

    if not results:
        raise SystemExit(
            "No result returned by SAP GUI collector."
        )

    result = results[0]

    data = getattr(result, "extra_data", {}) or {}

    print(f"T-code: {result.tcode}")
    print(f"Status: {result.display_value}")
    print(f"Attempts: {data.get('attempts', 0)}")
    print(f"Evidence ID: {data.get('evidence_id', '')}")
    print(
        f"Recovery actions: "
        f"{data.get('recovery_actions', [])}"
    )
    print(
        f"Screenshots: "
        f"{getattr(result, 'screenshot_paths', [])}"
    )

    if result.display_value == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()