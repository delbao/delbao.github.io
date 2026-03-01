from __future__ import annotations

import re
from typing import Iterable

from common.text import TextNormalizer

from .models import RenderedTurn, TranscriptSegment


class TranscriptRenderer:
    @staticmethod
    def render(
        segments: Iterable[TranscriptSegment],
        speaker_name_map: dict[str, str] | None = None,
    ) -> str:
        speaker_name_map = speaker_name_map or {}
        lines: list[str] = []
        current_turn: RenderedTurn | None = None

        for segment in segments:
            text = TextNormalizer.normalize_text(segment.text)
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
                    f"[{TranscriptRenderer.format_time_range(current_turn.start, current_turn.end)}] "
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
                f"[{TranscriptRenderer.format_time_range(current_turn.start, current_turn.end)}] "
                f"{current_turn.speaker}: {' '.join(current_turn.parts)}"
            )

        return "\n".join(lines)

    @staticmethod
    def format_time_range(start: float, end: float) -> str:
        return (
            f"{TranscriptRenderer.seconds_to_hhmmss(start)}-"
            f"{TranscriptRenderer.seconds_to_hhmmss(end)}"
        )

    @staticmethod
    def seconds_to_hhmmss(value: float) -> str:
        total_seconds = max(int(value), 0)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def indent_block(text: str, spaces: int = 2) -> str:
        prefix = " " * spaces
        return "\n".join(f"{prefix}{line}" if line else prefix for line in text.splitlines())
