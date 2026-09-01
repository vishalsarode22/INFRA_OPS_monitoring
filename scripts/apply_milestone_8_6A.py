from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "collectors" / "sap_gui_collector.py"

s = TARGET.read_text(encoding="utf-8")

old_import = "from sap_gui.tcode_navigator import recover_session\n"
new_import = old_import + (
    "from sap_gui.reliability import (\n"
    "    DEFAULT_TCODE_POLICY, is_non_retryable, is_session_failure,\n"
    ")\n"
)
if "DEFAULT_TCODE_POLICY" not in s:
    if old_import not in s:
        raise RuntimeError("Expected SAP GUI recovery import not found.")
    s = s.replace(old_import, new_import, 1)

s = s.replace(
    "TCODE_MAX_ATTEMPTS = 3  # 1 initial try + 2 retries\n",
    "TCODE_MAX_ATTEMPTS = DEFAULT_TCODE_POLICY.max_attempts\n",
)

start = s.find("        last_error = None\n        for attempt in range(1, TCODE_MAX_ATTEMPTS + 1):")
end = s.find("\n        if last_error is not None:\n", start)
if start == -1 or end == -1:
    raise RuntimeError("Could not locate T-code retry block.")

new_block = """        last_error = None
        last_error_kind = None

        for attempt in range(1, TCODE_MAX_ATTEMPTS + 1):
            screenshots = []

            def capture(suffix: str = ""):
                name = f"{tcode}_{suffix}" if suffix else tcode
                path = capture_screenshot(session, name)
                if path:
                    screenshots.append(path)
                return path

            try:
                action_fn = ACTIONS.get(action_name)
                if not action_fn:
                    raise ValueError(f"No action registered for '{action_name}'")

                extracted_data = action_fn(session, capture) or {}
                log.info(f"Extracted data for {tcode} (scripting): {extracted_data}")

                if not screenshots:
                    capture()

                ocr_patterns = all_ocr_patterns.get(tcode, {})
                if ocr_patterns and screenshots:
                    ocr_text = run_ocr(screenshots[-1])
                    ocr_data = extract_patterns(ocr_text, ocr_patterns)
                    for k, v in ocr_data.items():
                        extracted_data.setdefault(k, v)

                results.append(MetricResult(
                    name=f"screenshot_{tcode}",
                    value=None,
                    display_value=f"captured ({len(screenshots)})",
                    status=Status.UNKNOWN,
                    source="sap_gui_collector",
                    tcode=tcode,
                    detail=f"action={action_name}" + (
                        f", succeeded on attempt {attempt}" if attempt > 1 else ""
                    ),
                    screenshot_path=screenshots[0] if screenshots else None,
                    screenshot_paths=screenshots,
                    extra_data=extracted_data,
                ))
                last_error = None
                break

            except Exception as e:
                last_error = e
                last_error_kind = (
                    "non_retryable" if is_non_retryable(e)
                    else "session" if is_session_failure(e)
                    else "transient"
                )

                log.warning(
                    f"T-code {tcode} attempt {attempt}/{TCODE_MAX_ATTEMPTS} "
                    f"failed (kind={last_error_kind}, action={action_name}): {e}"
                )

                if last_error_kind == "non_retryable":
                    break

                if attempt < TCODE_MAX_ATTEMPTS:
                    recover_session(session)
                    DEFAULT_TCODE_POLICY.sleep_before_retry(attempt)

                    if last_error_kind == "session":
                        try:
                            session = get_scripting_session()
                            log.info(
                                f"Reacquired SAP GUI scripting session before "
                                f"retrying {tcode}."
                            )
                        except Exception as reacquire_error:
                            log.warning(
                                f"Could not reacquire SAP GUI session after "
                                f"{tcode} failure: {reacquire_error}"
                            )
"""
s = s[:start] + new_block + s[end:]

old_tail = 'log.error(f"Failed to collect T-code {tcode} after {TCODE_MAX_ATTEMPTS} attempts (action={action_name}): {last_error}")'
new_tail = 'log.error(f"Failed to collect T-code {tcode} after {TCODE_MAX_ATTEMPTS} attempts (action={action_name}, kind={last_error_kind}): {last_error}")'
if old_tail not in s:
    raise RuntimeError("Expected failure log not found.")
s = s.replace(old_tail, new_tail, 1)

TARGET.write_text(s, encoding="utf-8")
print("Milestone 8.6A reliability layer applied safely.")
