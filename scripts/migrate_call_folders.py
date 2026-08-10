#!/usr/bin/env python3
"""Move legacy flat results into one self-contained folder per call."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from local_transcriber.markdown_output import render_transcript_markdown  # noqa: E402
from local_transcriber.service import SUPPORTED_EXTENSIONS, extract_call_id  # noqa: E402


@dataclass(frozen=True)
class CallMigration:
    call_id: str
    audio_source: Path
    audio_destination: Path
    json_source: Path
    json_destination: Path
    markdown_destination: Path
    legacy_files: tuple[tuple[Path, Path], ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate data/input + data/output into data/calls/CALL_ID folders",
    )
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "data" / "input")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "output")
    parser.add_argument("--calls-dir", type=Path, default=PROJECT_ROOT / "data" / "calls")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "queue.sqlite3")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the migration; without this flag only a preflight summary is printed",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    migrations = _build_plan(args.input_dir, args.output_dir, args.calls_dir)
    print(f"calls={len(migrations)} mode={'apply' if args.apply else 'preview'}")
    for migration in migrations:
        print(
            f"call_id={migration.call_id} audio={migration.audio_source.name} "
            f"legacy_files={len(migration.legacy_files)}"
        )
    if not args.apply:
        return 0

    moved_hashes: dict[str, str] = {}
    for migration in migrations:
        migration.audio_destination.parent.mkdir(parents=True, exist_ok=True)
        original_hash = _sha256(migration.audio_source)
        os.replace(migration.audio_source, migration.audio_destination)
        moved_hash = _sha256(migration.audio_destination)
        if moved_hash != original_hash:
            raise RuntimeError(f"Audio integrity check failed after moving {migration.call_id}")
        moved_hashes[migration.call_id] = moved_hash

        os.replace(migration.json_source, migration.json_destination)
        payload = _load_completed_result(migration.json_destination, migration.call_id)
        _atomic_write_text(
            migration.markdown_destination,
            render_transcript_markdown(payload),
        )
        for source, destination in migration.legacy_files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)

    _update_completed_queue_paths(args.database, migrations)
    print(f"migrated={len(migrations)} verified_audio_hashes={len(moved_hashes)}")
    return 0


def _build_plan(input_dir: Path, output_dir: Path, calls_dir: Path) -> list[CallMigration]:
    if input_dir.is_symlink() or output_dir.is_symlink() or calls_dir.is_symlink():
        raise SystemExit("Input, output and calls directories must not be symlinks")
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    calls_dir.mkdir(parents=True, exist_ok=True)

    migrations: list[CallMigration] = []
    seen: set[str] = set()
    for audio_source in sorted(input_dir.iterdir()):
        if audio_source.is_symlink() or not audio_source.is_file():
            continue
        if audio_source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        call_id = extract_call_id(audio_source)
        if call_id in seen:
            raise SystemExit(f"Duplicate call_id in legacy input: {call_id}")
        seen.add(call_id)
        json_source = output_dir / f"{call_id}.json"
        if json_source.is_symlink() or not json_source.is_file():
            raise SystemExit(f"Completed JSON is missing for {call_id}")
        _load_completed_result(json_source, call_id)

        call_dir = calls_dir / call_id
        audio_destination = call_dir / audio_source.name
        json_destination = call_dir / f"{call_id}.json"
        markdown_destination = call_dir / f"{call_id}.md"
        legacy_files: list[tuple[Path, Path]] = []
        for candidate in sorted(output_dir.glob(f"{call_id}.*")):
            if candidate == json_source or candidate.name in {".DS_Store", ".gitkeep"}:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise SystemExit(f"Legacy artifact is not a regular file: {candidate}")
            legacy_files.append((candidate, call_dir / "legacy" / candidate.name))

        targets = [
            audio_destination,
            json_destination,
            markdown_destination,
            *(destination for _source, destination in legacy_files),
        ]
        existing = [path for path in targets if path.exists() or path.is_symlink()]
        if existing:
            raise SystemExit(f"Migration target already exists: {existing[0]}")

        migrations.append(
            CallMigration(
                call_id=call_id,
                audio_source=audio_source,
                audio_destination=audio_destination,
                json_source=json_source,
                json_destination=json_destination,
                markdown_destination=markdown_destination,
                legacy_files=tuple(legacy_files),
            )
        )

    transcript_jsons = {
        path.stem
        for path in output_dir.glob("*.json")
        if path.is_file() and "." not in path.stem
    }
    orphaned = sorted(transcript_jsons - seen)
    if orphaned:
        raise SystemExit(f"Audio is missing for completed JSON: {orphaned[0]}")
    return migrations


def _load_completed_result(path: Path, call_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read transcription JSON for {call_id}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Transcription JSON root must be an object for {call_id}")
    if payload.get("call_id") != call_id or payload.get("status") != "completed":
        raise SystemExit(f"Transcription JSON is not a completed result for {call_id}")
    return payload


def _update_completed_queue_paths(
    database_path: Path,
    migrations: Sequence[CallMigration],
) -> None:
    if not database_path.exists():
        return
    with sqlite3.connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for migration in migrations:
            connection.execute(
                """
                UPDATE jobs SET input_path=?, output_path=?
                WHERE call_id=? AND status='completed'
                """,
                (
                    str(migration.audio_destination.resolve()),
                    str(migration.json_destination.resolve()),
                    migration.call_id,
                ),
            )
        connection.commit()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
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
            destination.write(text)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
