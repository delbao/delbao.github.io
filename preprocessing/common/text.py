from __future__ import annotations

import re


class TextNormalizer:
    @staticmethod
    def normalize_text(text: str) -> str:
        return " ".join(text.split())

    @staticmethod
    def normalize_name_key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    @staticmethod
    def soundex(text: str) -> str:
        normalized = TextNormalizer.normalize_name_key(text)
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

    @staticmethod
    def slugify(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "meeting-summary"

    @staticmethod
    def sanitize_filename_title(text: str) -> str:
        cleaned = re.sub(r'[\\/:\0]+', " ", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.strip(" .")
        return cleaned or "Meeting"
