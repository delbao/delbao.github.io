#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import suppress
import difflib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable


DEFAULT_MODEL = os.environ.get("TRANSCRIPT_SUMMARY_MODEL", "gpt-4.1-mini")
DEFAULT_LLM_TIMEOUT_SECONDS = float(os.environ.get("TRANSCRIPT_SUMMARY_TIMEOUT_SECONDS", "90"))
DEFAULT_LLM_RETRIES = int(os.environ.get("TRANSCRIPT_SUMMARY_RETRIES", "2"))
FUZZY_NAME_THRESHOLD = 0.8
PHONETIC_FUZZY_NAME_THRESHOLD = 0.55
PREPROCESSING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PREPROCESSING_DIR.parent
PROMPTS_DIR = PREPROCESSING_DIR / "prompts"
SPEAKER_NAME_CORRECTIONS_PATH = PREPROCESSING_DIR / "speaker_name_corrections.json"
PRIVATE_POSTS_DIR = REPO_ROOT / "_private_posts"
LOGS_DIR = PREPROCESSING_DIR / "logs"
CALENDAR_NAMES = ("Vanta", "个人")
CALENDAR_QUERY_PADDING = timedelta(minutes=20)
CALENDAR_NEAREST_START_MAX_DELTA = timedelta(minutes=90)
CALENDAR_START_MATCH_WINDOW = timedelta(minutes=45)
CALENDAR_EARLY_START_GRACE = timedelta(minutes=5)
CALENDAR_LATE_START_GRACE = timedelta(seconds=30)
CALENDAR_START_TIE_WINDOW = timedelta(minutes=3)
CALENDAR_MAX_REASONABLE_DURATION = timedelta(hours=4)
CALENDAR_TRANSCRIPT_EXCERPT_CHARS = 8000
DEFAULT_POST_SOURCE = "Personal"


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


@dataclass(frozen=True)
class CalendarEvent:
    calendar_name: str
    summary: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PostContext:
    title: str
    source: str


LOGGER = logging.getLogger("summarize_transcript")


def load_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def setup_logging(log_path: Path) -> None:
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


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return load_text_file(PROMPTS_DIR / name)


@lru_cache(maxsize=1)
def load_speaker_name_corrections() -> dict[str, str]:
    raw = json.loads(load_text_file(SPEAKER_NAME_CORRECTIONS_PATH))
    if not isinstance(raw, dict):
        raise ValueError("speaker_name_corrections.json must be a JSON object")

    corrections: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("speaker_name_corrections.json keys and values must be strings")
        normalized_key = normalize_name_key(key)
        normalized_value = value.strip()
        if normalized_key and normalized_value:
            corrections[normalized_key] = normalized_value
    return corrections


def load_transcript_segments(path: Path) -> list[TranscriptSegment]:
    LOGGER.info("Loading transcript segments from %s", path)
    segments: list[TranscriptSegment] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {path}") from exc

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
    return segments


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def normalize_name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def soundex(text: str) -> str:
    normalized = normalize_name_key(text)
    if not normalized:
        return ""

    first_letter = normalized[0].upper()
    mappings = {
        "b": "1",
        "f": "1",
        "p": "1",
        "v": "1",
        "c": "2",
        "g": "2",
        "j": "2",
        "k": "2",
        "q": "2",
        "s": "2",
        "x": "2",
        "z": "2",
        "d": "3",
        "t": "3",
        "l": "4",
        "m": "5",
        "n": "5",
        "r": "6",
    }

    digits: list[str] = []
    last_digit = mappings.get(normalized[0], "")
    for char in normalized[1:]:
        digit = mappings.get(char, "")
        if digit != last_digit:
            if digit:
                digits.append(digit)
            last_digit = digit

    return (first_letter + "".join(digits) + "000")[:4]


def seconds_to_hhmmss(value: float) -> str:
    total_seconds = max(int(value), 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_time_range(start: float, end: float) -> str:
    return f"{seconds_to_hhmmss(start)}-{seconds_to_hhmmss(end)}"


def render_transcript(segments: Iterable[TranscriptSegment]) -> str:
    return render_transcript_with_speaker_names(segments, {})


def render_transcript_with_speaker_names(
    segments: Iterable[TranscriptSegment],
    speaker_name_map: dict[str, str],
) -> str:
    lines: list[str] = []
    current_turn: RenderedTurn | None = None

    for segment in segments:
        text = normalize_text(segment.text)
        if not text:
            continue

        speaker = speaker_name_map.get(
            segment.dominant_speaker or "UNKNOWN",
            segment.dominant_speaker or "UNKNOWN",
        )
        if current_turn and speaker == current_turn.speaker:
            current_turn.parts.append(text)
            current_turn.end = segment.end
            continue

        if current_turn:
            lines.append(
                f"[{format_time_range(current_turn.start, current_turn.end)}] "
                f"{current_turn.speaker}: {' '.join(current_turn.parts)}"
            )

        current_turn = RenderedTurn(
            speaker=speaker,
            start=segment.start,
            end=segment.end,
            parts=[text],
        )

    if current_turn:
        lines.append(
            f"[{format_time_range(current_turn.start, current_turn.end)}] "
            f"{current_turn.speaker}: {' '.join(current_turn.parts)}"
        )

    return "\n".join(lines)


def call_llm(model: str, system_prompt: str, user_prompt: str) -> str:
    LOGGER.info(
        "Calling LLM model=%s system_chars=%s user_chars=%s timeout=%ss retries=%s",
        model,
        len(system_prompt),
        len(user_prompt),
        DEFAULT_LLM_TIMEOUT_SECONDS,
        DEFAULT_LLM_RETRIES,
    )
    try:
        from litellm import completion
    except ImportError as exc:
        raise RuntimeError(
            "litellm is not installed. Create .venv and run "
            "`pip install -r preprocessing/requirements.txt`."
        ) from exc

    last_error: Exception | None = None
    for attempt in range(1, DEFAULT_LLM_RETRIES + 2):
        try:
            LOGGER.info("LLM request attempt %s/%s", attempt, DEFAULT_LLM_RETRIES + 1)
            response = completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                timeout=DEFAULT_LLM_TIMEOUT_SECONDS,
                response_format={"type": "json_object"},
            )
            LOGGER.info("Received LLM response from model=%s", model)
            break
        except Exception as exc:
            last_error = exc
            LOGGER.warning("LLM request attempt %s failed: %s", attempt, exc)
            if attempt > DEFAULT_LLM_RETRIES:
                raise
            sleep_seconds = min(2 ** (attempt - 1), 8)
            LOGGER.info("Retrying LLM request in %ss", sleep_seconds)
            time.sleep(sleep_seconds)
    else:
        assert last_error is not None
        raise last_error

    message = response.choices[0].message
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")).strip())
        return "\n".join(part for part in parts if part).strip()
    raise ValueError("LiteLLM response did not contain text content")


def extract_json_object(text: str) -> dict[str, object]:
    candidate = text.strip()
    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
    if fenced_match:
        candidate = fenced_match.group(1).strip()
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        with suppress(ImportError, ValueError, TypeError):
            from json_repair import repair_json

            repaired = repair_json(candidate, return_objects=True)
            if isinstance(repaired, dict):
                LOGGER.info("Recovered malformed LLM JSON response with json_repair")
                return repaired
        raise
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object from LLM response")
    return parsed


def correct_speaker_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or cleaned.startswith("SPEAKER_") or cleaned == "UNKNOWN":
        return cleaned

    normalized = normalize_name_key(cleaned)
    if not normalized:
        return cleaned

    corrections = load_speaker_name_corrections()
    if normalized in corrections:
        return corrections[normalized]

    best_name = cleaned
    best_score = 0.0
    best_soundex_match = False
    normalized_soundex = soundex(normalized)
    for candidate_key, replacement_name in corrections.items():
        score = difflib.SequenceMatcher(None, normalized, candidate_key).ratio()
        soundex_match = normalized_soundex and normalized_soundex == soundex(candidate_key)
        if score > best_score:
            best_score = score
            best_name = replacement_name
            best_soundex_match = bool(soundex_match)

    if best_score >= FUZZY_NAME_THRESHOLD:
        return best_name
    if best_soundex_match and best_score >= PHONETIC_FUZZY_NAME_THRESHOLD:
        return best_name

    return cleaned


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


def summarize_with_speaker_mapping(
    model: str,
    transcript_text: str,
    speaker_ids: list[str],
    source_name: str,
) -> tuple[dict[str, str], str]:
    speaker_list = ", ".join(speaker_ids)
    LOGGER.info("Preparing one-pass summary for %s speaker IDs", len(speaker_ids))
    system_prompt = load_prompt("meeting_summary_system.txt")
    user_prompt = load_prompt("meeting_summary_user.txt").format(
        source_name=source_name,
        speaker_list=speaker_list,
        transcript_text=transcript_text,
    )
    raw_response = ""
    parsed: dict[str, object] | None = None
    for attempt in range(1, DEFAULT_LLM_RETRIES + 2):
        raw_response = call_llm(model, system_prompt, user_prompt)
        try:
            parsed = extract_json_object(raw_response)
            LOGGER.info("Parsed structured LLM response")
            break
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "Failed to parse LLM JSON response on attempt %s/%s: %s",
                attempt,
                DEFAULT_LLM_RETRIES + 1,
                exc,
            )
            LOGGER.warning("Raw LLM response prefix: %r", raw_response[:1000])
            if attempt > DEFAULT_LLM_RETRIES:
                raise
            sleep_seconds = min(2 ** (attempt - 1), 8)
            LOGGER.info("Retrying full LLM call after parse failure in %ss", sleep_seconds)
            time.sleep(sleep_seconds)
    if parsed is None:
        raise ValueError("Failed to parse structured LLM response")

    raw_speaker_map = parsed.get("speaker_map", {})
    if not isinstance(raw_speaker_map, dict):
        raise ValueError("Expected 'speaker_map' to be a JSON object")

    result: dict[str, str] = {}
    for speaker_id in speaker_ids:
        value = raw_speaker_map.get(speaker_id, speaker_id)
        if not isinstance(value, str):
            result[speaker_id] = speaker_id
            continue
        guessed_name = correct_speaker_name(value)
        result[speaker_id] = guessed_name or speaker_id
    LOGGER.info("Resolved speaker map: %s", result)
    summary_markdown = parsed.get("summary_markdown", "")
    if not isinstance(summary_markdown, str) or not summary_markdown.strip():
        raise ValueError("Expected non-empty string 'summary_markdown' in LLM response")
    return result, postprocess_summary(summary_markdown.strip(), result)


def unique_speaker_ids(segments: Iterable[TranscriptSegment]) -> list[str]:
    speaker_ids: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        speaker_id = segment.dominant_speaker or "UNKNOWN"
        if speaker_id in seen:
            continue
        seen.add(speaker_id)
        speaker_ids.append(speaker_id)
    return speaker_ids


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


def run_osascript(script: str) -> str:
    try:
        result = subprocess.check_output(
            ["osascript", "-e", script],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("osascript is required to read macOS Calendar events") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Calendar query failed: {exc.output.strip()}") from exc
    return result.strip()


def fetch_calendar_events(window_start: datetime, window_end: datetime) -> list[CalendarEvent]:
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
{build_applescript_date("windowStart", window_start)}
{build_applescript_date("windowEnd", window_end)}
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

    raw_output = run_osascript(script)
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
        start = datetime.fromisoformat(start_text)
        end = datetime.fromisoformat(end_text)
        events.append(
            CalendarEvent(
                calendar_name=calendar_name.strip(),
                summary=summary,
                start=start,
                end=end,
            )
        )
    deduped_events = list(
        {
            (event.calendar_name, event.summary, event.start, event.end): event
            for event in events
        }.values()
    )
    deduped_events.sort(key=lambda event: (event.start, event.calendar_name, event.summary.lower()))
    LOGGER.info(
        "Fetched %s candidate Calendar events from %s",
        len(deduped_events),
        ", ".join(CALENDAR_NAMES),
    )
    return deduped_events


def select_time_matched_events(
    events: list[CalendarEvent],
    recording_start: datetime,
    recording_end: datetime,
) -> list[CalendarEvent]:
    recording_duration = max(recording_end - recording_start, timedelta())
    start_containing = [
        event
        for event in events
        if event.start - CALENDAR_EARLY_START_GRACE
        <= recording_start
        <= event.end + CALENDAR_LATE_START_GRACE
    ]
    if start_containing:
        reasonable_containing = [
            event
            for event in start_containing
            if (event.end - event.start) <= CALENDAR_MAX_REASONABLE_DURATION
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

    reasonable_nearby = [
        event for event in nearby if (event.end - event.start) <= CALENDAR_MAX_REASONABLE_DURATION
    ]
    candidate_events = reasonable_nearby or nearby

    start_deltas = {
        event: abs(event.start - recording_start)
        for event in candidate_events
    }
    best_start_delta = min(start_deltas.values())
    closest_start_events = [
        event
        for event in candidate_events
        if start_deltas[event] <= best_start_delta + CALENDAR_START_TIE_WINDOW
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
        event
        for event in closest_start_events
        if duration_deltas[event] == best_duration_delta
    ]
    LOGGER.info(
        "Found %s nearby calendar events after start-time and duration tie-breaks",
        len(duration_tied_events),
    )
    return duration_tied_events


def format_calendar_event_options(events: list[CalendarEvent]) -> str:
    lines: list[str] = []
    for index, event in enumerate(events, start=1):
        lines.append(
            f"{index}. {event.summary} | {event.calendar_name} | "
            f"{event.start:%Y-%m-%d %H:%M} - {event.end:%H:%M}"
        )
    return "\n".join(lines)


def calendar_source_label(calendar_name: str) -> str:
    if calendar_name == "Vanta":
        return "Vanta"
    return "Personal"


def choose_event_with_llm(
    model: str,
    transcript_text: str,
    candidate_events: list[CalendarEvent],
) -> CalendarEvent | None:
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
        f"Candidate events:\n{format_calendar_event_options(candidate_events)}\n\n"
        "Transcript excerpt:\n"
        f"{transcript_text[:CALENDAR_TRANSCRIPT_EXCERPT_CHARS]}"
    )
    parsed = extract_json_object(call_llm(model, system_prompt, user_prompt))
    selected_index = parsed.get("selected_index")
    if not isinstance(selected_index, int):
        raise ValueError("Expected integer selected_index from calendar disambiguation LLM")
    if 1 <= selected_index <= len(candidate_events):
        selected = candidate_events[selected_index - 1]
        LOGGER.info("LLM selected calendar event %s", selected.summary)
        return selected
    LOGGER.info("LLM did not select a calendar event")
    return None


def resolve_post_context(
    input_path: Path,
    segments: list[TranscriptSegment],
    transcript_text: str,
    model: str,
) -> PostContext:
    recording_start, derived_title = derive_post_metadata(input_path)
    if derived_title != input_path.stem:
        LOGGER.info("Using filename-derived title %s", derived_title)
        return PostContext(title=derived_title, source=DEFAULT_POST_SOURCE)

    recording_duration_seconds = max((segment.end for segment in segments), default=0.0)
    recording_end = recording_start + timedelta(seconds=max(recording_duration_seconds, 0.0))
    query_start = recording_start - CALENDAR_QUERY_PADDING
    query_end = recording_end + CALENDAR_QUERY_PADDING
    LOGGER.info(
        "Resolving title from Calendar for recording window %s to %s",
        recording_start,
        recording_end,
    )

    try:
        calendar_events = fetch_calendar_events(query_start, query_end)
    except RuntimeError as exc:
        LOGGER.warning("Calendar lookup failed; falling back to filename title: %s", exc)
        return PostContext(title=derived_title, source=DEFAULT_POST_SOURCE)

    matched_events = select_time_matched_events(calendar_events, recording_start, recording_end)
    if len(matched_events) == 1:
        matched_event = matched_events[0]
        LOGGER.info("Resolved title from single calendar event: %s", matched_event.summary)
        return PostContext(
            title=matched_event.summary,
            source=calendar_source_label(matched_event.calendar_name),
        )

    chosen_event = choose_event_with_llm(model, transcript_text, matched_events)
    if chosen_event:
        return PostContext(
            title=chosen_event.summary,
            source=calendar_source_label(chosen_event.calendar_name),
        )

    LOGGER.info("No calendar title match found; falling back to filename title")
    return PostContext(title=derived_title, source=DEFAULT_POST_SOURCE)


def summarize_meeting_transcript(
    transcript_path: str | Path,
    model: str = DEFAULT_MODEL,
) -> SummaryResult:
    path = Path(transcript_path).expanduser()
    segments = load_transcript_segments(path)
    if not segments:
        raise ValueError(f"No transcript text found in {path}")

    unlabeled_transcript_text = render_transcript(segments)
    speaker_name_map, summary_markdown = summarize_with_speaker_mapping(
        model=model,
        transcript_text=unlabeled_transcript_text,
        speaker_ids=unique_speaker_ids(segments),
        source_name=path.name,
    )
    rendered_transcript = render_transcript_with_speaker_names(segments, speaker_name_map)
    post_context = resolve_post_context(path, segments, rendered_transcript, model)
    LOGGER.info("Rendered transcript with speaker names")
    return SummaryResult(
        speaker_name_map=speaker_name_map,
        rendered_transcript=rendered_transcript,
        summary_markdown=summary_markdown,
        post_title=post_context.title,
        post_source=post_context.source,
    )


def default_output_path(input_path: Path, post_title: str | None = None) -> Path:
    date_value, title = derive_post_metadata(input_path)
    title = post_title or title
    return PRIVATE_POSTS_DIR / f"{date_value:%Y-%m-%d}-{slugify(title)}.md"


def default_log_path(input_path: Path) -> Path:
    date_value, title = derive_post_metadata(input_path)
    return LOGS_DIR / f"{date_value:%Y-%m-%d}-{slugify(title)}.log"


def derive_post_metadata(input_path: Path) -> tuple[datetime, str]:
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
        date_value = datetime(
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
            date_value = datetime(
                year=int(date_only.group("date")[0:4]),
                month=int(date_only.group("date")[5:7]),
                day=int(date_only.group("date")[8:10]),
            )
            title_source = normalized_name[: date_only.start()].strip(" -_.")
        else:
            date_value = datetime.fromtimestamp(input_path.stat().st_mtime)
            title_source = normalized_name

    title_source = re.sub(
        r"(\.(mp4|webm|mov))?\.smart\.diarization\.jsonl$",
        "",
        title_source,
        flags=re.IGNORECASE,
    ).strip(" -_.")
    if not title_source:
        title_source = input_path.stem

    return date_value, title_source


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "meeting-summary"


def sanitize_filename_title(text: str) -> str:
    cleaned = re.sub(r'[\\/:\0]+', " ", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned or "Meeting"


def transcript_suffix_chain(path: Path) -> str:
    match = re.search(r"(\.(?:mp4|webm|mov))?\.smart\.diarization\.jsonl$", path.name, re.IGNORECASE)
    if match:
        return match.group(0)
    return "".join(path.suffixes)


def source_media_path_for_transcript(path: Path) -> Path | None:
    transcript_suffix = ".smart.diarization.jsonl"
    if not path.name.lower().endswith(transcript_suffix):
        return None
    media_name = path.name[: -len(transcript_suffix)]
    media_path = path.with_name(media_name)
    if media_path.exists():
        return media_path
    return None


def choose_recording_rename_targets(input_path: Path, post_title: str) -> dict[Path, Path]:
    sanitized_title = sanitize_filename_title(post_title)
    transcript_suffix = transcript_suffix_chain(input_path)
    media_path = source_media_path_for_transcript(input_path)
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


def rename_recording_files(input_path: Path, post_title: str) -> Path:
    rename_targets = choose_recording_rename_targets(input_path, post_title)
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


def indent_block(text: str, spaces: int = 2) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else prefix for line in text.splitlines())


def compose_post_markdown(
    input_path: Path,
    result: SummaryResult,
) -> str:
    date_value, _ = derive_post_metadata(input_path)
    title = result.post_title
    front_matter = [
        "---",
        "layout: post",
        f'title: {json.dumps(title)}',
        f'date: "{date_value:%Y-%m-%d %H:%M:%S}"',
        f'source: {json.dumps(result.post_source)}',
        "comments: false",
        "raw_llm_input: |",
        indent_block(result.rendered_transcript),
        "---",
        "",
    ]
    return "\n".join(front_matter) + result.summary_markdown.rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize a meeting transcript JSONL file with LiteLLM."
    )
    parser.add_argument(
        "transcript",
        type=Path,
        help="Path to a diarized transcript .jsonl file",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="LiteLLM model string, e.g. gpt-4.1-mini or gemini/gemini-2.0-flash",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_LLM_TIMEOUT_SECONDS,
        help="Per-request LLM timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_LLM_RETRIES,
        help="Number of retries for timeout/provider failures and malformed JSON responses.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional markdown output path. Defaults next to the input file.",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        help="Optional log file path. Defaults under preprocessing/logs/.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the summary instead of writing a file",
    )
    return parser.parse_args()


def main() -> int:
    global DEFAULT_LLM_TIMEOUT_SECONDS
    global DEFAULT_LLM_RETRIES
    args = parse_args()
    DEFAULT_LLM_TIMEOUT_SECONDS = args.timeout_seconds
    DEFAULT_LLM_RETRIES = args.retries
    log_path = (args.log_path or default_log_path(args.transcript)).expanduser()
    setup_logging(log_path)
    LOGGER.info(
        "Starting summarization for %s with timeout=%ss retries=%s",
        args.transcript,
        DEFAULT_LLM_TIMEOUT_SECONDS,
        DEFAULT_LLM_RETRIES,
    )
    result = summarize_meeting_transcript(
        transcript_path=args.transcript,
        model=args.model,
    )

    if args.stdout:
        LOGGER.info("Printing summary to stdout")
        print(result.summary_markdown)
        return 0

    output_path = (args.output or default_output_path(args.transcript, result.post_title)).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(compose_post_markdown(args.transcript, result), encoding="utf-8")
    LOGGER.info("Wrote private post to %s", output_path)
    renamed_transcript_path = rename_recording_files(args.transcript.expanduser(), result.post_title)
    if renamed_transcript_path != args.transcript.expanduser():
        LOGGER.info("Updated transcript path to %s", renamed_transcript_path)
    print(f"Wrote post to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
