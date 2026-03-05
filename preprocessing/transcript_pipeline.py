#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from summary.config import DEFAULT_FALLBACK_MODEL, DEFAULT_LLM_RETRIES, DEFAULT_LLM_TIMEOUT_SECONDS, DEFAULT_MODEL, LOGGER
from summary.pipeline import FileNamingService, LoggingManager, PostComposer, TranscriptSummarizer


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
        "--fallback-model",
        default=DEFAULT_FALLBACK_MODEL,
        help="Optional fallback LiteLLM model string to try after the primary model fails.",
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
    args = parse_args()
    transcript_path = args.transcript.expanduser()
    log_path = (args.log_path or FileNamingService.default_log_path(transcript_path)).expanduser()
    LoggingManager.setup(log_path)
    LOGGER.info(
        "Starting summarization for %s with timeout=%ss retries=%s",
        transcript_path,
        args.timeout_seconds,
        args.retries,
    )

    summarizer = TranscriptSummarizer(
        model=args.model,
        fallback_model=args.fallback_model or None,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
    )
    result = summarizer.summarize_document(transcript_path)

    if args.stdout:
        LOGGER.info("Printing summary to stdout")
        print(result.summary_markdown)
        return 0

    output_path = (args.output or FileNamingService.default_output_path(transcript_path, result.post_title)).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(PostComposer.compose(transcript_path, result), encoding="utf-8")
    LOGGER.info("Wrote private post to %s", output_path)

    renamed_transcript_path = summarizer.rename_recording_files(transcript_path, result.post_title)
    if renamed_transcript_path != transcript_path:
        LOGGER.info("Updated transcript path to %s", renamed_transcript_path)

    print(f"Wrote post to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
