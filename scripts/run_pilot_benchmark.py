#!/usr/bin/env python3
"""Aggregate an already completed local pilot benchmark without running ASR."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from evaluate_transcript import edit_distance, evaluate, normalize

REPORT_SCHEMA_VERSION = "1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON file: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path.name}")
    return payload


def _resolve_input(manifest_path: Path, value: object, field: str, sample_id: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Sample {sample_id!r} requires a non-empty {field!r} path")
    path = Path(value)
    if not path.is_absolute():
        path = manifest_path.parent / path
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Sample {sample_id!r} {field!r} must be a regular file")
    return path.resolve(strict=True)


def _evidence(path: Path, field: str, sample_id: str) -> dict[str, int | str]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
    except OSError as exc:
        raise ValueError(f"Sample {sample_id!r} {field!r} cannot be read") from exc
    return {"sha256": digest.hexdigest(), "size_bytes": size_bytes}


def _score(reference_text: str, hypothesis_text: str) -> dict[str, int | float]:
    word_metrics = evaluate(reference_text, hypothesis_text)
    reference_characters = list(" ".join(normalize(reference_text)))
    hypothesis_characters = list(" ".join(normalize(hypothesis_text)))
    reference_words = int(word_metrics["reference_words"])
    word_errors = sum(int(word_metrics[key]) for key in ("substitutions", "deletions", "insertions"))
    character_errors = edit_distance(reference_characters, hypothesis_characters)
    return {
        "reference_words": reference_words,
        "word_errors": word_errors,
        "word_error_rate": word_errors / reference_words,
        "reference_characters": len(reference_characters),
        "character_errors": character_errors,
        "character_error_rate": character_errors / len(reference_characters),
    }


def _new_accumulator() -> dict[str, Any]:
    return {
        "reference_words": 0,
        "word_errors": 0,
        "reference_characters": 0,
        "character_errors": 0,
        "word_error_rates": [],
        "character_error_rates": [],
    }


def _accumulate(accumulator: dict[str, Any], score: dict[str, int | float]) -> None:
    for key in ("reference_words", "word_errors", "reference_characters", "character_errors"):
        accumulator[key] += int(score[key])
    accumulator["word_error_rates"].append(float(score["word_error_rate"]))
    accumulator["character_error_rates"].append(float(score["character_error_rate"]))


def _finish(accumulator: dict[str, Any]) -> dict[str, int | float | None]:
    word_rates = accumulator["word_error_rates"]
    character_rates = accumulator["character_error_rates"]
    return {
        "reference_words": accumulator["reference_words"],
        "word_errors": accumulator["word_errors"],
        "micro_word_error_rate": round(
            accumulator["word_errors"] / accumulator["reference_words"], 4
        ),
        "macro_word_error_rate": round(sum(word_rates) / len(word_rates), 4),
        "reference_characters": accumulator["reference_characters"],
        "character_errors": accumulator["character_errors"],
        "micro_character_error_rate": round(
            accumulator["character_errors"] / accumulator["reference_characters"], 4
        ),
        "macro_character_error_rate": round(sum(character_rates) / len(character_rates), 4),
    }


def _timestamp_violations(transcript: dict[str, Any]) -> int:
    duration = transcript.get("duration_seconds")
    duration_valid = (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration >= 0
    )
    violations = 0 if duration_valid else 1
    segments = transcript.get("segments")
    if not isinstance(segments, list):
        return violations + 1

    previous_start = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            violations += 1
            continue
        start = segment.get("start")
        end = segment.get("end")
        numeric = all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in (start, end)
        )
        if not numeric:
            violations += 1
            continue
        start = float(start)
        end = float(end)
        if (
            start < 0
            or end < start
            or start < previous_start
            or (duration_valid and (start > duration or end > duration))
        ):
            violations += 1
        previous_start = float(start)
    return violations


def git_identity(project_root: Path) -> dict[str, object]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"revision": None, "dirty": None}
    revision_text = revision.stdout.strip()
    return {
        "revision": revision_text if revision.returncode == 0 and revision_text else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def source_tree_sha256(project_root: Path) -> str:
    """Hash reviewable source, including uncommitted changes but excluding model weights."""

    included_roots = (
        ".github",
        "config",
        "docs",
        "examples",
        "models",
        "requirements",
        "schemas",
        "scripts",
        "security",
        "src",
        "tests",
    )
    files = [
        project_root / name
        for name in (
            ".gitattributes",
            ".gitignore",
            "pyproject.toml",
            "README.md",
            "THIRD_PARTY_NOTICES.md",
            "transcribe.py",
            "worker.py",
            "crm_api.py",
            "Запустить транскрибатор.cmd",
            "Запустить транскрибатор.sh",
            "Запустить транскрибатор.command",
            "Проверить готовность.cmd",
        )
        if (project_root / name).is_file()
    ]
    for root_name in included_roots:
        root = project_root / root_name
        if not root.is_dir():
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not {
                "whisper-large-v3",
                "whisper-vad",
                "t-one",
            }.intersection(path.relative_to(project_root).parts)
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda value: value.relative_to(project_root).as_posix()):
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def build_report(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.resolve(strict=True)
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != "1.0":
        raise ValueError("Pilot manifest schema_version must be '1.0'")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("Pilot manifest must contain a non-empty samples list")

    accumulators = {"raw_asr": _new_accumulator(), "final": _new_accumulator()}
    hallucinated_words = {"raw_asr": 0, "final": 0}
    sample_counts = {"total": 0, "speech": 0, "nonspeech": 0, "diagnostic": 0}
    timestamp_violation_count = 0
    timestamp_violation_samples = 0
    sample_ids: set[str] = set()
    evidence: list[dict[str, Any]] = []

    for item in samples:
        if not isinstance(item, dict):
            raise ValueError("Every pilot sample must be an object")
        sample_id = item.get("id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in sample_ids:
            raise ValueError("Every pilot sample requires a unique non-empty id")
        sample_ids.add(sample_id)
        kind = item.get("kind")
        if kind not in {"speech", "nonspeech", "diagnostic"}:
            raise ValueError(
                f"Sample {sample_id!r} kind must be 'speech', 'nonspeech', or 'diagnostic'"
            )
        transcript_path = _resolve_input(manifest_path, item.get("transcript"), "transcript", sample_id)
        transcript = _read_json(transcript_path)
        if transcript.get("status") != "completed":
            raise ValueError(f"Sample {sample_id!r} transcript is not completed")
        raw_text = transcript.get("raw_text")
        final_text = transcript.get("text")
        if not isinstance(raw_text, str) or not isinstance(final_text, str):
            raise ValueError(f"Sample {sample_id!r} transcript must contain string raw_text and text")

        sample_evidence: dict[str, Any] = {
            "id": sample_id,
            "transcript": _evidence(transcript_path, "transcript", sample_id),
        }
        reference_path: Path | None = None
        if kind == "speech" or "reference" in item:
            reference_path = _resolve_input(
                manifest_path,
                item.get("reference"),
                "reference",
                sample_id,
            )
            sample_evidence["reference"] = _evidence(reference_path, "reference", sample_id)
        if "audio" in item:
            audio_path = _resolve_input(manifest_path, item.get("audio"), "audio", sample_id)
            sample_evidence["audio"] = _evidence(audio_path, "audio", sample_id)
        evidence.append(sample_evidence)

        sample_counts["total"] += 1
        sample_counts[kind] += 1
        violations = _timestamp_violations(transcript)
        timestamp_violation_count += violations
        timestamp_violation_samples += bool(violations)

        if kind == "nonspeech":
            hallucinated_words["raw_asr"] += len(normalize(raw_text))
            hallucinated_words["final"] += len(normalize(final_text))
            continue
        if kind == "diagnostic":
            continue

        assert reference_path is not None
        reference_text = reference_path.read_text(encoding="utf-8")
        if not normalize(reference_text):
            raise ValueError(f"Sample {sample_id!r} reference has no scorable words")
        _accumulate(accumulators["raw_asr"], _score(reference_text, raw_text))
        _accumulate(accumulators["final"], _score(reference_text, final_text))

    if sample_counts["speech"] == 0:
        raise ValueError("Pilot manifest requires at least one speech sample")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_tree": {
            **git_identity(PROJECT_ROOT),
            "sha256": source_tree_sha256(PROJECT_ROOT),
        },
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "evidence": evidence,
        "samples": sample_counts,
        "raw_asr": _finish(accumulators["raw_asr"]),
        "final": _finish(accumulators["final"]),
        "hallucinated_words": hallucinated_words,
        "timestamp_violations": {
            "count": timestamp_violation_count,
            "samples": timestamp_violation_samples,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Manifest 1.0 (paths are relative to the manifest; do not embed transcripts):
{
  "schema_version": "1.0",
  "samples": [
    {"id": "speech-001", "kind": "speech", "reference": "refs/001.txt", "transcript": "results/001.json", "audio": "audio/001.wav"},
    {"id": "silence-001", "kind": "nonspeech", "transcript": "results/silence-001.json"},
    {"id": "terms-001", "kind": "diagnostic", "reference": "refs/terms.txt", "transcript": "results/terms.json", "audio": "audio/terms.wav"}
  ]
}

Example:
  python scripts/run_pilot_benchmark.py --manifest pilot/manifest.json --output pilot/report.json
""",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = build_report(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
