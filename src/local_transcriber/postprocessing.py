"""Deterministic local cleanup that always preserves the ASR text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import PostprocessingConfigError

DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "config" / "glossary.json"
TERMINAL_PUNCTUATION = frozenset({".", "!", "?", "…"})


@dataclass(frozen=True)
class ReplacementRule:
    """One literal, case-insensitive replacement with word-edge guards."""

    source: str
    target: str


@dataclass(frozen=True)
class Glossary:
    """Validated deterministic rules loaded from a local JSON file."""

    version: str
    terms: tuple[ReplacementRule, ...]
    phrases: tuple[ReplacementRule, ...]


@dataclass(frozen=True)
class PostprocessedTranscript:
    """Cleaned output plus the untouched text returned by ASR."""

    text: str
    raw_text: str
    segments: list[dict[str, Any]]
    metadata: dict[str, Any]


def load_glossary(path: Path = DEFAULT_GLOSSARY_PATH) -> Glossary:
    """Load and validate a local glossary; no fallback or network lookup is allowed."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PostprocessingConfigError(f"Cannot read local glossary: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PostprocessingConfigError(f"Local glossary is not valid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise PostprocessingConfigError("Local glossary root must be a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PostprocessingConfigError("Local glossary must contain a non-empty string version")
    terms = _parse_rules(payload.get("terms"), "terms")
    phrases = _parse_rules(payload.get("phrases"), "phrases")
    return Glossary(version=version.strip(), terms=terms, phrases=phrases)


def postprocess_segments(
    segments: Sequence[Mapping[str, Any]],
    *,
    glossary_path: Path = DEFAULT_GLOSSARY_PATH,
) -> PostprocessedTranscript:
    """Clean segment text while retaining every original ASR string in ``asr_text``."""

    glossary = load_glossary(glossary_path)
    cleaned_segments: list[dict[str, Any]] = []
    raw_parts: list[str] = []
    cleaned_parts: list[str] = []
    term_replacements = 0
    phrase_replacements = 0

    prepared: list[tuple[Mapping[str, Any], str, str, int, int]] = []
    for segment in segments:
        raw_value = segment.get("asr_text", segment.get("text", ""))
        raw_segment_text = str(raw_value).strip()
        cleaned_text = _normalize_whitespace(raw_segment_text)
        cleaned_text, term_count = _apply_rules(cleaned_text, glossary.terms)
        cleaned_text, phrase_count = _apply_rules(cleaned_text, glossary.phrases)
        prepared.append((segment, raw_segment_text, cleaned_text, term_count, phrase_count))

    nonempty_indexes = [index for index, item in enumerate(prepared) if item[2]]
    last_nonempty_index = nonempty_indexes[-1] if nonempty_indexes else None
    capitalize_next = True

    for index, (segment, raw_segment_text, cleaned_text, term_count, phrase_count) in enumerate(prepared):
        if cleaned_text and capitalize_next:
            cleaned_text = _capitalize_first_word(cleaned_text)
        had_sentence_end = _ends_sentence(cleaned_text)
        if index == last_nonempty_index:
            cleaned_text = _ensure_terminal_punctuation(cleaned_text)
        if cleaned_text:
            capitalize_next = had_sentence_end

        cleaned_segment = dict(segment)
        cleaned_segment["asr_text"] = raw_segment_text
        cleaned_segment["text"] = cleaned_text
        cleaned_segments.append(cleaned_segment)

        if raw_segment_text:
            raw_parts.append(raw_segment_text)
        if cleaned_text:
            cleaned_parts.append(cleaned_text)
        term_replacements += term_count
        phrase_replacements += phrase_count

    return PostprocessedTranscript(
        text=" ".join(cleaned_parts),
        raw_text=" ".join(raw_parts),
        segments=cleaned_segments,
        metadata={
            "method": "deterministic_glossary_v2",
            "glossary_version": glossary.version,
            "term_replacements": term_replacements,
            "phrase_replacements": phrase_replacements,
        },
    )


def _parse_rules(value: object, field_name: str) -> tuple[ReplacementRule, ...]:
    if not isinstance(value, list):
        raise PostprocessingConfigError(f"Local glossary field {field_name!r} must be a list")

    parsed: list[ReplacementRule] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PostprocessingConfigError(f"{field_name}[{index}] must be an object")
        source = item.get("from")
        target = item.get("to")
        if not isinstance(source, str) or not source.strip():
            raise PostprocessingConfigError(f"{field_name}[{index}].from must be a non-empty string")
        if not isinstance(target, str) or not target.strip():
            raise PostprocessingConfigError(f"{field_name}[{index}].to must be a non-empty string")
        normalized_source = _normalize_whitespace(source)
        key = normalized_source.casefold()
        if key in seen:
            raise PostprocessingConfigError(f"Duplicate source in {field_name}: {normalized_source!r}")
        seen.add(key)
        parsed.append(ReplacementRule(normalized_source, _normalize_whitespace(target)))

    # Longer literal phrases win if one source contains another.
    return tuple(sorted(parsed, key=lambda rule: len(rule.source), reverse=True))


def _apply_rules(text: str, rules: Sequence[ReplacementRule]) -> tuple[str, int]:
    total = 0
    for rule in rules:
        pattern = re.compile(rf"(?<!\w){re.escape(rule.source)}(?!\w)", flags=re.IGNORECASE)
        text, count = pattern.subn(lambda _match, target=rule.target: target, text)
        total += count
    return text, total


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _capitalize_first_word(text: str) -> str:
    for index, character in enumerate(text):
        if not character.isalpha():
            continue
        token_match = re.match(r"[\w-]+", text[index:])
        token = token_match.group(0) if token_match else character
        # Preserve brands such as cTrader and iPhone that intentionally start
        # with a lowercase letter and contain an uppercase letter later.
        if character.islower() and any(letter.isupper() for letter in token[1:]):
            return text
        return f"{text[:index]}{character.upper()}{text[index + 1:]}"
    return text


def _ensure_terminal_punctuation(text: str) -> str:
    if not text or _ends_sentence(text):
        return text
    if text[-1] in {",", ";", ":"}:
        return f"{text[:-1]}."
    return f"{text}."


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip()
    while stripped and stripped[-1] in {'"', "'", "»", "”", ")", "]", "}"}:
        stripped = stripped[:-1].rstrip()
    return bool(stripped) and stripped[-1] in TERMINAL_PUNCTUATION
