import json
import os
from datetime import datetime


def save_sm50_snapshot(
    system,
    client,
    extracted_data,
    history_root="reports/sm50_history",
):
    os.makedirs(history_root, exist_ok=True)

    timestamp = datetime.now().astimezone().isoformat()

    snapshot = {
        "timestamp": timestamp,
        "system": system,
        "client": str(client),
        "total_work_processes": extracted_data.get(
            "total_work_processes", 0
        ),
        "state_counts": extracted_data.get(
            "state_counts", {}
        ),
        "processes": extracted_data.get(
            "processes", []
        ),
    }

    filename = (
        f"{system}_{client}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    )

    path = os.path.join(history_root, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            snapshot,
            f,
            indent=2,
            ensure_ascii=False,
        )

    return path