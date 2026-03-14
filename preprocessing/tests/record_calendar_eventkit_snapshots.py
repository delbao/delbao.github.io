from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SWIFT_FETCH_SCRIPT = REPO_ROOT / "preprocessing" / "common" / "calendar_eventkit_fetch.swift"
FIXTURES_DIR = REPO_ROOT / "preprocessing" / "tests" / "fixtures" / "calendar_eventkit"
CALENDAR_NAMES = "Vanta,个人"


CASES = [
    {
        "case_id": "2026-03-10_recording_window",
        "recording_start": "2026-03-10T11:32:30",
        "recording_duration_seconds": 1891.42,
        "pad": timedelta(minutes=20),
    },
    {
        "case_id": "2026-03-11_recording_window",
        "recording_start": "2026-03-11T11:57:48",
        "recording_duration_seconds": 2101.28,
        "pad": timedelta(minutes=20),
    },
    {
        "case_id": "2026-03-06_wide_window",
        "recording_start": "2026-03-06T14:22:47",
        "recording_duration_seconds": 1157.24,
        "pad": timedelta(hours=4),
    },
    {
        "case_id": "2026-03-12_recording_window",
        "recording_start": "2026-03-12T11:03:23",
        "recording_duration_seconds": 1871.18,
        "pad": timedelta(minutes=20),
    },
]


def fetch_events(window_start: datetime, window_end: datetime) -> list[dict[str, object]]:
    command = [
        "/usr/bin/swift",
        str(SWIFT_FETCH_SCRIPT),
        str(window_start.timestamp()),
        str(window_end.timestamp()),
        CALENDAR_NAMES,
    ]
    output = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)
    payload = json.loads(output)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected EventKit payload shape")
    return payload


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        recording_start = datetime.fromisoformat(case["recording_start"])
        recording_end = recording_start + timedelta(seconds=float(case["recording_duration_seconds"]))
        pad = case["pad"]
        window_start = recording_start - pad
        window_end = recording_end + pad

        payload = fetch_events(window_start, window_end)
        fixture_events: list[dict[str, object]] = []
        for event in payload:
            try:
                start = datetime.fromtimestamp(float(event["start_epoch"]))
                end = datetime.fromtimestamp(float(event["end_epoch"]))
            except (KeyError, TypeError, ValueError):
                continue
            fixture_events.append(
                {
                    "calendar_name": str(event.get("calendar_name", "")),
                    "summary": str(event.get("summary", "")),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
            )

        document = {
            "case_id": case["case_id"],
            "recording_start": recording_start.isoformat(),
            "recording_duration_seconds": float(case["recording_duration_seconds"]),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "events": fixture_events,
        }
        output_path = FIXTURES_DIR / f"{case['case_id']}.json"
        output_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
