from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import unittest

from summary.models import CalendarEvent
from summary.pipeline import CalendarEventService


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "calendar_eventkit"


CASES = [
    {
        "fixture": "2026-03-10_recording_window.json",
        "expected_summaries": {
            "AI Platform - Sprint Planning",
            "Office Hours with Iccha",
        },
        "expected_match": "AI Platform - Sprint Planning",
    },
    {
        "fixture": "2026-03-11_recording_window.json",
        "expected_summaries": {
            "Del / Tony (Meet N Greet)",
            "Del / Andy - weekly 1:1",
            "Vanta SF Team Lunch Weekly",
        },
        "expected_match": "Del / Andy - weekly 1:1",
    },
    {
        "fixture": "2026-03-06_wide_window.json",
        "expected_summaries": {
            "AI Experiment Day - No meetings! No interviews!",
            "❌ 🗓️ FOCUS FRIDAYS [NAMER]",
            "Eng Onboarding: Frontend Q&A",
        },
        "expected_match": "Eng Onboarding: Frontend Q&A",
    },
    {
        "fixture": "2026-03-12_recording_window.json",
        "expected_summaries": {
            "[Placeholder Item]",
            "Eng Onboarding: Engineering Strategy",
        },
        "expected_match": "Eng Onboarding: Engineering Strategy",
    },
]


def _load_fixture(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _events_from_fixture(data: dict[str, object]) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for item in data.get("events", []):
        if not isinstance(item, dict):
            continue
        events.append(
            CalendarEvent(
                calendar_name=str(item["calendar_name"]),
                summary=str(item["summary"]),
                start=datetime.fromisoformat(str(item["start"])),
                end=datetime.fromisoformat(str(item["end"])),
                description=None,
                links=[],
                attendees=[],
            )
        )
    return events


class CalendarEventKitSnapshotIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = CalendarEventService(llm_client=None)

    def test_persisted_eventkit_snapshots_include_expected_events(self) -> None:
        for case in CASES:
            fixture_path = FIXTURES_DIR / case["fixture"]
            self.assertTrue(fixture_path.exists(), f"Missing fixture file: {fixture_path}")
            fixture = _load_fixture(fixture_path)
            summaries = {str(item["summary"]) for item in fixture.get("events", []) if isinstance(item, dict)}
            self.assertTrue(
                set(case["expected_summaries"]).issubset(summaries),
                f"Fixture {case['fixture']} missing expected summaries. Found: {sorted(summaries)}",
            )

    def test_matching_on_persisted_snapshots_is_stable(self) -> None:
        for case in CASES:
            fixture_path = FIXTURES_DIR / case["fixture"]
            fixture = _load_fixture(fixture_path)
            events = _events_from_fixture(fixture)
            recording_start = datetime.fromisoformat(str(fixture["recording_start"]))
            recording_end = recording_start + timedelta(seconds=float(fixture["recording_duration_seconds"]))
            matched = self.service.match_events(events, recording_start, recording_end)
            matched_summaries = [event.summary for event in matched]
            self.assertIn(
                case["expected_match"],
                matched_summaries,
                f"Expected match '{case['expected_match']}' not present for {case['fixture']}. Matched: {matched_summaries}",
            )

    def test_live_eventkit_matches_persisted_snapshot_when_enabled(self) -> None:
        if os.environ.get("RUN_CALENDAR_INTEGRATION") != "1":
            self.skipTest("Set RUN_CALENDAR_INTEGRATION=1 to compare live EventKit output with persisted fixtures")

        for case in CASES:
            fixture_path = FIXTURES_DIR / case["fixture"]
            fixture = _load_fixture(fixture_path)
            window_start = datetime.fromisoformat(str(fixture["window_start"]))
            window_end = datetime.fromisoformat(str(fixture["window_end"]))
            try:
                live_events = self.service.fetch_events(window_start, window_end)
            except RuntimeError as exc:
                message = str(exc).lower()
                if "access denied" in message or "calendar access denied" in message:
                    self.skipTest(f"Calendar permission is not granted for EventKit: {exc}")
                raise

            live_summary_set = {event.summary for event in live_events}
            expected_summary_set = {
                str(item["summary"]) for item in fixture.get("events", []) if isinstance(item, dict)
            }
            self.assertTrue(
                expected_summary_set.issubset(live_summary_set),
                f"Live EventKit output for {case['fixture']} differs from persisted snapshot."
                f" Expected subset: {sorted(expected_summary_set)}. Live: {sorted(live_summary_set)}",
            )


if __name__ == "__main__":
    unittest.main()
