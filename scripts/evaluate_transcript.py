#!/usr/bin/env python3
"""Compare a completed JSON transcript with a human-controlled UTF-8 reference."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class AlignmentItem:
    operation: str
    reference: str | None
    hypothesis: str | None


def normalize(text: str) -> list[str]:
    normalized = text.lower().replace("ё", "е")
    return [word for word in re.sub(r"[^a-zа-я0-9]+", " ", normalized).split() if word]


def align(reference: Sequence[str], hypothesis: Sequence[str]) -> list[AlignmentItem]:
    rows = len(reference) + 1
    columns = len(hypothesis) + 1
    costs = [[0] * columns for _ in range(rows)]
    previous: list[list[str | None]] = [[None] * columns for _ in range(rows)]
    for index in range(1, rows):
        costs[index][0] = index
        previous[index][0] = "deletion"
    for index in range(1, columns):
        costs[0][index] = index
        previous[0][index] = "insertion"

    for row in range(1, rows):
        for column in range(1, columns):
            if reference[row - 1] == hypothesis[column - 1]:
                choices = ((costs[row - 1][column - 1], "equal"),)
            else:
                choices = (
                    (costs[row - 1][column - 1] + 1, "substitution"),
                    (costs[row - 1][column] + 1, "deletion"),
                    (costs[row][column - 1] + 1, "insertion"),
                )
            costs[row][column], previous[row][column] = min(choices, key=lambda item: item[0])

    result: list[AlignmentItem] = []
    row, column = len(reference), len(hypothesis)
    while row or column:
        operation = previous[row][column]
        if operation in {"equal", "substitution"}:
            result.append(AlignmentItem(operation, reference[row - 1], hypothesis[column - 1]))
            row -= 1
            column -= 1
        elif operation == "deletion":
            result.append(AlignmentItem(operation, reference[row - 1], None))
            row -= 1
        elif operation == "insertion":
            result.append(AlignmentItem(operation, None, hypothesis[column - 1]))
            column -= 1
        else:
            raise RuntimeError("Invalid alignment state")
    result.reverse()
    return result


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, reference_item in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (reference_item != hypothesis_item),
                )
            )
        previous = current
    return previous[-1]


def evaluate(reference_text: str, hypothesis_text: str) -> dict[str, object]:
    reference = normalize(reference_text)
    hypothesis = normalize(hypothesis_text)
    alignment = align(reference, hypothesis)
    substitutions = sum(item.operation == "substitution" for item in alignment)
    deletions = sum(item.operation == "deletion" for item in alignment)
    insertions = sum(item.operation == "insertion" for item in alignment)
    errors = substitutions + deletions + insertions
    reference_characters = list(" ".join(reference))
    hypothesis_characters = list(" ".join(hypothesis))
    character_errors = edit_distance(reference_characters, hypothesis_characters)
    return {
        "reference_words": len(reference),
        "hypothesis_words": len(hypothesis),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "word_error_rate": round(errors / len(reference), 4) if reference else None,
        "reference_characters": len(reference_characters),
        "hypothesis_characters": len(hypothesis_characters),
        "character_error_rate": (
            round(character_errors / len(reference_characters), 4) if reference_characters else None
        ),
        "errors": [
            {
                "operation": item.operation,
                "reference": item.reference,
                "hypothesis": item.hypothesis,
            }
            for item in alignment
            if item.operation != "equal"
        ],
    }


def load_critical_fields(path: Path) -> list[tuple[str, list[str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValueError("Critical-fields schema_version must be '1.0'")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("Critical-fields file requires a non-empty fields list")
    parsed: list[tuple[str, list[str]]] = []
    names: set[str] = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            raise ValueError("Every critical field must be an object")
        name = item.get("name")
        accepted = item.get("accepted")
        if not isinstance(name, str) or not name.strip() or name.strip() in names:
            raise ValueError("Critical field names must be unique non-empty strings")
        if not isinstance(accepted, list) or not accepted or not all(
            isinstance(value, str) and normalize(value) for value in accepted
        ):
            raise ValueError(f"Critical field {name!r} requires non-empty accepted strings")
        normalized_name = name.strip()
        names.add(normalized_name)
        parsed.append((normalized_name, accepted))
    return parsed


def evaluate_critical_fields(
    fields: Sequence[tuple[str, Sequence[str]]],
    hypothesis_text: str,
) -> dict[str, object]:
    hypothesis = normalize(hypothesis_text)
    results: list[dict[str, object]] = []
    for name, accepted in fields:
        candidates = [normalize(value) for value in accepted]
        matched = any(
            candidate
            and any(
                hypothesis[index : index + len(candidate)] == candidate
                for index in range(len(hypothesis) - len(candidate) + 1)
            )
            for candidate in candidates
        )
        results.append({"name": name, "matched": matched})
    matched_count = sum(bool(item["matched"]) for item in results)
    return {
        "total": len(results),
        "matched": matched_count,
        "exact_match_rate": round(matched_count / len(results), 4) if results else None,
        "fields": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Optional evaluation JSON")
    parser.add_argument(
        "--critical-fields",
        type=Path,
        help="Optional JSON with named fields and explicit accepted text variants",
    )
    args = parser.parse_args()

    reference_text = args.reference.read_text(encoding="utf-8")
    transcript = json.loads(args.transcript.read_text(encoding="utf-8"))
    if transcript.get("status") != "completed":
        raise SystemExit("transcript JSON is not completed")
    final_text = str(transcript.get("text", ""))
    result = {
        "raw_asr": evaluate(reference_text, str(transcript.get("raw_text", final_text))),
        "final": evaluate(reference_text, final_text),
    }
    if args.critical_fields is not None:
        fields = load_critical_fields(args.critical_fields)
        result["critical_fields"] = {
            "raw_asr": evaluate_critical_fields(
                fields,
                str(transcript.get("raw_text", final_text)),
            ),
            "final": evaluate_critical_fields(fields, final_text),
        }
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
