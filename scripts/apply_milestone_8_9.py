from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
COL = ROOT / "collectors" / "sap_gui_collector.py"

def backup(p):
    b = p.with_suffix(p.suffix + ".pre_8_9.bak")
    if not b.exists():
        shutil.copy2(p, b)

backup(MAIN)
s = MAIN.read_text(encoding="utf-8")
s = s.replace(
    "collect_tcode_evidence(tasks, system_name=system_config.get('name'), client=system_config.get('client'))",
    "collect_tcode_evidence(tasks, system=system_config.get('name', 'UNKNOWN'), client=system_config.get('client', 'UNKNOWN'))",
)
MAIN.write_text(s, encoding="utf-8")

backup(COL)
s = COL.read_text(encoding="utf-8")
s = s.replace(
    "from reporting.evidence import create_evidence_id",
    "from reporting.evidence import EvidenceRecord, create_evidence_id, write_evidence_index",
    1,
)
if "from reporting.system_paths import system_evidence_root" not in s:
    anchor = "from reporting.evidence import EvidenceRecord, create_evidence_id, write_evidence_index\n"
    if anchor not in s:
        raise RuntimeError("Evidence import anchor not found")
    s = s.replace(anchor, anchor + "from reporting.system_paths import system_evidence_root\n", 1)

start = s.find("def collect_tcode_evidence(")
if start < 0:
    raise RuntimeError("collect_tcode_evidence() not found")
prefix = s[:start]

new_function = """def collect_tcode_evidence(
    tasks: list[dict],
    system: str = "UNKNOWN",
    client: str = "UNKNOWN",
) -> list[MetricResult]:
    # Production collector: three attempts per T-code, session recovery,
    # system-scoped screenshots, timestamps, and evidence index persistence.
    results = []

    try:
        session = get_scripting_session()
    except Exception as e:
        log.error("Could not acquire scripting session: %s", type(e).__name__)
        return results

    all_ocr_patterns = get_ocr_patterns()

    for task in tasks:
        tcode = task["tcode"]
        action_name = task.get("action", "simple")
        evidence_id = create_evidence_id(system or "UNKNOWN", tcode)
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        recovery_actions = []
        evidence_attempts = 0
        last_error = None
        final_screenshots = []
        extracted_data = {}

        for attempt in range(1, TCODE_MAX_ATTEMPTS + 1):
            evidence_attempts = attempt
            screenshots = []

            def capture(suffix: str = ""):
                name = f"{tcode}_{suffix}" if suffix else tcode
                path = capture_screenshot(
                    session,
                    name,
                    output_dir=str(system_evidence_root(system) / "screenshots"),
                )
                if path:
                    screenshots.append(path)
                return path

            try:
                action_fn = ACTIONS.get(action_name)
                if not action_fn:
                    raise ValueError(f"No action registered for '{action_name}'")

                extracted_data = action_fn(session, capture) or {}

                if not screenshots:
                    capture()

                ocr_patterns = all_ocr_patterns.get(tcode, {})
                if ocr_patterns and screenshots:
                    ocr_text = run_ocr(screenshots[-1])
                    ocr_data = extract_patterns(ocr_text, ocr_patterns)
                    for key, value in ocr_data.items():
                        extracted_data.setdefault(key, value)

                final_screenshots = screenshots
                last_error = None
                break

            except Exception as e:
                last_error = e
                kind = (
                    "non_retryable" if is_non_retryable(e)
                    else "session" if is_session_failure(e)
                    else "transient"
                )

                log.warning(
                    "T-code %s attempt %s/%s failed (kind=%s): %s",
                    tcode, attempt, TCODE_MAX_ATTEMPTS, kind, type(e).__name__,
                )

                if kind == "non_retryable" or attempt >= TCODE_MAX_ATTEMPTS:
                    break

                if kind == "session":
                    try:
                        recover_session(session)
                        recovery_actions.append(
                            f"session_recovery_after_attempt_{attempt}"
                        )
                    except Exception:
                        recovery_actions.append(
                            f"session_recovery_failed_after_attempt_{attempt}"
                        )

                    try:
                        session = get_scripting_session()
                        recovery_actions.append(
                            f"session_reacquired_before_attempt_{attempt + 1}"
                        )
                    except Exception as reacquire_error:
                        recovery_actions.append(
                            f"session_reacquire_failed_before_attempt_{attempt + 1}"
                        )
                        log.warning(
                            "Could not reacquire SAP GUI session: %s",
                            type(reacquire_error).__name__,
                        )
                else:
                    recovery_actions.append(
                        f"retry_after_{kind}_failure_{attempt}"
                    )

                DEFAULT_TCODE_POLICY.sleep_before_retry(attempt)

        finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        evidence_data = {
            **extracted_data,
            "evidence_id": evidence_id,
            "attempts": evidence_attempts,
            "system": system or "UNKNOWN",
            "client": client or "",
            "recovery_actions": recovery_actions,
            "started_at": started_at,
            "finished_at": finished_at,
        }

        if last_error is None:
            display = f"captured ({len(final_screenshots)})"
            detail = f"action={action_name}" + (
                f", succeeded on attempt {evidence_attempts}"
                if evidence_attempts > 1 else ""
            )
        else:
            display = "failed"
            detail = (
                f"action={action_name}, error={type(last_error).__name__}, "
                f"attempts={evidence_attempts}"
            )

        results.append(MetricResult(
            name=f"screenshot_{tcode}",
            value=None,
            display_value=display,
            status=Status.UNKNOWN,
            source="sap_gui_collector",
            tcode=tcode,
            detail=detail,
            screenshot_path=final_screenshots[0] if final_screenshots else None,
            screenshot_paths=final_screenshots,
            extra_data=evidence_data,
        ))

    try:
        records = []
        for item in results:
            data = getattr(item, "extra_data", {}) or {}
            records.append(EvidenceRecord(
                evidence_id=data.get("evidence_id", ""),
                system=system or "UNKNOWN",
                client=client or "",
                tcode=getattr(item, "tcode", "") or "",
                started_at=data.get("started_at", ""),
                finished_at=data.get("finished_at", ""),
                status=str(getattr(item, "display_value", "unknown")),
                attempts=int(data.get("attempts", 0) or 0),
                recovery_actions=list(data.get("recovery_actions", []) or []),
                screenshot_paths=list(getattr(item, "screenshot_paths", []) or []),
                extracted_data={
                    k: v for k, v in data.items()
                    if k not in {
                        "evidence_id", "attempts", "system", "client",
                        "recovery_actions", "started_at", "finished_at"
                    }
                },
            ))

        write_evidence_index(
            records,
            system_evidence_root(system) / "evidence_index.json",
        )
    except Exception as evidence_error:
        log.warning(
            "Evidence index write failed without affecting monitoring: %s",
            type(evidence_error).__name__,
        )

    log.info("Collected evidence for %s T-codes.", len(results))
    return results
"""
COL.write_text(prefix + new_function + "\n", encoding="utf-8")
print("Milestone 8.9 reliability/evidence repair applied.")
