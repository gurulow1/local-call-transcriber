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


def evaluate(reference_text: str, hypothesis_text: str) -> dict[str, object]:
    reference = normalize(reference_text)
    hypothesis = normalize(hypothesis_text)
    alignment = align(reference, hypothesis)
    substitutions = sum(item.operation == "substitution" for item in alignment)
    deletions = sum(item.operation == "deletion" for item in alignment)
    insertions = sum(item.operation == "insertion" for item in alignment)
    errors = substitutions + deletions + insertions
    return {
        "reference_words": len(reference),
        "hypothesis_words": len(hypothesis),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "word_error_rate": round(errors / len(reference), 4) if reference else None,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Optional evaluation JSON")
    args = parser.parse_args()

    reference_text = args.reference.read_text(encoding="utf-8")
    transcript = json.loads(args.transcript.read_text(encoding="utf-8"))
    if transcript.get("status") != "completed":
        raise SystemExit("transcript JSON is not completed")
    result = evaluate(reference_text, str(transcript.get("text", "")))
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

