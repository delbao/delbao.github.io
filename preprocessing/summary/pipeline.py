from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    DEFAULT_POST_SOURCE,
    FUZZY_NAME_THRESHOLD,
    LOGGER,
    LOGS_DIR,
    PHONETIC_FUZZY_NAME_THRESHOLD,
    PRIVATE_POSTS_DIR,
    load_prompt,
    load_speaker_name_corrections,
)
from common.llm import LLMClient
from .models import CalendarEvent, PostContext, RecordingMetadata, SummaryResult, TranscriptDocument, TranscriptSegment
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

    def resolve_names(self, transcript_text: str, speaker_ids: list[str], source_name: str) -> tuple[dict[str, str], str]:
        speaker_list = ", ".join(speaker_ids)
        LOGGER.info("Preparing one-pass summary for %s speaker IDs", len(speaker_ids))
        system_prompt = load_prompt("meeting_summary_system.txt")
        user_prompt = load_prompt("meeting_summary_user.txt").format(
            source_name=source_name,
            speaker_list=speaker_list,
            transcript_text=transcript_text,
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
        return resolved_map, self.postprocess_summary(summary_markdown.strip(), resolved_map)

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
    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def resolve_post_context(self, document: TranscriptDocument, transcript_text: str) -> PostContext:
        metadata = FileNamingService.derive_recording_metadata(document.path)
        if metadata.title_hint != document.path.stem:
            LOGGER.info("Using filename-derived title %s", metadata.title_hint)
            return PostContext(title=metadata.title_hint, source=DEFAULT_POST_SOURCE)

        recording_start = metadata.started_at
        recording_end = recording_start + timedelta(seconds=max(document.duration_seconds, 0.0))
        query_start = recording_start - CALENDAR_QUERY_PADDING
        query_end = recording_end + CALENDAR_QUERY_PADDING
        LOGGER.info("Resolving title from Calendar for recording window %s to %s", recording_start, recording_end)

        try:
            calendar_events = self.fetch_events(query_start, query_end)
        except RuntimeError as exc:
            LOGGER.warning("Calendar lookup failed; falling back to filename title: %s", exc)
            return PostContext(title=metadata.title_hint, source=DEFAULT_POST_SOURCE)

        matched_events = self.match_events(calendar_events, recording_start, recording_end)
        if len(matched_events) == 1:
            matched_event = matched_events[0]
            LOGGER.info("Resolved title from single calendar event: %s", matched_event.summary)
            return PostContext(
                title=matched_event.summary,
                source=self.calendar_source_label(matched_event.calendar_name),
            )

        chosen_event = self.choose_event_with_llm(transcript_text, matched_events)
        if chosen_event:
            return PostContext(
                title=chosen_event.summary,
                source=self.calendar_source_label(chosen_event.calendar_name),
            )

        LOGGER.info("No calendar title match found; falling back to filename title")
        return PostContext(title=metadata.title_hint, source=DEFAULT_POST_SOURCE)

    def fetch_events(self, window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
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
          set rowText to (name of theCal as text) & fieldSeparator & summaryText & fieldSeparator & my isoStringForDate(start date of e) & fieldSeparator & my isoStringForDate(endDateValue)
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
            if len(parts) != 4:
                continue
            calendar_name, summary, start_text, end_text = parts
            summary = summary.strip()
            if not summary:
                continue
            events.append(
                CalendarEvent(
                    calendar_name=calendar_name.strip(),
                    summary=summary,
                    start=datetime.fromisoformat(start_text),
                    end=datetime.fromisoformat(end_text),
                )
            )

        deduped_events = list(
            {
                (event.calendar_name, event.summary, event.start, event.end): event
                for event in events
            }.values()
        )
        deduped_events.sort(key=lambda event: (event.start, event.calendar_name, event.summary.lower()))
        LOGGER.info("Fetched %s candidate Calendar events from %s", len(deduped_events), ", ".join(CALENDAR_NAMES))
        return deduped_events

    def match_events(self, events: list[CalendarEvent], recording_start: datetime, recording_end: datetime) -> list[CalendarEvent]:
        recording_duration = max(recording_end - recording_start, timedelta())
        start_containing = [
            event
            for event in events
            if event.start - CALENDAR_EARLY_START_GRACE <= recording_start <= event.end + CALENDAR_LATE_START_GRACE
        ]
        if start_containing:
            reasonable_containing = [
                event for event in start_containing if (event.end - event.start) <= CALENDAR_MAX_REASONABLE_DURATION
            ]
            selected = reasonable_containing or start_containing
            LOGGER.info("Found %s events containing recording start", len(selected))
            return selected

        nearby = [
            event
            for event in events
            if abs(event.start - recording_start) <= CALENDAR_NEAREST_START_MAX_DELTA
        ]
        if not nearby:
            LOGGER.info("Found 0 nearby calendar events for recording start")
            return []

        reasonable_nearby = [event for event in nearby if (event.end - event.start) <= CALENDAR_MAX_REASONABLE_DURATION]
        candidate_events = reasonable_nearby or nearby
        start_deltas = {event: abs(event.start - recording_start) for event in candidate_events}
        best_start_delta = min(start_deltas.values())
        closest_start_events = [
            event for event in candidate_events if start_deltas[event] <= best_start_delta + CALENDAR_START_TIE_WINDOW
        ]
        if len(closest_start_events) == 1:
            LOGGER.info("Resolved single closest-start calendar event")
            return closest_start_events

        duration_deltas = {
            event: abs((event.end - event.start) - recording_duration)
            for event in closest_start_events
        }
        best_duration_delta = min(duration_deltas.values())
        duration_tied_events = [
            event for event in closest_start_events if duration_deltas[event] == best_duration_delta
        ]
        LOGGER.info(
            "Found %s nearby calendar events after start-time and duration tie-breaks",
            len(duration_tied_events),
        )
        return duration_tied_events

    def choose_event_with_llm(self, transcript_text: str, candidate_events: list[CalendarEvent]) -> CalendarEvent | None:
        if len(candidate_events) <= 1:
            return candidate_events[0] if candidate_events else None

        system_prompt = (
            "You resolve which calendar event title best matches a meeting transcript. "
            "Return only a JSON object with one top-level key: selected_index. "
            "Choose the 1-based candidate index that best matches the transcript context. "
            "If the transcript is too ambiguous, return 0."
        )
        user_prompt = (
            "Pick the best matching calendar event for this transcript.\n\n"
            f"Candidate events:\n{self.format_event_options(candidate_events)}\n\n"
            "Transcript excerpt:\n"
            f"{transcript_text[:CALENDAR_TRANSCRIPT_EXCERPT_CHARS]}"
        )
        parsed = self.llm_client.complete_json(system_prompt, user_prompt)
        selected_index = parsed.get("selected_index")
        if not isinstance(selected_index, int):
            raise ValueError("Expected integer selected_index from calendar disambiguation LLM")
        if 1 <= selected_index <= len(candidate_events):
            selected = candidate_events[selected_index - 1]
            LOGGER.info("LLM selected calendar event %s", selected.summary)
            return selected
        LOGGER.info("LLM did not select a calendar event")
        return None

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
        if calendar_name == "Vanta":
            return "Vanta"
        return "Personal"

    @staticmethod
    def format_event_options(events: list[CalendarEvent]) -> str:
        lines: list[str] = []
        for index, event in enumerate(events, start=1):
            lines.append(
                f"{index}. {event.summary} | {event.calendar_name} | "
                f"{event.start:%Y-%m-%d %H:%M} - {event.end:%H:%M}"
            )
        return "\n".join(lines)


class FileNamingService:
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

        title_hint = re.sub(
            r"(\.(mp4|webm|mov))?\.smart\.diarization\.jsonl$",
            "",
            title_source,
            flags=re.IGNORECASE,
        ).strip(" -_.")
        if not title_hint:
            title_hint = input_path.stem
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
        transcript_suffix = ".smart.diarization.jsonl"
        if not path.name.lower().endswith(transcript_suffix):
            return None
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
        existing_paths = {path.resolve() for path in [input_path, media_path] if path is not None and path.exists()}

        for attempt in range(1, 101):
            suffix = "" if attempt == 1 else f" ({attempt})"
            candidate_base = f"{sanitized_title}{suffix}"
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
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        retries: int = DEFAULT_LLM_RETRIES,
    ) -> None:
        self.llm_client = LLMClient(model=model, timeout_seconds=timeout_seconds, retries=retries)
        self.speaker_names = SpeakerNameService(self.llm_client)
        self.calendar = CalendarEventService(self.llm_client)

    def summarize_document(self, transcript_path: str | Path) -> SummaryResult:
        document = TranscriptIO.load(transcript_path)
        if not document.segments:
            raise ValueError(f"No transcript text found in {document.path}")

        unlabeled_transcript = TranscriptRenderer.render(document.segments)
        speaker_name_map, summary_markdown = self.speaker_names.resolve_names(
            transcript_text=unlabeled_transcript,
            speaker_ids=document.speaker_ids,
            source_name=document.source_name,
        )
        rendered_transcript = TranscriptRenderer.render(document.segments, speaker_name_map)
        post_context = self.calendar.resolve_post_context(document, rendered_transcript)
        LOGGER.info("Rendered transcript with speaker names")
        return SummaryResult(
            speaker_name_map=speaker_name_map,
            rendered_transcript=rendered_transcript,
            summary_markdown=summary_markdown,
            post_title=post_context.title,
            post_source=post_context.source,
        )

    def resolve_speaker_names(self, transcript_text: str, speaker_ids: list[str], source_name: str) -> tuple[dict[str, str], str]:
        return self.speaker_names.resolve_names(transcript_text, speaker_ids, source_name)

    def correct_speaker_name(self, name: str) -> str:
        return self.speaker_names.correct_name(name)

    def resolve_post_context(self, transcript_path: str | Path, rendered_transcript: str | None = None) -> PostContext:
        document = TranscriptIO.load(transcript_path)
        transcript_text = rendered_transcript or TranscriptRenderer.render(document.segments)
        return self.calendar.resolve_post_context(document, transcript_text)

    def rename_recording_files(self, transcript_path: str | Path, post_title: str) -> Path:
        return FileNamingService.rename_recording_files(Path(transcript_path).expanduser(), post_title)
