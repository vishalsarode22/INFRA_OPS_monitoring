"""
ST03N workload analyzer.

Converts structured ST03N ALV data into monitoring findings.
"""

import re


def _number(value):
    """
    Convert SAP formatted numbers such as:

        511,7
        25.333
        1.020

    into Python floats.

    SAP GUI output uses locale formatting, so this function
    intentionally handles comma decimals and dot thousands.
    """

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(" ", "")

    # SAP format:
    # 5.106,8 -> 5106.8
    if "," in text:
        text = text.replace(".", "")
        text = text.replace(",", ".")

    else:
        # No decimal comma.
        # Treat dots as normal decimal separators.
        # This is mainly useful for already-normalized values.
        pass

    try:
        return float(text)

    except (TypeError, ValueError):
        return None


def _row(task_types, name):
    """
    Return a task-type row by name.
    """

    for row in task_types:
        if str(row.get("task_type", "")).upper() == name.upper():
            return row

    return None


def analyze_st03n(extracted_data):
    """
    Analyze structured ST03N workload data.

    Returns:
        {
            "health": "HEALTHY" | "WARNING" | "CRITICAL",
            "summary": {...},
            "findings": [...]
        }
    """

    if not extracted_data:
        return {
            "health": "UNKNOWN",
            "summary": {
                "task_type_count": 0,
            },
            "findings": [
                {
                    "severity": "WARNING",
                    "category": "st03n_data_missing",
                    "reason": "No ST03N workload data was available.",
                }
            ],
        }

    task_types = extracted_data.get("task_types", [])

    if not task_types:
        return {
            "health": "UNKNOWN",
            "summary": {
                "task_type_count": 0,
            },
            "findings": [
                {
                    "severity": "WARNING",
                    "category": "st03n_data_missing",
                    "reason": "ST03N returned no workload task types.",
                }
            ],
        }

    findings = []

    # ---------------------------------------------------------------
    # Important workload rows
    # ---------------------------------------------------------------

    dialog = _row(task_types, "DIALOG")
    background = _row(task_types, "BACKGROUND")
    rfc = _row(task_types, "RFC")
    update = _row(task_types, "UPDATE")
    other = _row(task_types, "OTHER")

    # ---------------------------------------------------------------
    # DIALOG response-time analysis
    # ---------------------------------------------------------------

    if dialog:

        response = _number(
            dialog.get("avg_response_ms")
        )

        roll_wait = _number(
            dialog.get("avg_roll_wait_ms")
        )

        network = _number(
            dialog.get("avg_network_ms")
        )

        gui = _number(
            dialog.get("avg_gui_ms")
        )

        if response is not None and response >= 1000:

            findings.append({
                "severity": "CRITICAL",
                "category": "high_dialog_response_time",
                "reason": (
                    "Average DIALOG response time is "
                    f"{response:.1f} ms."
                ),
                "task_type": "DIALOG",
                "metric": "avg_response_ms",
                "value": response,
            })

        elif response is not None and response >= 500:

            findings.append({
                "severity": "WARNING",
                "category": "elevated_dialog_response_time",
                "reason": (
                    "Average DIALOG response time is "
                    f"{response:.1f} ms."
                ),
                "task_type": "DIALOG",
                "metric": "avg_response_ms",
                "value": response,
            })

        # -----------------------------------------------------------
        # Roll wait
        # -----------------------------------------------------------

        if (
            roll_wait is not None
            and response is not None
            and response > 0
        ):

            ratio = roll_wait / response

            if ratio >= 0.50:

                findings.append({
                    "severity": "WARNING",
                    "category": "high_dialog_roll_wait",
                    "reason": (
                        "A significant portion of DIALOG "
                        "response time is spent waiting for roll-in/roll-out."
                    ),
                    "task_type": "DIALOG",
                    "roll_wait_ms": roll_wait,
                    "response_ms": response,
                    "ratio": round(ratio, 3),
                })

        # -----------------------------------------------------------
        # GUI time
        # -----------------------------------------------------------

        if (
            gui is not None
            and response is not None
            and response > 0
        ):

            ratio = gui / response

            if ratio >= 0.50:

                findings.append({
                    "severity": "WARNING",
                    "category": "high_dialog_gui_time",
                    "reason": (
                        "A significant portion of DIALOG "
                        "response time is attributed to GUI time."
                    ),
                    "task_type": "DIALOG",
                    "gui_ms": gui,
                    "response_ms": response,
                    "ratio": round(ratio, 3),
                })

        # -----------------------------------------------------------
        # Network time
        # -----------------------------------------------------------

        if (
            network is not None
            and response is not None
            and response > 0
        ):

            ratio = network / response

            if ratio >= 0.30:

                findings.append({
                    "severity": "WARNING",
                    "category": "high_dialog_network_time",
                    "reason": (
                        "A significant portion of DIALOG "
                        "response time is attributed to network time."
                    ),
                    "task_type": "DIALOG",
                    "network_ms": network,
                    "response_ms": response,
                    "ratio": round(ratio, 3),
                })

    # ---------------------------------------------------------------
    # RFC response-time analysis
    # ---------------------------------------------------------------

    if rfc:

        response = _number(
            rfc.get("avg_response_ms")
        )

        if response is not None and response >= 1000:

            findings.append({
                "severity": "CRITICAL",
                "category": "high_rfc_response_time",
                "reason": (
                    "Average RFC response time is "
                    f"{response:.1f} ms."
                ),
                "task_type": "RFC",
                "value": response,
            })

        elif response is not None and response >= 500:

            findings.append({
                "severity": "WARNING",
                "category": "elevated_rfc_response_time",
                "reason": (
                    "Average RFC response time is "
                    f"{response:.1f} ms."
                ),
                "task_type": "RFC",
                "value": response,
            })

    # ---------------------------------------------------------------
    # BACKGROUND response-time analysis
    # ---------------------------------------------------------------

    if background:

        response = _number(
            background.get("avg_response_ms")
        )

        db = _number(
            background.get("avg_db_ms")
        )

        if response is not None and response >= 1000:

            findings.append({
                "severity": "WARNING",
                "category": "high_background_response_time",
                "reason": (
                    "Average BACKGROUND response time is "
                    f"{response:.1f} ms."
                ),
                "task_type": "BACKGROUND",
                "value": response,
            })

        if (
            db is not None
            and response is not None
            and response > 0
            and db / response >= 0.30
        ):

            findings.append({
                "severity": "WARNING",
                "category": "background_db_time",
                "reason": (
                    "A significant portion of BACKGROUND "
                    "response time is attributed to database time."
                ),
                "task_type": "BACKGROUND",
                "db_ms": db,
                "response_ms": response,
                "ratio": round(db / response, 3),
            })

    # ---------------------------------------------------------------
    # OTHER task type
    # ---------------------------------------------------------------

    if other:

        response = _number(
            other.get("avg_response_ms")
        )

        wait = _number(
            other.get("avg_wait_ms")
        )

        if (
            response is not None
            and wait is not None
            and response > 0
            and wait / response >= 0.70
            and response >= 50
        ):

            findings.append({
                "severity": "WARNING",
                "category": "other_task_wait_time",
                "reason": (
                    "OTHER workload is dominated by wait time."
                ),
                "task_type": "OTHER",
                "wait_ms": wait,
                "response_ms": response,
                "ratio": round(wait / response, 3),
            })

    # ---------------------------------------------------------------
    # Determine overall health
    # ---------------------------------------------------------------

    severities = {
        finding.get("severity")
        for finding in findings
    }

    if "CRITICAL" in severities:
        health = "CRITICAL"

    elif "WARNING" in severities:
        health = "WARNING"

    else:
        health = "HEALTHY"

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------

    summary = {
        "task_type_count": len(task_types),
        "dialog": bool(dialog),
        "background": bool(background),
        "rfc": bool(rfc),
        "update": bool(update),
        "other": bool(other),
    }

    if dialog:
        summary["dialog_response_ms"] = _number(
            dialog.get("avg_response_ms")
        )

        summary["dialog_roll_wait_ms"] = _number(
            dialog.get("avg_roll_wait_ms")
        )

        summary["dialog_network_ms"] = _number(
            dialog.get("avg_network_ms")
        )

        summary["dialog_gui_ms"] = _number(
            dialog.get("avg_gui_ms")
        )

    return {
        "health": health,
        "summary": summary,
        "findings": findings,
    }