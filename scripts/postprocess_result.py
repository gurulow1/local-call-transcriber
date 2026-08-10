#!/usr/bin/env python3
"""Create a separately named, deterministically cleaned copy of an ASR JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from local_transcriber.postprocessing import postprocess_segments  # noqa: E402
from local_transcriber.service import SCHEMA_VERSION  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a cleaned JSON copy while preserving the original ASR text",
    )
    parser.add_argument("--input-json", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = args.input_json
    output_path = args.output_json
    if input_path.is_symlink() or not input_path.is_file():
        raise SystemExit("Input JSON must be an existing regular file, not a symlink")
    if input_path.suffix.lower() != ".json" or output_path.suffix.lower() != ".json":
        raise SystemExit("Input and output filenames must end with .json")
    if input_path.resolve() == output_path.resolve(strict=False):
        raise SystemExit("Output must use a separate filename; the source JSON is never overwritten")
    if output_path.exists() and not args.overwrite:
        raise SystemExit("Output already exists; use --overwrite for explicit replacement")

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read input JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        raise SystemExit("Input JSON must be a completed transcription result")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not all(isinstance(segment, dict) for segment in segments):
        raise SystemExit("Input JSON must contain a list of segment objects")

    postprocessed = postprocess_segments(segments)
    cleaned_payload: dict[str, Any] = dict(payload)
    cleaned_payload["schema_version"] = SCHEMA_VERSION
    cleaned_payload["text"] = postprocessed.text
    cleaned_payload["raw_text"] = postprocessed.raw_text
    cleaned_payload["segments"] = postprocessed.segments
    cleaned_payload["postprocessing"] = postprocessed.metadata
    _atomic_write_json(output_path, cleaned_payload)
    print(
        json.dumps(
            {
                "status": "completed",
                "input": str(input_path),
                "output": str(output_path),
                "postprocessing": postprocessed.metadata,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
            json.dump(payload, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
