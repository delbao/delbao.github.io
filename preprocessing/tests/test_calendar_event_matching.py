from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from summary.models import CalendarEvent, TranscriptDocument, TranscriptSegment
from summary.pipeline import CalendarEventService


def _event(
    summary: str,
    start: datetime,
    end: datetime,
    *,
    calendar_name: str = "Vanta",
) -> CalendarEvent:
    return CalendarEvent(
        calendar_name=calendar_name,
        summary=summary,
        start=start,
        end=end,
        description=None,
        links=[],
        attendees=[],
    )


def _doc(name: str, duration_seconds: float) -> TranscriptDocument:
    return TranscriptDocument(
        path=Path("/tmp") / f"{name}.mp4.smart.diarization.jsonl",
        segments=[
            TranscriptSegment(
                start=0.0,
                end=duration_seconds,
                start_srt=None,
                end_srt=None,
                text="hello",
                dominant_speaker="SPEAKER_00",
            )
        ],
    )


class CalendarEventMatchingTests(unittest.TestCase):
    def test_back_to_back_prefers_next_meeting_start_within_grace(self) -> None:
        svc = CalendarEventService(llm_client=None)
        recording_start = datetime(2026, 3, 11, 11, 57, 48)
        recording_end = recording_start + timedelta(minutes=35)
        events = [
            _event("Del / Andy - weekly 1:1", datetime(2026, 3, 11, 11, 0, 0), datetime(2026, 3, 11, 11, 30, 0)),
            _event("Del / Tony (Meet N Greet)", datetime(2026, 3, 11, 11, 30, 0), datetime(2026, 3, 11, 12, 0, 0)),
            _event("Vanta SF Team Lunch Weekly", datetime(2026, 3, 11, 12, 0, 0), datetime(2026, 3, 11, 13, 0, 0)),
        ]

        matched = svc.match_events(events, recording_start, recording_end)
        self.assertEqual([event.summary for event in matched], ["Vanta SF Team Lunch Weekly"])

    def test_placeholder_is_filtered_when_real_event_exists(self) -> None:
        svc = CalendarEventService(llm_client=None)
        recording_start = datetime(2026, 3, 12, 11, 3, 23)
        recording_end = recording_start + timedelta(minutes=31)
        events = [
            _event("[Placeholder Item]", datetime(2026, 3, 12, 11, 0, 0), datetime(2026, 3, 12, 11, 30, 0)),
            _event("Eng Onboarding: Engineering Strategy", datetime(2026, 3, 12, 11, 0, 0), datetime(2026, 3, 12, 11, 30, 0)),
        ]

        matched = svc.match_events(events, recording_start, recording_end)
        self.assertEqual([event.summary for event in matched], ["Eng Onboarding: Engineering Strategy"])

    def test_weak_match_triggers_widened_retry_and_finds_real_event(self) -> None:
        svc = CalendarEventService(llm_client=None)
        document = _doc("2026-03-06 14-22-47", 1157.24)

        weak_events = [
            _event(
                "AI Experiment Day - No meetings! No interviews!",
                datetime(2026, 3, 6, 6, 0, 0),
                datetime(2026, 3, 6, 18, 0, 0),
            ),
            _event(
                "❌ 🗓️ FOCUS FRIDAYS [NAMER]",
                datetime(2026, 3, 6, 6, 0, 0),
                datetime(2026, 3, 6, 18, 0, 0),
            ),
        ]
        widened_events = weak_events + [
            _event(
                "Eng Onboarding: Frontend Q&A",
                datetime(2026, 3, 6, 11, 30, 0),
                datetime(2026, 3, 6, 12, 0, 0),
            )
        ]

        with patch.object(CalendarEventService, "fetch_events", side_effect=[weak_events, widened_events]) as mocked_fetch:
            context = svc.build_meeting_context(document)

        self.assertEqual(mocked_fetch.call_count, 2)
        self.assertEqual(context.fallback_title, "Eng Onboarding: Frontend Q&A")
        self.assertEqual([event.summary for event in context.candidate_events], ["Eng Onboarding: Frontend Q&A"])

    def test_all_weak_matches_fall_back_to_filename_title(self) -> None:
        svc = CalendarEventService(llm_client=None)
        document = _doc("2026-03-12 11-03-23", 1871.18)
        weak_events = [
            _event("[Placeholder Item]", datetime(2026, 3, 12, 11, 0, 0), datetime(2026, 3, 12, 11, 30, 0)),
        ]

        with patch.object(CalendarEventService, "fetch_events", side_effect=[weak_events, weak_events]):
            context = svc.build_meeting_context(document)

        self.assertEqual(context.fallback_title, "2026-03-12 11-03-23.mp4.smart.diarization")
        self.assertEqual(context.candidate_events, [])

    def test_normalize_event_window_shifts_stale_recurring_master_to_query_window(self) -> None:
        start = datetime(2026, 3, 9, 10, 30, 0)
        end = datetime(2026, 3, 9, 11, 0, 0)
        window_start = datetime(2026, 3, 10, 11, 12, 30)
        window_end = datetime(2026, 3, 10, 12, 24, 1)

        normalized_start, normalized_end = CalendarEventService.normalize_event_window(
            start,
            end,
            window_start,
            window_end,
        )

        self.assertEqual(normalized_start, datetime(2026, 3, 10, 10, 30, 0))
        self.assertEqual(normalized_end, datetime(2026, 3, 10, 11, 0, 0))


if __name__ == "__main__":
    unittest.main()
