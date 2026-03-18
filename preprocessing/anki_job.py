#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from typing import Any

from litellm import completion


DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_RETRIES = 2


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
    return parser.parse_args()


def truncate_text(value: object, max_length: int) -> str:
  if not isinstance(value, str):
      return ""
  trimmed = value.strip()
  if len(trimmed) <= max_length:
      return trimmed
  return f"{trimmed[:max_length]}\n...[truncated]"


def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    text = truncate_text(payload.get("text"), 36000)

    system_prompt = (
        "You create study materials from meeting and note content. "
        "Return JSON with one key named cards. cards must be an array of objects with "
        "string keys front and back. Keep cards concise, factual, and grounded only in the provided context."
    )
    user_prompt = "\n\n".join(
        part
        for part in [
            "Create high-value ANKI flashcards from the text below.",
            "Prefer 8 to 16 cards unless the material is too sparse.",
            "Focus on durable knowledge, decisions, action items, terminology, and key takeaways.",
            f"Source text:\n{text}",
        ]
        if part
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


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


def call_model(args: argparse.Namespace, messages: list[dict[str, str]]) -> dict[str, Any]:
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
    payload = json.load(sys.stdin)
    if payload.get("job_type") != "anki_csv":
        raise ValueError("Only job_type=anki_csv is supported")

    if not isinstance(payload.get("text"), str) or not str(payload.get("text")).strip():
        raise ValueError("A non-empty text payload is required")

    parsed = call_model(args, build_messages(payload))
    cards = parsed.get("cards")
    if not isinstance(cards, list):
        raise ValueError("Expected 'cards' array in LLM response")

    csv_content = to_csv(cards)
    if not csv_content.strip():
        raise ValueError("Generated CSV was empty")

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
