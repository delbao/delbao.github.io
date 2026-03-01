#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable


DEFAULT_MODEL = os.environ.get("TRANSCRIPT_SUMMARY_MODEL", "gpt-4.1-mini")
FUZZY_NAME_THRESHOLD = 0.8
PHONETIC_FUZZY_NAME_THRESHOLD = 0.55
PREPROCESSING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PREPROCESSING_DIR.parent
PROMPTS_DIR = PREPROCESSING_DIR / "prompts"
SPEAKER_NAME_CORRECTIONS_PATH = PREPROCESSING_DIR / "speaker_name_corrections.json"
PRIVATE_POSTS_DIR = REPO_ROOT / "_private_posts"
LOGS_DIR = PREPROCESSING_DIR / "logs"


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
        "Calling LLM model=%s system_chars=%s user_chars=%s",
        model,
        len(system_prompt),
        len(user_prompt),
    )
    try:
        from litellm import completion
    except ImportError as exc:
        raise RuntimeError(
            "litellm is not installed. Create .venv and run "
            "`pip install -r preprocessing/requirements.txt`."
        ) from exc

    response = completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    LOGGER.info("Received LLM response from model=%s", model)
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

    parsed = json.loads(candidate)
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
    raw_response = call_llm(model, system_prompt, user_prompt)
    parsed = extract_json_object(raw_response)
    LOGGER.info("Parsed structured LLM response")

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
    LOGGER.info("Rendered transcript with speaker names")
    return SummaryResult(
        speaker_name_map=speaker_name_map,
        rendered_transcript=rendered_transcript,
        summary_markdown=summary_markdown,
    )


def default_output_path(input_path: Path) -> Path:
    date_value, title = derive_post_metadata(input_path)
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


def indent_block(text: str, spaces: int = 2) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" if line else prefix for line in text.splitlines())


def compose_post_markdown(
    input_path: Path,
    result: SummaryResult,
) -> str:
    date_value, title = derive_post_metadata(input_path)
    front_matter = [
        "---",
        "layout: post",
        f'title: {json.dumps(title)}',
        f'date: "{date_value:%Y-%m-%d %H:%M:%S}"',
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
    args = parse_args()
    log_path = (args.log_path or default_log_path(args.transcript)).expanduser()
    setup_logging(log_path)
    LOGGER.info("Starting summarization for %s", args.transcript)
    result = summarize_meeting_transcript(
        transcript_path=args.transcript,
        model=args.model,
    )

    if args.stdout:
        LOGGER.info("Printing summary to stdout")
        print(result.summary_markdown)
        return 0

    output_path = (args.output or default_output_path(args.transcript)).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(compose_post_markdown(args.transcript, result), encoding="utf-8")
    LOGGER.info("Wrote private post to %s", output_path)
    print(f"Wrote post to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
