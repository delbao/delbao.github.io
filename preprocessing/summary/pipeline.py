from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from .config import (
    CALENDAR_EARLY_START_GRACE,
    CALENDAR_LATE_START_GRACE,
    CALENDAR_MAX_REASONABLE_DURATION,
    CALENDAR_NAMES,
    CALENDAR_NEAREST_START_MAX_DELTA,
    CALENDAR_QUERY_PADDING,
    CALENDAR_START_TIE_WINDOW,
    CALENDAR_TRANSCRIPT_EXCERPT_CHARS,
    DEFAULT_LLM_RETRIES,
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    DEFAULT_POST_SOURCE,
    FUZZY_NAME_THRESHOLD,
    LOGGER,
    LOGS_DIR,
    PHONETIC_FUZZY_NAME_THRESHOLD,
    PRIVATE_POSTS_DIR,
    assemble_prompt,
    load_prompt,
    load_speaker_name_corrections,
)
from common.llm import LLMClient
from .models import CalendarEvent, MeetingContext, PostContext, RecordingMetadata, SummaryResult, TranscriptDocument, TranscriptSegment
from common.text import TextNormalizer
from .text import TranscriptRenderer


class LoggingManager:
    @staticmethod
    def setup(log_path: Path) -> None:
        LOGGER.setLevel(logging.INFO)
        LOGGER.handlers.clear()
        LOGGER.propagate = False

        log_path.parent.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)

        LOGGER.addHandler(stream_handler)
        LOGGER.addHandler(file_handler)
        LOGGER.info("Logging to %s", log_path)


class TranscriptIO:
    @staticmethod
    def load(path: str | Path) -> TranscriptDocument:
        transcript_path = Path(path).expanduser()
        LOGGER.info("Loading transcript segments from %s", transcript_path)
        segments: list[TranscriptSegment] = []
        with transcript_path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON on line {line_number} of {transcript_path}") from exc

                text = str(item.get("text", "")).strip()
                if not text:
                    continue

                segments.append(
                    TranscriptSegment(
                        start=float(item.get("start", 0.0) or 0.0),
                        end=float(item.get("end", 0.0) or 0.0),
                        start_srt=item.get("start_srt"),
                        end_srt=item.get("end_srt"),
                        text=text,
                        dominant_speaker=item.get("dominant_speaker"),
                    )
                )

        LOGGER.info("Loaded %s transcript segments", len(segments))
        return TranscriptDocument(path=transcript_path, segments=segments)


class SpeakerNameService:
    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def correct_name(self, name: str) -> str:
        cleaned = name.strip()
        if not cleaned or cleaned.startswith("SPEAKER_") or cleaned == "UNKNOWN":
            return cleaned

        normalized = TextNormalizer.normalize_name_key(cleaned)
        if not normalized:
            return cleaned

        corrections = load_speaker_name_corrections()
        if normalized in corrections:
            return corrections[normalized]

        best_name = cleaned
        best_score = 0.0
        best_soundex_match = False
        normalized_soundex = TextNormalizer.soundex(normalized)
        for candidate_key, replacement_name in corrections.items():
            score = difflib.SequenceMatcher(None, normalized, candidate_key).ratio()
            soundex_match = normalized_soundex and normalized_soundex == TextNormalizer.soundex(candidate_key)
            if score > best_score:
                best_score = score
                best_name = replacement_name
                best_soundex_match = bool(soundex_match)

        if best_score >= FUZZY_NAME_THRESHOLD:
            return best_name
        if best_soundex_match and best_score >= PHONETIC_FUZZY_NAME_THRESHOLD:
            return best_name

        return cleaned

    def summarize(
        self,
        transcript_text: str,
        speaker_ids: list[str],
        source_name: str,
        meeting_context: MeetingContext,
    ) -> tuple[dict[str, str], str, int]:
        speaker_list = ", ".join(speaker_ids)
        LOGGER.info("Preparing one-pass summary for %s speaker IDs", len(speaker_ids))
        system_prompt = assemble_prompt(
            "meeting_summary_system.txt",
            "meeting_summary_system_metadata.txt",
        )
        user_prompt = assemble_prompt(
            "meeting_summary_user.txt",
            "meeting_summary_user_metadata.txt",
        ).format(
            source_name=source_name,
            speaker_list=speaker_list,
            transcript_text=transcript_text,
            meeting_context_json=json.dumps(
                self.serialize_meeting_context(meeting_context),
                ensure_ascii=False,
                indent=2,
            ),
        )

        parsed: dict[str, object] | None = None
        raw_response = ""
        for attempt in range(1, self.llm_client.retries + 2):
            raw_response = self.llm_client.complete_text(system_prompt, user_prompt)
            try:
                parsed = self.llm_client.extract_json_object(raw_response)
                LOGGER.info("Parsed structured LLM response")
                break
            except json.JSONDecodeError as exc:
                LOGGER.warning(
                    "Failed to parse LLM JSON response on attempt %s/%s: %s",
                    attempt,
                    self.llm_client.retries + 1,
                    exc,
                )
                LOGGER.warning("Raw LLM response prefix: %r", raw_response[:1000])
                if attempt > self.llm_client.retries:
                    raise
                sleep_seconds = min(2 ** (attempt - 1), 8)
                LOGGER.info("Retrying full LLM call after parse failure in %ss", sleep_seconds)
                time.sleep(sleep_seconds)

        if parsed is None:
            raise ValueError("Failed to parse structured LLM response")

        raw_speaker_map = parsed.get("speaker_map", {})
        if not isinstance(raw_speaker_map, dict):
            raise ValueError("Expected 'speaker_map' to be a JSON object")

        resolved_map: dict[str, str] = {}
        for speaker_id in speaker_ids:
            value = raw_speaker_map.get(speaker_id, speaker_id)
            if not isinstance(value, str):
                resolved_map[speaker_id] = speaker_id
                continue
            guessed_name = self.correct_name(value)
            resolved_map[speaker_id] = guessed_name or speaker_id

        LOGGER.info("Resolved speaker map: %s", resolved_map)
        summary_markdown = parsed.get("summary_markdown", "")
        if not isinstance(summary_markdown, str) or not summary_markdown.strip():
            raise ValueError("Expected non-empty string 'summary_markdown' in LLM response")
        selected_event_index = parsed.get("selected_event_index", 0)
        if not isinstance(selected_event_index, int):
            raise ValueError("Expected integer 'selected_event_index' in LLM response")
        return (
            resolved_map,
            self.postprocess_summary(summary_markdown.strip(), resolved_map),
            selected_event_index,
        )

    @staticmethod
    def serialize_meeting_context(meeting_context: MeetingContext) -> dict[str, object]:
        return {
            "fallback_title": meeting_context.fallback_title,
            "fallback_source": meeting_context.fallback_source,
            "recording_started_at": meeting_context.recording_started_at.isoformat(),
            "recording_ended_at": meeting_context.recording_ended_at.isoformat(),
            "candidate_events": [
                {
                    "index": index,
                    "title": event.summary,
                    "source": CalendarEventService.calendar_source_label(event.calendar_name),
                    "calendar_name": event.calendar_name,
                    "start_at": event.start.isoformat(),
                    "end_at": event.end.isoformat(),
                    "description": event.description,
                    "links": event.links,
                    "attendees": event.attendees,
                }
                for index, event in enumerate(meeting_context.candidate_events, start=1)
            ],
        }

    @staticmethod
    def postprocess_summary(summary_markdown: str, speaker_name_map: dict[str, str]) -> str:
        updated = summary_markdown
        for speaker_id, speaker_name in speaker_name_map.items():
            if not speaker_name or speaker_name == speaker_id:
                continue
            updated = re.sub(rf"\b{re.escape(speaker_id)}\b", speaker_name, updated)

        for speaker_name in sorted(
            {name for name in speaker_name_map.values() if name and not name.startswith("SPEAKER_")},
            key=len,
            reverse=True,
        ):
            updated = re.sub(
                rf"\b{re.escape(speaker_name)}\s*\(\s*{re.escape(speaker_name)}\s*\)",
                speaker_name,
                updated,
            )
            updated = re.sub(
                rf"\b{re.escape(speaker_name)}\s*/\s*{re.escape(speaker_name)}\b",
                speaker_name,
                updated,
            )
            updated = re.sub(
                rf"\b{re.escape(speaker_name)}\s*,\s*{re.escape(speaker_name)}\b",
                speaker_name,
                updated,
            )
        return updated


class CalendarEventService:
    _TRIVIAL_LINK_HOSTS = {
        "calendar.google.com",
        "meet.google.com",
        "zoom.us",
        "teams.microsoft.com",
        "webex.com",
    }
    _USEFUL_LINK_HOST_MARKERS = (
        "docs.google.com",
        "drive.google.com",
        "notion.so",
        "atlassian.net",
        "confluence",
        "figma.com",
        "miro.com",
        "github.com",
    )
    _USEFUL_PATH_MARKERS = (
        "/document/",
        "/presentation/",
        "/spreadsheets/",
        "/file/d/",
        "/wiki/",
        "/slides/",
        "/deck",
        "/doc",
        "/paper",
    )
    _USEFUL_FILE_SUFFIXES = (".pdf", ".ppt", ".pptx", ".key", ".doc", ".docx", ".xls", ".xlsx")
    _LOW_SIGNAL_TITLE_MARKERS = (
        "placeholder",
        "no meeting",
        "focus",
        "ooo",
        "out of office",
    )

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def build_meeting_context(self, document: TranscriptDocument) -> MeetingContext:
        metadata = FileNamingService.derive_recording_metadata(document.path)
        recording_start = metadata.started_at
        recording_end = recording_start + timedelta(seconds=max(document.duration_seconds, 0.0))
        query_start = recording_start - CALENDAR_QUERY_PADDING
        query_end = recording_end + CALENDAR_QUERY_PADDING
        LOGGER.info("Resolving title from Calendar for recording window %s to %s", recording_start, recording_end)

        calendar_events = self.fetch_events(query_start, query_end)

        matched_events = self.match_events(calendar_events, recording_start, recording_end)
        if matched_events and all(self.is_weak_match(event) for event in matched_events):
            widened_start = recording_start - timedelta(hours=4)
            widened_end = recording_end + timedelta(hours=4)
            LOGGER.info(
                "Initial calendar match was weak; retrying with widened window %s to %s",
                widened_start,
                widened_end,
            )
            try:
                widened_events = self.fetch_events(widened_start, widened_end)
                widened_matched = self.match_events(widened_events, recording_start, recording_end)
                if widened_matched and any(not self.is_weak_match(event) for event in widened_matched):
                    LOGGER.info("Using widened-window match with %s events", len(widened_matched))
                    matched_events = widened_matched
            except RuntimeError as exc:
                LOGGER.warning("Widened calendar lookup failed; keeping original weak match: %s", exc)
        if matched_events and all(self.is_weak_match(event) for event in matched_events):
            LOGGER.info("All matched events remain weak; falling back to filename-derived title context")
            matched_events = []
        fallback_title = matched_events[0].summary if len(matched_events) == 1 else metadata.title_hint
        fallback_source = (
            self.calendar_source_label(matched_events[0].calendar_name)
            if len(matched_events) == 1
            else DEFAULT_POST_SOURCE
        )
        return MeetingContext(
            fallback_title=fallback_title,
            fallback_source=fallback_source,
            candidate_events=matched_events,
            recording_started_at=recording_start,
            recording_ended_at=recording_end,
        )

    def fetch_events(self, window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        return self.fetch_events_eventkit(window_start, window_end)

    def fetch_events_eventkit(self, window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        script_path = Path(__file__).resolve().parents[1] / "common" / "calendar_eventkit_fetch.swift"
        if not script_path.exists():
            raise RuntimeError(f"EventKit fetch script missing: {script_path}")

        command = [
            "/usr/bin/swift",
            str(script_path),
            str(window_start.timestamp()),
            str(window_end.timestamp()),
            ",".join(CALENDAR_NAMES),
        ]
        try:
            raw_output = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise RuntimeError("swift runtime is required for EventKit calendar fetch") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"EventKit calendar query failed: {exc.output.strip()}") from exc

        if not raw_output.strip():
            return []

        try:
            payloads = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON from EventKit calendar query: {exc}") from exc

        if not isinstance(payloads, list):
            raise RuntimeError("EventKit calendar query returned non-list payload")

        events: list[CalendarEvent] = []
        for item in payloads:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary", "")).strip()
            calendar_name = str(item.get("calendar_name", "")).strip()
            if not summary or not calendar_name:
                continue

            try:
                start_epoch = float(item.get("start_epoch"))
                end_epoch = float(item.get("end_epoch"))
            except (TypeError, ValueError):
                continue
            start = datetime.fromtimestamp(start_epoch)
            end = datetime.fromtimestamp(end_epoch)

            description_raw = item.get("description")
            description = str(description_raw).strip() if isinstance(description_raw, str) else None
            event_url_raw = item.get("event_url")
            event_url = str(event_url_raw).strip() if isinstance(event_url_raw, str) else ""
            attendees_raw = item.get("attendees")
            attendees: list[str] = []
            if isinstance(attendees_raw, list):
                attendees = [str(attendee).strip() for attendee in attendees_raw if str(attendee).strip()]

            links = self.extract_invite_links(description, event_url)
            events.append(
                CalendarEvent(
                    calendar_name=calendar_name,
                    summary=summary,
                    start=start,
                    end=end,
                    description=description or None,
                    links=links,
                    attendees=attendees,
                )
            )

        deduped_events = self.dedupe_sort_events(events)
        LOGGER.info("Fetched %s candidate Calendar events from %s via EventKit", len(deduped_events), ", ".join(CALENDAR_NAMES))
        return deduped_events

    def fetch_events_osascript(self, window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
        script = f'''
on sanitizeText(value)
  set originalDelimiters to AppleScript's text item delimiters
  set AppleScript's text item delimiters to character id 31
  set value to text items of (value as text)
  set AppleScript's text item delimiters to " "
  set value to value as text
  set AppleScript's text item delimiters to character id 30
  set value to text items of value
  set AppleScript's text item delimiters to " "
  set value to value as text
  set AppleScript's text item delimiters to originalDelimiters
  return value
end sanitizeText

on isoStringForDate(theDate)
  set yearText to text -4 thru -1 of ("0000" & (year of theDate as integer))
  set monthValue to month of theDate as integer
  set monthText to text -2 thru -1 of ("00" & monthValue)
  set dayText to text -2 thru -1 of ("00" & (day of theDate as integer))
  set hourText to text -2 thru -1 of ("00" & (hours of theDate as integer))
  set minuteText to text -2 thru -1 of ("00" & (minutes of theDate as integer))
  set secondText to text -2 thru -1 of ("00" & (seconds of theDate as integer))
  return yearText & "-" & monthText & "-" & dayText & "T" & hourText & ":" & minuteText & ":" & secondText
end isoStringForDate

set allowedCalendars to {{"Vanta", "个人"}}
{self.build_applescript_date("windowStart", window_start)}
{self.build_applescript_date("windowEnd", window_end)}
set fieldSeparator to character id 31
set rowSeparator to character id 30
set attendeeSeparator to character id 29

tell application "Calendar"
  set outputRows to {{}}
  repeat with calendarName in allowedCalendars
    set matchingCalendars to every calendar whose name is (calendarName as text)
    repeat with theCal in matchingCalendars
      set matchingEvents to every event of theCal whose end date ≥ windowStart and start date ≤ windowEnd
      repeat with e in matchingEvents
        if (allday event of e) is false then
          set endDateValue to end date of e
          set summaryText to summary of e as text
          set summaryText to my sanitizeText(summaryText)
          set descriptionText to ""
          try
            set descriptionText to description of e as text
          end try
          set descriptionText to my sanitizeText(descriptionText)
          set urlText to ""
          try
            set urlText to url of e as text
          end try
          set urlText to my sanitizeText(urlText)
          set attendeeTexts to {{}}
          try
            repeat with attendeeItem in (attendees of e)
              set attendeeText to ""
              try
                set attendeeText to display name of attendeeItem as text
              end try
              if attendeeText is "" then
                try
                  set attendeeText to email of attendeeItem as text
                end try
              end if
              if attendeeText is not "" then
                set attendeeText to my sanitizeText(attendeeText)
                copy attendeeText to end of attendeeTexts
              end if
            end repeat
          end try
          set AppleScript's text item delimiters to attendeeSeparator
          set attendeeTextValue to attendeeTexts as text
          set AppleScript's text item delimiters to ""
          set rowText to (name of theCal as text) & fieldSeparator & summaryText & fieldSeparator & my isoStringForDate(start date of e) & fieldSeparator & my isoStringForDate(endDateValue) & fieldSeparator & attendeeTextValue & fieldSeparator & descriptionText & fieldSeparator & urlText
          copy rowText to end of outputRows
        end if
      end repeat
    end repeat
  end repeat
end tell

set AppleScript's text item delimiters to rowSeparator
set joinedRows to outputRows as text
set AppleScript's text item delimiters to ""
return joinedRows
'''

        raw_output = self.run_osascript(script)
        if not raw_output:
            return []

        events: list[CalendarEvent] = []
        for row in raw_output.split(chr(30)):
            if not row.strip():
                continue
            parts = row.split(chr(31))
            if len(parts) != 7:
                continue
            calendar_name, summary, start_text, end_text, attendees_text, description_text, event_url = parts
            summary = summary.strip()
            if not summary:
                continue
            attendees = [
                item.strip()
                for item in attendees_text.split(chr(29))
                if item.strip()
            ]
            description = description_text.strip() or None
            links = self.extract_invite_links(description, event_url.strip())
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
            normalized_start, normalized_end = self.normalize_event_window(start, end, window_start, window_end)
            events.append(
                CalendarEvent(
                    calendar_name=calendar_name.strip(),
                    summary=summary,
                    start=normalized_start,
                    end=normalized_end,
                    description=description,
                    links=links,
                    attendees=attendees,
                )
            )

        deduped_events = self.dedupe_sort_events(events)
        LOGGER.info("Fetched %s candidate Calendar events from %s via AppleScript", len(deduped_events), ", ".join(CALENDAR_NAMES))
        return deduped_events

    @staticmethod
    def dedupe_sort_events(events: list[CalendarEvent]) -> list[CalendarEvent]:
        deduped_events = list(
            {
                (
                    event.calendar_name,
                    event.summary,
                    event.start,
                    event.end,
                    event.description,
                    tuple((link["label"], link["url"]) for link in event.links),
                    tuple(event.attendees),
                ): event
                for event in events
            }.values()
        )
        deduped_events.sort(key=lambda event: (event.start, event.calendar_name, event.summary.lower()))
        return deduped_events

    def match_events(self, events: list[CalendarEvent], recording_start: datetime, recording_end: datetime) -> list[CalendarEvent]:
        recording_duration = max(recording_end - recording_start, timedelta())
        non_low_signal = [event for event in events if not self.is_low_signal_event(event)]
        filtered_events = non_low_signal or events
        starts_soon = [
            event
            for event in filtered_events
            if recording_start < event.start <= recording_start + CALENDAR_EARLY_START_GRACE
        ]
        started_or_ongoing = [
            event
            for event in filtered_events
            if event.start <= recording_start <= event.end + CALENDAR_LATE_START_GRACE
        ]
        # Prefer meetings that are about to start over a prior meeting still in progress.
        start_containing = starts_soon or started_or_ongoing
        if start_containing:
            reasonable_containing = [
                event for event in start_containing if (event.end - event.start) <= CALENDAR_MAX_REASONABLE_DURATION
            ]
            selected = reasonable_containing or start_containing
            best_containing = self.pick_best_events(selected, recording_start, recording_duration)
            if best_containing and not self.is_weak_match(best_containing[0]):
                LOGGER.info("Found %s events containing recording start", len(best_containing))
                return best_containing

            alternative_window = timedelta(hours=4)
            alternatives = [
                event
                for event in filtered_events
                if not self.is_weak_match(event)
                and abs(event.start - recording_start) <= alternative_window
            ]
            if alternatives:
                vanta_alternatives = [
                    event
                    for event in alternatives
                    if self.calendar_source_label(event.calendar_name) == "Vanta"
                ]
                prioritized = vanta_alternatives or alternatives
                best_alternatives = self.pick_best_events(prioritized, recording_start, recording_duration)
                LOGGER.info(
                    "Switched from weak containing event to %s nearby alternatives",
                    len(best_alternatives),
                )
                return best_alternatives

            LOGGER.info("Found %s weak containing events and no better alternatives", len(best_containing))
            return best_containing

        nearby = [
            event
            for event in filtered_events
            if abs(event.start - recording_start) <= CALENDAR_NEAREST_START_MAX_DELTA
        ]
        if not nearby:
            extended_nearby = [
                event
                for event in filtered_events
                if abs(event.start - recording_start) <= timedelta(hours=4)
            ]
            if not extended_nearby:
                LOGGER.info("Found 0 nearby calendar events for recording start")
                return []
            vanta_extended = [
                event
                for event in extended_nearby
                if self.calendar_source_label(event.calendar_name) == "Vanta"
            ]
            prioritized_extended = vanta_extended or extended_nearby
            resolved_extended = self.pick_best_events(prioritized_extended, recording_start, recording_duration)
            LOGGER.info(
                "Found %s extended-nearby calendar events for recording start",
                len(resolved_extended),
            )
            return resolved_extended

        reasonable_nearby = [event for event in nearby if (event.end - event.start) <= CALENDAR_MAX_REASONABLE_DURATION]
        candidate_events = reasonable_nearby or nearby
        resolved = self.pick_best_events(candidate_events, recording_start, recording_duration)
        LOGGER.info(
            "Found %s nearby calendar events after start-time and duration tie-breaks",
            len(resolved),
        )
        return resolved

    def pick_best_events(
        self,
        events: list[CalendarEvent],
        recording_start: datetime,
        recording_duration: timedelta,
    ) -> list[CalendarEvent]:
        if not events:
            return []

        start_deltas = [(event, abs(event.start - recording_start)) for event in events]
        best_start_delta = min(delta for _, delta in start_deltas)
        closest_start_events = [
            event for event, delta in start_deltas if delta <= best_start_delta + CALENDAR_START_TIE_WINDOW
        ]
        if len(closest_start_events) == 1:
            return closest_start_events

        duration_deltas = [
            (event, abs((event.end - event.start) - recording_duration))
            for event in closest_start_events
        ]
        best_duration_delta = min(delta for _, delta in duration_deltas)
        duration_tied_events = [
            event for event, delta in duration_deltas if delta == best_duration_delta
        ]
        if len(duration_tied_events) == 1:
            return duration_tied_events

        vanta_events = [
            event
            for event in duration_tied_events
            if self.calendar_source_label(event.calendar_name) == "Vanta"
        ]
        return vanta_events or duration_tied_events

    @staticmethod
    def is_low_signal_event(event: CalendarEvent) -> bool:
        normalized = event.summary.lower()
        return any(marker in normalized for marker in CalendarEventService._LOW_SIGNAL_TITLE_MARKERS)

    @staticmethod
    def is_weak_match(event: CalendarEvent) -> bool:
        duration = event.end - event.start
        return duration > CALENDAR_MAX_REASONABLE_DURATION or CalendarEventService.is_low_signal_event(event)

    @staticmethod
    def normalize_event_window(
        start: datetime,
        end: datetime,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[datetime, datetime]:
        # Calendar AppleScript can return recurring-series master timestamps; shift by common periods
        # so matching uses the occurrence nearest to the query window.
        if end >= window_start and start <= window_end:
            return start, end

        window_mid = window_start + (window_end - window_start) / 2
        best_start = start
        best_end = end
        best_distance = abs(start - window_mid)
        for period_days in (7, 14, 1):
            period_seconds = period_days * 86400
            shift_guess = round((window_mid - start).total_seconds() / period_seconds)
            for shift in (shift_guess - 1, shift_guess, shift_guess + 1):
                if shift == 0:
                    continue
                shifted_start = start + timedelta(days=period_days * shift)
                shifted_end = end + timedelta(days=period_days * shift)
                if shifted_end < window_start - timedelta(hours=12):
                    continue
                if shifted_start > window_end + timedelta(hours=12):
                    continue
                distance = abs(shifted_start - window_mid)
                if distance < best_distance:
                    best_start = shifted_start
                    best_end = shifted_end
                    best_distance = distance
        return best_start, best_end

    @staticmethod
    def build_applescript_date(variable_name: str, value: datetime) -> str:
        return "\n".join(
            [
                f"set {variable_name} to current date",
                f"set year of {variable_name} to {value.year}",
                f"set month of {variable_name} to {value.month}",
                f"set day of {variable_name} to {value.day}",
                f"set hours of {variable_name} to {value.hour}",
                f"set minutes of {variable_name} to {value.minute}",
                f"set seconds of {variable_name} to {value.second}",
            ]
        )

    @staticmethod
    def run_osascript(script: str) -> str:
        try:
            result = subprocess.check_output(["osascript", "-e", script], text=True, stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise RuntimeError("osascript is required to read macOS Calendar events") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Calendar query failed: {exc.output.strip()}") from exc
        return result.strip()

    @staticmethod
    def calendar_source_label(calendar_name: str) -> str:
        normalized = calendar_name.strip().lower()
        if normalized == "vanta":
            return "Vanta"
        if normalized in {"个人", "personal"}:
            return "Personal"
        return calendar_name.strip() or DEFAULT_POST_SOURCE

    @staticmethod
    def extract_invite_links(description: str | None, event_url: str | None) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[str] = set()

        def add_link(label: str, url: str) -> None:
            cleaned = url.strip()
            if not cleaned or cleaned in seen:
                return
            if not CalendarEventService.is_useful_attachment_link(cleaned):
                return
            seen.add(cleaned)
            links.append({"label": label, "url": cleaned})

        if event_url:
            add_link("Event URL", event_url)
        if description:
            link_index = 1
            for match in re.finditer(r"https?://[^\s)\]>\"']+", description):
                before_count = len(links)
                add_link(f"Link {link_index}", match.group(0))
                if len(links) > before_count:
                    link_index += 1
        return links

    @staticmethod
    def is_useful_attachment_link(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False

        host = (parsed.netloc or "").lower()
        host = host[4:] if host.startswith("www.") else host
        path = (parsed.path or "").lower()

        if host in CalendarEventService._TRIVIAL_LINK_HOSTS:
            return False
        if host == "calendar.google.com" and "/event" in path:
            return False

        if host.endswith(CalendarEventService._USEFUL_LINK_HOST_MARKERS):
            return True

        if path.endswith(CalendarEventService._USEFUL_FILE_SUFFIXES):
            return True

        for marker in CalendarEventService._USEFUL_PATH_MARKERS:
            if marker in path:
                return True

        return False

class FileNamingService:
    _RECORDING_DATETIME_PATTERN = re.compile(
        r"(?P<datetime>\d{4}-\d{2}-\d{2}(?:\s+at)?\s+\d{1,2}[-:]\d{2}(?:[-:]\d{2})?)"
    )

    @staticmethod
    def strip_transcript_suffixes(title: str) -> str:
        cleaned = re.sub(
            r"(\.(mp4|webm|mov))?\.smart(?:\.diarization)?\.jsonl$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip(" -_.")
        return cleaned

    @staticmethod
    def derive_recording_metadata(input_path: Path) -> RecordingMetadata:
        normalized_name = input_path.name.replace("\u202f", " ").replace("\xa0", " ")
        match = re.search(
            r"(?P<date>\d{4}-\d{2}-\d{2})(?:\s+at)?\s+"
            r"(?P<hour>\d{1,2})[.\-:](?P<minute>\d{2})[.\-:](?P<second>\d{2})"
            r"(?:\s*(?P<ampm>AM|PM))?",
            normalized_name,
            re.IGNORECASE,
        )

        if match:
            hour = int(match.group("hour"))
            ampm = match.group("ampm")
            if ampm:
                upper_ampm = ampm.upper()
                if upper_ampm == "PM" and hour != 12:
                    hour += 12
                if upper_ampm == "AM" and hour == 12:
                    hour = 0
            started_at = datetime(
                year=int(match.group("date")[0:4]),
                month=int(match.group("date")[5:7]),
                day=int(match.group("date")[8:10]),
                hour=hour,
                minute=int(match.group("minute")),
                second=int(match.group("second")),
            )
            title_source = normalized_name[: match.start()].strip(" -_.")
        else:
            date_only = re.search(r"(?P<date>\d{4}-\d{2}-\d{2})", normalized_name)
            if date_only:
                started_at = datetime(
                    year=int(date_only.group("date")[0:4]),
                    month=int(date_only.group("date")[5:7]),
                    day=int(date_only.group("date")[8:10]),
                )
                title_source = normalized_name[: date_only.start()].strip(" -_.")
            else:
                started_at = datetime.fromtimestamp(input_path.stat().st_mtime)
                title_source = normalized_name

        title_hint = FileNamingService.strip_transcript_suffixes(title_source)
        if not title_hint:
            title_hint = FileNamingService.strip_transcript_suffixes(input_path.stem) or input_path.stem
        return RecordingMetadata(started_at=started_at, title_hint=title_hint)

    @staticmethod
    def default_output_path(input_path: Path, post_title: str | None = None) -> Path:
        metadata = FileNamingService.derive_recording_metadata(input_path)
        title = post_title or metadata.title_hint
        return PRIVATE_POSTS_DIR / f"{metadata.started_at:%Y-%m-%d}-{TextNormalizer.slugify(title)}.md"

    @staticmethod
    def default_log_path(input_path: Path) -> Path:
        metadata = FileNamingService.derive_recording_metadata(input_path)
        return LOGS_DIR / f"{metadata.started_at:%Y-%m-%d}-{TextNormalizer.slugify(metadata.title_hint)}.log"

    @staticmethod
    def transcript_suffix_chain(path: Path) -> str:
        match = re.search(r"(\.(?:mp4|webm|mov))?\.smart\.diarization\.jsonl$", path.name, re.IGNORECASE)
        if match:
            return match.group(0)
        return "".join(path.suffixes)

    @staticmethod
    def source_media_path_for_transcript(path: Path) -> Path | None:
        suffixes = (".smart.diarization.jsonl", ".smart.jsonl")
        lower_name = path.name.lower()
        for transcript_suffix in suffixes:
            if not lower_name.endswith(transcript_suffix):
                continue
            media_name = path.name[: -len(transcript_suffix)]
            media_path = path.with_name(media_name)
            if media_path.exists():
                return media_path
        return None

    @staticmethod
    def choose_recording_rename_targets(input_path: Path, post_title: str) -> dict[Path, Path]:
        sanitized_title = TextNormalizer.sanitize_filename_title(post_title)
        transcript_suffix = FileNamingService.transcript_suffix_chain(input_path)
        media_path = FileNamingService.source_media_path_for_transcript(input_path)
        media_suffix = "".join(media_path.suffixes) if media_path else None
        datetime_suffix = FileNamingService.recording_datetime_suffix(input_path, media_path)
        base_title = sanitized_title
        if datetime_suffix and datetime_suffix.lower() not in sanitized_title.lower():
            base_title = f"{sanitized_title} - {datetime_suffix}"
        existing_paths = {path.resolve() for path in [input_path, media_path] if path is not None and path.exists()}

        for attempt in range(1, 101):
            suffix = "" if attempt == 1 else f" ({attempt})"
            candidate_base = f"{base_title}{suffix}"
            transcript_target = input_path.with_name(f"{candidate_base}{transcript_suffix}")
            rename_targets: dict[Path, Path] = {input_path: transcript_target}
            if media_path and media_suffix:
                rename_targets[media_path] = media_path.with_name(f"{candidate_base}{media_suffix}")

            conflict = False
            for current_path, target_path in rename_targets.items():
                if target_path == current_path:
                    continue
                if target_path.exists() and target_path.resolve() not in existing_paths:
                    conflict = True
                    break
            if not conflict:
                return rename_targets

        raise RuntimeError(f"Could not find available recording rename target for {input_path}")

    @staticmethod
    def recording_datetime_suffix(input_path: Path, media_path: Path | None = None) -> str:
        candidates: list[str] = []
        if media_path is not None:
            candidates.append(media_path.stem)

        transcript_name = input_path.name
        lower_name = transcript_name.lower()
        for transcript_suffix in (".smart.diarization.jsonl", ".smart.jsonl"):
            if lower_name.endswith(transcript_suffix):
                transcript_name = transcript_name[: -len(transcript_suffix)]
                break
        transcript_stem = Path(transcript_name).stem if "." in transcript_name else transcript_name
        candidates.append(transcript_stem)

        for candidate in candidates:
            match = FileNamingService._RECORDING_DATETIME_PATTERN.search(candidate)
            if match:
                return match.group("datetime")
        return ""

    @staticmethod
    def rename_recording_files(input_path: Path, post_title: str) -> Path:
        rename_targets = FileNamingService.choose_recording_rename_targets(input_path, post_title)
        ordered_paths = sorted(rename_targets.items(), key=lambda item: len(item[0].suffixes), reverse=True)
        renamed_transcript_path = input_path
        for current_path, target_path in ordered_paths:
            if current_path == target_path:
                continue
            current_path.rename(target_path)
            LOGGER.info("Renamed %s -> %s", current_path, target_path)
            if current_path == input_path:
                renamed_transcript_path = target_path
        return renamed_transcript_path


class PostComposer:
    @staticmethod
    def compose(input_path: Path, result: SummaryResult) -> str:
        metadata = FileNamingService.derive_recording_metadata(input_path)
        front_matter = [
            "---",
            "layout: post",
            f'title: {json.dumps(result.post_title)}',
            f'date: "{metadata.started_at:%Y-%m-%d %H:%M:%S}"',
            f'source: {json.dumps(result.post_source)}',
            f'video_url: {json.dumps(result.video_url)}',
            f"meeting: {json.dumps(result.meeting_metadata, ensure_ascii=False)}",
            "comments: false",
            "raw_llm_input: |",
            TranscriptRenderer.indent_block(result.rendered_transcript),
            "---",
            "",
        ]
        return "\n".join(front_matter) + result.summary_markdown.rstrip() + "\n"


class TranscriptSummarizer:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        retries: int = DEFAULT_LLM_RETRIES,
    ) -> None:
        self.llm_client = LLMClient(
            model=model,
            fallback_model=fallback_model,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
        self.speaker_names = SpeakerNameService(self.llm_client)
        self.calendar = CalendarEventService(self.llm_client)

    def summarize_document(self, transcript_path: str | Path) -> SummaryResult:
        document = TranscriptIO.load(transcript_path)
        if not document.segments:
            raise ValueError(f"No transcript text found in {document.path}")

        unlabeled_transcript = TranscriptRenderer.render(document.segments)
        meeting_context = self.calendar.build_meeting_context(document)
        speaker_name_map, summary_markdown, selected_event_index = self.speaker_names.summarize(
            transcript_text=unlabeled_transcript,
            speaker_ids=document.speaker_ids,
            source_name=document.source_name,
            meeting_context=meeting_context,
        )
        if selected_event_index == 0 and len(meeting_context.candidate_events) == 1:
            selected_event_index = 1
        rendered_transcript = TranscriptRenderer.render(document.segments, speaker_name_map)
        post_context = self.select_post_context(meeting_context, selected_event_index)
        selected_event = self.select_event(meeting_context, selected_event_index)
        finalized_meeting_metadata = {
            "description": selected_event.description if selected_event else None,
            "links": selected_event.links if selected_event else [],
            "attendees": selected_event.attendees if selected_event else [],
        }
        LOGGER.info("Rendered transcript with speaker names")
        return SummaryResult(
            speaker_name_map=speaker_name_map,
            rendered_transcript=rendered_transcript,
            summary_markdown=summary_markdown,
            post_title=post_context.title,
            post_source=post_context.source,
            meeting_metadata=finalized_meeting_metadata,
            video_url=self.build_drive_search_url(post_context.title),
        )

    def resolve_speaker_names(
        self,
        transcript_text: str,
        speaker_ids: list[str],
        source_name: str,
        meeting_context: MeetingContext | None = None,
    ) -> tuple[dict[str, str], str, int]:
        meeting_context = meeting_context or MeetingContext(
            fallback_title="",
            fallback_source=DEFAULT_POST_SOURCE,
            candidate_events=[],
            recording_started_at=datetime.min,
            recording_ended_at=datetime.min,
        )
        return self.speaker_names.summarize(transcript_text, speaker_ids, source_name, meeting_context)

    def correct_speaker_name(self, name: str) -> str:
        return self.speaker_names.correct_name(name)

    def resolve_post_context(self, transcript_path: str | Path, rendered_transcript: str | None = None) -> PostContext:
        document = TranscriptIO.load(transcript_path)
        _ = rendered_transcript or TranscriptRenderer.render(document.segments)
        meeting_context = self.calendar.build_meeting_context(document)
        return self.select_post_context(meeting_context, 1 if len(meeting_context.candidate_events) == 1 else 0)

    def rename_recording_files(self, transcript_path: str | Path, post_title: str) -> Path:
        return FileNamingService.rename_recording_files(Path(transcript_path).expanduser(), post_title)

    def select_post_context(self, meeting_context: MeetingContext, selected_event_index: int) -> PostContext:
        if 1 <= selected_event_index <= len(meeting_context.candidate_events):
            event = meeting_context.candidate_events[selected_event_index - 1]
            return PostContext(
                title=event.summary,
                source=CalendarEventService.calendar_source_label(event.calendar_name),
            )
        if len(meeting_context.candidate_events) == 1:
            event = meeting_context.candidate_events[0]
            return PostContext(
                title=event.summary,
                source=CalendarEventService.calendar_source_label(event.calendar_name),
            )
        return PostContext(
            title=meeting_context.fallback_title,
            source=meeting_context.fallback_source,
        )

    @staticmethod
    def select_event(meeting_context: MeetingContext, selected_event_index: int) -> CalendarEvent | None:
        if 1 <= selected_event_index <= len(meeting_context.candidate_events):
            return meeting_context.candidate_events[selected_event_index - 1]
        if len(meeting_context.candidate_events) == 1:
            return meeting_context.candidate_events[0]
        return None

    @staticmethod
    def build_drive_search_url(post_title: str) -> str:
        return f"https://drive.google.com/drive/search?q={quote(post_title, safe='')}"
