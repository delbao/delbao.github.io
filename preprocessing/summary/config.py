from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from functools import lru_cache
from pathlib import Path


DEFAULT_MODEL = os.environ.get("TRANSCRIPT_SUMMARY_MODEL", "gpt-4.1-mini")
DEFAULT_LLM_TIMEOUT_SECONDS = float(os.environ.get("TRANSCRIPT_SUMMARY_TIMEOUT_SECONDS", "90"))
DEFAULT_LLM_RETRIES = int(os.environ.get("TRANSCRIPT_SUMMARY_RETRIES", "2"))
FUZZY_NAME_THRESHOLD = 0.8
PHONETIC_FUZZY_NAME_THRESHOLD = 0.55
PREPROCESSING_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PREPROCESSING_DIR.parent
PROMPTS_DIR = PREPROCESSING_DIR / "prompts"
SPEAKER_NAME_CORRECTIONS_PATH = PREPROCESSING_DIR / "speaker_name_corrections.json"
PRIVATE_POSTS_DIR = REPO_ROOT / "_private_posts"
LOGS_DIR = PREPROCESSING_DIR / "logs"
CALENDAR_NAMES = ("Vanta", "个人")
CALENDAR_QUERY_PADDING = timedelta(minutes=20)
CALENDAR_NEAREST_START_MAX_DELTA = timedelta(minutes=90)
CALENDAR_EARLY_START_GRACE = timedelta(minutes=5)
CALENDAR_LATE_START_GRACE = timedelta(seconds=30)
CALENDAR_START_TIE_WINDOW = timedelta(minutes=3)
CALENDAR_MAX_REASONABLE_DURATION = timedelta(hours=4)
CALENDAR_TRANSCRIPT_EXCERPT_CHARS = 8000
DEFAULT_POST_SOURCE = "Personal"

LOGGER = logging.getLogger("summarize_transcript")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def assemble_prompt(*names: str) -> str:
    return "\n\n".join(load_prompt(name).strip() for name in names if name).strip()


@lru_cache(maxsize=1)
def load_speaker_name_corrections() -> dict[str, str]:
    from common.text import TextNormalizer

    raw = json.loads(SPEAKER_NAME_CORRECTIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("speaker_name_corrections.json must be a JSON object")

    corrections: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("speaker_name_corrections.json keys and values must be strings")
        normalized_key = TextNormalizer.normalize_name_key(key)
        normalized_value = value.strip()
        if normalized_key and normalized_value:
            corrections[normalized_key] = normalized_value
    return corrections
