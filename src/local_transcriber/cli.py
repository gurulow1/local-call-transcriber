"""Argument parsing for the single-file MVP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .service import TranscriptionRequest, transcribe_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe one Russian call locally with pre-provisioned T-one weights",
    )
    parser.add_argument("--input", required=True, type=Path, help="Local WAV/MP3/FLAC/OGG/AAC source")
    parser.add_argument("--output", required=True, type=Path, help="JSON path with the same call_id")
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "models" / "t-one",
        help="Pre-provisioned local T-one directory (default: models/t-one)",
    )
    parser.add_argument(
        "--decoder",
        choices=("beam_search", "greedy"),
        default="beam_search",
        help="beam_search requires local kenlm.bin; greedy requires only model.onnx",
    )
    parser.add_argument("--txt-output", type=Path, help="Optional human-readable TXT path")
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Optional formatted Markdown path with the same call_id",
    )
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing result")
    parser.add_argument(
        "--verify-model-hashes",
        action="store_true",
        help="Hash model files before loading (slow for the 5.46 GB language model)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = TranscriptionRequest(
        input_path=args.input,
        output_path=args.output,
        model_dir=args.model_dir,
        decoder=args.decoder,
        txt_output_path=args.txt_output,
        markdown_output_path=args.markdown_output,
        overwrite=args.overwrite,
        verify_model_hashes=args.verify_model_hashes,
    )
    result = transcribe_file(request)
    summary = {
        "call_id": result["call_id"],
        "status": result["status"],
        "output": str(args.output),
        "processing_seconds": result["processing_seconds"],
        "error": result["error"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1
