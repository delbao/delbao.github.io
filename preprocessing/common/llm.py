from __future__ import annotations

from contextlib import suppress
import json
import logging
import re
import time

LOGGER = logging.getLogger("summarize_transcript")


class LLMClient:
    def __init__(self, model: str, timeout_seconds: float, retries: int) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        return self.extract_json_object(self.complete_text(system_prompt, user_prompt))

    def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        LOGGER.info(
            "Calling LLM model=%s system_chars=%s user_chars=%s timeout=%ss retries=%s",
            self.model,
            len(system_prompt),
            len(user_prompt),
            self.timeout_seconds,
            self.retries,
        )
        try:
            from litellm import completion
        except ImportError as exc:
            raise RuntimeError(
                "litellm is not installed. Create .venv and run "
                "`pip install -r preprocessing/requirements.txt`."
            ) from exc

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 2):
            try:
                LOGGER.info("LLM request attempt %s/%s", attempt, self.retries + 1)
                response = completion(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.2,
                    timeout=self.timeout_seconds,
                    response_format={"type": "json_object"},
                )
                LOGGER.info("Received LLM response from model=%s", self.model)
                return self.extract_text_content(response.choices[0].message)
            except Exception as exc:
                last_error = exc
                LOGGER.warning("LLM request attempt %s failed: %s", attempt, exc)
                if attempt > self.retries:
                    raise
                sleep_seconds = min(2 ** (attempt - 1), 8)
                LOGGER.info("Retrying LLM request in %ss", sleep_seconds)
                time.sleep(sleep_seconds)

        assert last_error is not None
        raise last_error

    @staticmethod
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

    @staticmethod
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
