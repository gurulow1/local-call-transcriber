#!/usr/bin/env python3
"""Verify a prepared T-one model bundle without network access."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.model_manifest import ModelManifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--greedy-only", action="store_true", help="Do not require kenlm.bin")
    args = parser.parse_args()

    model_dir = args.model_dir.resolve(strict=False)
    manifest = ModelManifest.load(model_dir)
    filenames = ["model.onnx"] if args.greedy_only else ["model.onnx", "kenlm.bin"]
    for filename in filenames:
        manifest.validate_artifact(model_dir, filename, verify_hash=True)
        print(f"verified {filename}")
    print(f"model={manifest.name} version={manifest.version} revision={manifest.source_revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

