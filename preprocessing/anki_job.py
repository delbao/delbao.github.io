#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_RETRIES = 2
DEFAULT_PROMPT_NAME = "anki_cards"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ALLOWED_MODES = ("self_improvement", "phrase_mining")
DEFAULT_MODE = "self_improvement"
ALLOWED_FOCUSES = ("word_choice", "communication_clarity", "speaking_structure")
DEFAULT_FOCUSES = ALLOWED_FOCUSES
DEFAULT_POINT_COUNT = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ANKI CSV from post text JSON read from stdin.")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="LiteLLM model string, e.g. gpt-4.1-mini or gemini/gemini-2.0-flash",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-request LLM timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Number of retries for provider failures or malformed JSON responses.",
    )
    parser.add_argument(
        "--prompt-name",
        default=DEFAULT_PROMPT_NAME,
        help="Prompt template prefix under preprocessing/prompts/.",
    )
    return parser.parse_args()


def truncate_text(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    trimmed = value.strip()
    if len(trimmed) <= max_length:
        return trimmed
    return f"{trimmed[:max_length]}\n...[truncated]"


@lru_cache(maxsize=None)
def load_prompt(prompt_name: str, prompt_kind: str) -> str:
    prompt_path = PROMPTS_DIR / f"{prompt_name}_{prompt_kind}.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Missing prompt file: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def load_mode_prompt(mode_name: str) -> str:
    mode_path = PROMPTS_DIR / f"anki_mode_{mode_name}.txt"
    if not mode_path.exists():
        raise FileNotFoundError(f"Missing mode prompt file: {mode_path}")
    return mode_path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def load_focus_prompt(focus_name: str) -> str:
    focus_path = PROMPTS_DIR / f"anki_focus_{focus_name}.txt"
    if not focus_path.exists():
        raise FileNotFoundError(f"Missing focus prompt file: {focus_path}")
    return focus_path.read_text(encoding="utf-8").strip()


def normalize_mode(payload: dict[str, Any]) -> str:
    raw_mode = payload.get("mode")
    if not isinstance(raw_mode, str):
        return DEFAULT_MODE

    mode = raw_mode.strip()
    return mode if mode in ALLOWED_MODES else DEFAULT_MODE


def normalize_focuses(payload: dict[str, Any]) -> tuple[str, ...]:
    raw_focuses = payload.get("focuses")
    if not isinstance(raw_focuses, list):
        return DEFAULT_FOCUSES

    focuses = []
    for item in raw_focuses:
        if not isinstance(item, str):
            continue
        focus = item.strip()
        if focus and focus in ALLOWED_FOCUSES and focus not in focuses:
            focuses.append(focus)

    normalized = tuple(focuses)
    return normalized or DEFAULT_FOCUSES


def build_messages(args: argparse.Namespace, payload: dict[str, Any]) -> list[dict[str, str]]:
    text = truncate_text(payload.get("text"), 36000)
    prompt_name = str(payload.get("prompt_name") or args.prompt_name or DEFAULT_PROMPT_NAME).strip() or DEFAULT_PROMPT_NAME
    mode_requirements = load_mode_prompt(normalize_mode(payload))
    focus_requirements = "\n".join(f"- {load_focus_prompt(focus_name)}" for focus_name in normalize_focuses(payload))
    point_count = normalize_point_count(payload)
    user_pointers = format_user_pointers(payload)
    system_prompt = load_prompt(prompt_name, "system")
    user_prompt = load_prompt(prompt_name, "user").format(
        source_text=text,
        mode_requirements=mode_requirements,
        focus_requirements=focus_requirements,
        point_count=point_count,
        user_pointers=user_pointers,
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def serialize_messages(messages: list[dict[str, str]]) -> str:
    parts = []
    for message in messages:
        role = str(message.get("role") or "").strip().title() or "Message"
        content = str(message.get("content") or "").strip()
        parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts).strip()


def parse_source_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"metadata": []}
    current = "metadata"

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        lower = line.lower()
        if lower == "raw transcript:":
            current = "raw_transcript"
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    metadata: dict[str, str] = {}
    for line in sections.get("metadata", []):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip()

    return {
        "title": metadata.get("title", "").strip(),
        "date": metadata.get("date", "").strip(),
        "source": metadata.get("source", "").strip(),
        "video_url": metadata.get("video url", "").strip(),
        "raw_transcript": "\n".join(sections.get("raw_transcript", [])).strip(),
    }


def normalize_point_count(payload: dict[str, Any]) -> int:
    raw_value = payload.get("point_count")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_POINT_COUNT
    return max(1, min(50, value))


def normalize_user_pointers(payload: dict[str, Any]) -> tuple[str, ...]:
    raw_value = payload.get("user_pointers")
    if not isinstance(raw_value, str):
        return ()

    parts = re.split(r"[\n,]+", raw_value)
    pointers = []
    for part in parts:
        pointer = part.strip()
        if pointer and pointer not in pointers:
            pointers.append(pointer)
    return tuple(pointers)


def format_user_pointers(payload: dict[str, Any]) -> str:
    pointers = normalize_user_pointers(payload)
    if not pointers:
        return "- None provided."
    return "\n".join(f"- {pointer}" for pointer in pointers)


def normalize_phrase_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def stem_token(token: str) -> str:
    irregular_map = {
        "made": "make",
        "making": "make",
        "ran": "run",
        "running": "run",
    }
    if token in irregular_map:
        return irregular_map[token]
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def tokenize_for_match(value: str) -> list[str]:
    return [stem_token(token) for token in re.findall(r"[a-z0-9]+", value.lower()) if token]


def count_pointer_match(pointer: str, chunk: str) -> int:
    normalized_pointer = normalize_phrase_for_match(pointer)
    normalized_chunk = normalize_phrase_for_match(chunk)
    if normalized_pointer and normalized_pointer in normalized_chunk:
        return 2

    pointer_tokens = tokenize_for_match(pointer)
    chunk_tokens = tokenize_for_match(chunk)
    if not pointer_tokens or not chunk_tokens:
        return 0

    pointer_joined = "".join(pointer_tokens)
    chunk_joined = "".join(chunk_tokens)
    if pointer_joined and pointer_joined in chunk_joined:
        return 2

    shared_tokens = [token for token in pointer_tokens if token in chunk_tokens]
    if len(shared_tokens) == len(pointer_tokens):
        return 2
    if shared_tokens:
        return 1
    return 0


def best_matching_pointer(chunk: str, user_pointers: tuple[str, ...]) -> tuple[str | None, int]:
    best_pointer: str | None = None
    best_score = 0
    for pointer in user_pointers:
        score = count_pointer_match(pointer, chunk)
        if score > best_score:
            best_pointer = pointer
            best_score = score
    return best_pointer, best_score


def format_pointer_label(pointer: str) -> str:
    words = [word for word in re.split(r"[^a-zA-Z0-9]+", pointer.strip()) if word]
    if not words:
        return "Useful phrase"
    return " ".join(word.capitalize() for word in words)


def parse_timestamp_to_seconds(value: str) -> int:
    hours, minutes, seconds = [int(part) for part in value.split(":")]
    return hours * 3600 + minutes * 60 + seconds


def build_timestamp_link(video_url: str, start_seconds: int) -> str:
    if not video_url:
        return ""

    separator = "&" if "?" in video_url else "?"
    return f"{video_url}{separator}t={start_seconds // 60}m{start_seconds % 60}s"


def split_into_sentence_like_chunks(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return []

    parts = [part.strip(" ,") for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    if len(parts) > 1:
        return parts

    words = normalized.split()
    if len(words) <= 24:
        return [normalized]

    chunks = []
    for index in range(0, len(words), 20):
        chunk = " ".join(words[index:index + 20]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def parse_transcript_entries(transcript: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pattern = re.compile(r"^\[(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})\]\s+([^:]+):\s*(.*)$")

    for line in transcript.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        start_time, end_time, speaker, content = match.groups()
        cleaned_content = re.sub(r"\s+", " ", content).strip()
        if not cleaned_content:
            continue
        entries.append(
            {
                "start_time": start_time,
                "start_seconds": parse_timestamp_to_seconds(start_time),
                "end_seconds": parse_timestamp_to_seconds(end_time),
                "speaker": speaker.strip(),
                "content": cleaned_content,
            }
        )

    return entries


def normalize_text_fragment(text: str) -> str:
    normalized = re.sub(r"\b(yeah|uh|um)\b[\s,.]*", " ", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\bI don't know\b", "I am not fully certain", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bkind of\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bsort of\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^(so|but|and)\s*,?\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bI actually\b", "I", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bI'?m thinking maybe\b", "I think", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\blet me share with you\b", "let me show you", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bpretty typical\b", "a typical pattern", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\ba few minutes because you're going to have the vacation tomorrow\b", "a few minutes before your vacation tomorrow", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ,")
    normalized = re.sub(r"\b([A-Za-z]+)(?:\s+\1\b){1,}", r"\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ,")
    if not normalized:
        return ""
    normalized = normalized[0].upper() + normalized[1:]
    if normalized[-1] not in ".!?":
        normalized += "."
    return normalized


def is_low_value_self_improvement_chunk(text: str) -> bool:
    lowered = text.lower()
    if len(text.split()) < 8:
        return True

    low_value_patterns = (
        "how are you doing",
        "all good",
        "sound, camera",
        "thanks for taking time",
        "mostly free today",
        "vacation tomorrow",
        "just a few days",
        "awesome, awesome",
    )
    return any(pattern in lowered for pattern in low_value_patterns)


def build_quote_excerpt(text: str, max_words: int = 16) -> str:
    words = text.split()
    excerpt = " ".join(words[:max_words]).strip(" ,")
    if len(words) > max_words:
        excerpt += "..."
    return excerpt


def infer_self_improvement_feedback(text: str, sequence: int) -> tuple[str, str, str]:
    lowered = text.lower()
    filler_count = len(re.findall(r"\b(yeah|uh|um|like|actually)\b", lowered))
    repeated_word_match = re.search(r"\b(\w+)\b(?:\s+\1\b){1,}", lowered)

    if "i don't know" in lowered or "not sure" in lowered or "maybe" in lowered:
        return (
            "State uncertainty more cleanly",
            "Acknowledge uncertainty directly without weakening your point.",
            normalize_text_fragment(text),
        )
    if filler_count >= 3:
        return (
            "Reduce filler words",
            "Too many fillers dilute confidence and distract from the point.",
            normalize_text_fragment(text),
        )
    if repeated_word_match:
        return (
            "Avoid repetition",
            "Compress repeated wording so the main idea lands once and clearly.",
            normalize_text_fragment(text),
        )
    if len(text.split()) >= 28:
        return (
            "Break long explanations",
            "Shorter sentences would make the point easier to follow.",
            normalize_text_fragment(text),
        )
    if lowered.startswith(("so ", "but ", "and ")):
        return (
            "Use a clearer transition",
            "Introduce the next point more explicitly instead of relying on a soft transition word.",
            normalize_text_fragment(text),
        )

    labels = (
        ("Tighten wording", "Use more precise wording so the main point sounds more deliberate."),
        ("Sharpen the main point", "Lead with the conclusion more directly before adding supporting detail."),
        ("Make the phrasing more direct", "A more direct sentence would sound clearer and more confident."),
    )
    label, suggestion = labels[(sequence - 1) % len(labels)]
    return (label, suggestion, normalize_text_fragment(text))


def infer_phrase_mining_feedback(text: str, sequence: int) -> tuple[str, str, str]:
    labels = (
        ("Reusable framing", "This is a useful framing pattern worth reusing in future conversations."),
        ("Reusable transition", "This transition is worth keeping because it moves the conversation cleanly."),
        ("Reusable explanation", "This explanation pattern is strong and can be reused in similar discussions."),
    )
    label, suggestion = labels[(sequence - 1) % len(labels)]
    revision = normalize_text_fragment(text)
    return (label, suggestion, revision)


def build_formatted_card(title: str, video_url: str, sequence: int, label: str, start_seconds: int, quote: str, suggestion: str, revision: str) -> dict[str, str]:
    safe_title = title or "Meeting"
    front = f"{safe_title} ({sequence}): {label}"

    parts = []
    timestamp_url = build_timestamp_link(video_url, start_seconds)
    if timestamp_url:
        escaped_url = html.escape(timestamp_url, quote=True)
        parts.append(f"<a href='{escaped_url}'>{escaped_url}</a>")
    parts.append(f"<b>Origin quote:</b> {html.escape(quote)}")
    parts.append(f"<b>Suggestion:</b> {html.escape(suggestion)}")
    parts.append(f"<b>Revision:</b> {html.escape(revision)}")

    return {
        "front": front,
        "back": "<br></br>".join(parts),
    }


def build_structured_cards_from_transcript(text: str, mode: str, focuses: tuple[str, ...], point_count: int, user_pointers: tuple[str, ...]) -> list[dict[str, str]]:
    sections = parse_source_sections(text)
    transcript = sections["raw_transcript"]
    title = sections["title"] or "Meeting"
    video_url = sections["video_url"]
    entries = parse_transcript_entries(transcript)
    if not entries:
        return []

    target_speaker = "Del" if mode == "self_improvement" else None
    speaker_candidates = [entry for entry in entries if not target_speaker or entry["speaker"] == target_speaker]
    if mode == "phrase_mining":
        non_del = [entry for entry in entries if entry["speaker"] != "Del"]
        speaker_candidates = non_del or entries

    candidates: list[tuple[int, int, dict[str, str]]] = []
    seen_quotes: set[str] = set()
    order = 0
    for entry in speaker_candidates:
        chunks = split_into_sentence_like_chunks(entry["content"])
        total_chunks = max(len(chunks), 1)
        duration = max(entry["end_seconds"] - entry["start_seconds"], 0)
        for index, chunk in enumerate(chunks):
            if len(chunk.split()) < 6:
                continue
            if mode == "self_improvement" and is_low_value_self_improvement_chunk(chunk):
                continue
            normalized_key = re.sub(r"\s+", " ", chunk.lower()).strip(" ,.")
            if normalized_key in seen_quotes:
                continue
            seen_quotes.add(normalized_key)
            sequence = order + 1
            chunk_start_seconds = entry["start_seconds"] + round(duration * (index / total_chunks))
            matched_pointer, pointer_score = best_matching_pointer(chunk, user_pointers)
            if mode == "self_improvement":
                label, suggestion, revision = infer_self_improvement_feedback(chunk, sequence)
            else:
                label, suggestion, revision = infer_phrase_mining_feedback(chunk, sequence)
                if matched_pointer and pointer_score >= 2:
                    label = format_pointer_label(matched_pointer)
                    suggestion = f"This phrase directly matches your priority pointer and is worth reusing."
            order += 1
            candidates.append(
                (
                    pointer_score,
                    order,
                    build_formatted_card(
                        title=title,
                        video_url=video_url,
                        sequence=sequence,
                        label=label,
                        start_seconds=chunk_start_seconds,
                        quote=build_quote_excerpt(chunk),
                        suggestion=suggestion,
                        revision=revision,
                    ),
                )
            )
    sorted_candidates = sorted(candidates, key=lambda item: (-item[0], item[1]))
    cards: list[dict[str, str]] = []
    for sequence, (_, _, card) in enumerate(sorted_candidates[:point_count], start=1):
        card["front"] = re.sub(r"\(\d+\):", f"({sequence}):", card["front"], count=1)
        cards.append(card)
    return cards


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def extract_text_content(message: object) -> str:
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


def provider_credentials_configured() -> bool:
    return any(
        os.environ.get(name)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "AZURE_OPENAI_API_KEY",
        )
    )


def call_model(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
    if not provider_credentials_configured():
        raise RuntimeError(
            "No LLM provider credentials configured. Set one of OPENAI_API_KEY, ANTHROPIC_API_KEY, "
            "GEMINI_API_KEY, GOOGLE_API_KEY, or AZURE_OPENAI_API_KEY."
        )

    try:
        from litellm import completion
    except ImportError as exc:
        raise RuntimeError(
            "LiteLLM is not installed in the Python runtime used by the job. Install the preprocessing dependencies first."
        ) from exc

    last_error: Exception | None = None

    for attempt in range(1, args.retries + 2):
        try:
            response = completion(
                model=args.model,
                messages=messages,
                temperature=0.3,
                timeout=args.timeout_seconds,
                response_format={"type": "json_object"},
            )
            raw_text = extract_text_content(response.choices[0].message)
            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object from LLM response")
            return parsed
        except Exception as exc:
            last_error = exc
            if attempt > args.retries:
                break

    assert last_error is not None
    raise last_error


def to_csv(cards: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)

    for card in cards:
        front = str(card.get("front") or "").strip()
        back = str(card.get("back") or "").strip()
        if not front or not back:
            continue
        writer.writerow([front, back])

    return output.getvalue()


def main() -> int:
    args = parse_args()
    log("Reading job payload")
    payload = json.load(sys.stdin)
    if payload.get("job_type") != "anki_csv":
        raise ValueError("Only job_type=anki_csv is supported")

    if not isinstance(payload.get("text"), str) or not str(payload.get("text")).strip():
        raise ValueError("A non-empty text payload is required")

    messages = build_messages(args, payload)
    prompt_text = serialize_messages(messages)
    log("Building prompt")
    parsed = call_model(args, messages)
    log("Parsing model response")
    cards = parsed.get("cards")
    if not isinstance(cards, list):
        raise ValueError("Expected 'cards' array in LLM response")

    log(f"Converting {len(cards)} cards to CSV")
    csv_content = to_csv(cards)
    if not csv_content.strip():
        raise ValueError("Generated CSV was empty")

    log("Writing final job result")
    json.dump(
        {
            "content": csv_content,
            "content_type": "text/csv;charset=utf-8",
            "file_name": f"{str(payload.get('file_stem') or 'anki-cards').strip()}-anki.csv",
            "prompt_text": prompt_text,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
