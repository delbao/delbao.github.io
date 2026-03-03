from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class TranscriptSegment:
    start: float
    end: float
    start_srt: str | None
    end_srt: str | None
    text: str
    dominant_speaker: str | None


@dataclass
class RenderedTurn:
    speaker: str
    start: float
    end: float
    parts: list[str]


@dataclass
class SummaryResult:
    speaker_name_map: dict[str, str]
    rendered_transcript: str
    summary_markdown: str
    post_title: str
    post_source: str
    meeting_metadata: dict[str, object]
    video_url: str


@dataclass(frozen=True)
class CalendarEvent:
    calendar_name: str
    summary: str
    start: datetime
    end: datetime
    attendees: list[str]


@dataclass(frozen=True)
class PostContext:
    title: str
    source: str


@dataclass(frozen=True)
class RecordingMetadata:
    started_at: datetime
    title_hint: str


@dataclass
class TranscriptDocument:
    path: Path
    segments: list[TranscriptSegment]

    @property
    def source_name(self) -> str:
        return self.path.name

    @property
    def duration_seconds(self) -> float:
        return max((segment.end for segment in self.segments), default=0.0)

    @property
    def speaker_ids(self) -> list[str]:
        speaker_ids: list[str] = []
        seen: set[str] = set()
        for segment in self.segments:
            speaker_id = segment.dominant_speaker or "UNKNOWN"
            if speaker_id in seen:
                continue
            seen.add(speaker_id)
            speaker_ids.append(speaker_id)
        return speaker_ids


@dataclass(frozen=True)
class MeetingContext:
    fallback_title: str
    fallback_source: str
    candidate_events: list[CalendarEvent]
    recording_started_at: datetime
    recording_ended_at: datetime
