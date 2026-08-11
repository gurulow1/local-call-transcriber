"""Argument parsing for the single-file MVP."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from .service import TranscriptionRequest, transcribe_file

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_whisper_cli_path() -> Path:
    runtime_dir = PROJECT_ROOT / "third_party" / "whisper.cpp"
    executable = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    match = next(runtime_dir.rglob(executable), None) if runtime_dir.is_dir() else None
    return match or runtime_dir / "Release" / executable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe one Russian call locally with pre-provisioned ASR weights",
    )
    parser.add_argument("--input", required=True, type=Path, help="Local WAV/MP3/FLAC/OGG/AAC source")
    parser.add_argument("--output", required=True, type=Path, help="JSON path with the same call_id")
    parser.add_argument(
        "--engine",
        choices=("whisper", "t-one"),
        default="whisper",
        help="whisper large-v3 is the quality default; t-one is the lightweight fallback",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        help="Pre-provisioned local model directory (chosen from --engine by default)",
    )
    parser.add_argument(
        "--whisper-cli",
        type=Path,
        default=default_whisper_cli_path(),
        help="Pinned local whisper.cpp executable",
    )
    parser.add_argument(
        "--decoder",
        choices=("beam_search", "greedy"),
        default="beam_search",
        help="beam_search is the quality default; T-one beam search requires local kenlm.bin",
    )
    parser.add_argument("--txt-output", type=Path, help="Optional human-readable TXT path")
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Optional formatted Markdown path with the same call_id",
    )
    parser.add_argument(
        "--initial-prompt",
        help="Optional domain vocabulary hint, for example: 'БИК, ИНН, НКЦ, клиринг'",
    )
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing result")
    parser.add_argument(
        "--verify-model-hashes",
        action="store_true",
        help="Hash model files before loading (slow for multi-gigabyte models)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model_dir = args.model_dir or PROJECT_ROOT / "models" / (
        "whisper-large-v3" if args.engine == "whisper" else "t-one"
    )
    request = TranscriptionRequest(
        input_path=args.input,
        output_path=args.output,
        model_dir=model_dir,
        decoder=args.decoder,
        engine_name=args.engine,
        whisper_cli_path=args.whisper_cli,
        initial_prompt=args.initial_prompt,
        txt_output_path=args.txt_output,
        markdown_output_path=args.markdown_output,
        overwrite=args.overwrite,
        verify_model_hashes=args.verify_model_hashes,
    )
    result = transcribe_file(request)
    summary = {
        "call_id": result["call_id"],
        "status": result["status"],
        "engine": args.engine,
        "output": str(args.output),
        "processing_seconds": result["processing_seconds"],
        "error": result["error"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1
