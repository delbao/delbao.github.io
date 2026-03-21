#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ANKI CSV from post text JSON read from stdin.")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="LiteLLM model string, e.g. gpt-4.1-mini or gemini/gemini-2.0-flash",
    )
    parser.add_argument(
        "--fallback-model",
        default="",
        help="Optional fallback LiteLLM model string to try if the primary model fails.",
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
    system_prompt = load_prompt(prompt_name, "system")
    user_prompt = load_prompt(prompt_name, "user").format(
        source_text=text,
        mode_requirements=mode_requirements,
        focus_requirements=focus_requirements,
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


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
    from litellm import completion

    models = [args.model]
    if args.fallback_model and args.fallback_model != args.model:
        models.append(args.fallback_model)

    last_error: Exception | None = None

    for model in models:
        for attempt in range(1, args.retries + 2):
            try:
                response = completion(
                    model=model,
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


def build_fallback_cards(text: str, mode: str, focuses: tuple[str, ...]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    title_line = next((line for line in lines if line.lower().startswith("title:")), "")
    if title_line:
        title = title_line.split(":", 1)[1].strip()
        if title:
            cards.append(
                {
                    "front": "What is the title of this note?",
                    "back": title,
                }
            )

    cleaned_sentences: list[str] = []
    for line in lines:
        if line.lower().startswith(("title:", "date:", "source:", "post content:", "raw transcript:")):
            continue
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", line) if part.strip()]
        for part in parts:
            if len(part) >= 25:
                cleaned_sentences.append(part)

    prefixes = [mode.replace("_", " ")] if mode else []
    prefixes.extend(focus.replace("_", " ") for focus in focuses)
    focus_prefix = " / ".join(prefixes)

    for sentence in cleaned_sentences[:8]:
        subject = sentence[:56].rstrip(" ,;:")
        cards.append(
            {
                "front": f"What should you remember about '{subject}'?" if not focus_prefix else f"[{focus_prefix}] What should you remember about '{subject}'?",
                "back": sentence,
            }
        )

    if not cards and text.strip():
        cards.append(
            {
                "front": "What is the main content of this note?",
                "back": text.strip()[:500],
            }
        )

    return cards


def to_csv(cards: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["front", "back"])

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

    text = str(payload.get("text"))
    mode = normalize_mode(payload)
    focuses = normalize_focuses(payload)
    if provider_credentials_configured():
        log("Building prompt")
        parsed = call_model(args, build_messages(args, payload))
        log("Parsing model response")
        cards = parsed.get("cards")
        if not isinstance(cards, list):
            raise ValueError("Expected 'cards' array in LLM response")
    else:
        log("No LLM provider credentials found; using local fallback card generator")
        cards = build_fallback_cards(text, mode, focuses)

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
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
