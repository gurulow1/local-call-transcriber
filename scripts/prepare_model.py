#!/usr/bin/env python3
"""Stage pinned T-one artifacts; never import or call this from runtime code."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.model_manifest import sha256_file  # noqa: E402

REPO_ID = "t-tech/T-one"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicit staging-only download of pinned T-one model files",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=PROJECT_ROOT / "models" / "t-one",
        help="Local bundle directory",
    )
    parser.add_argument(
        "--include-kenlm",
        action="store_true",
        help="Also fetch the 5,463,477,004-byte language model for beam search",
    )
    parser.add_argument(
        "--allow-network-download",
        action="store_true",
        help="Required acknowledgement: this staging command uses Hugging Face",
    )
    args = parser.parse_args()
    if not args.allow_network_download:
        parser.error("refusing network staging without --allow-network-download")

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit("huggingface-hub is not installed in the staging environment") from exc

    manifest_source = PROJECT_ROOT / "models" / "manifest.example.json"
    manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
    revision = manifest["source_revision"]
    target = args.target.resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    filenames = ["model.onnx"]
    if args.include_kenlm:
        filenames.append("kenlm.bin")

    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    for filename in filenames:
        expected = manifest["files"][filename]
        print(f"staging {filename} from {REPO_ID}@{revision}")
        downloaded = Path(
            hf_hub_download(
                repo_id=REPO_ID,
                filename=filename,
                revision=revision,
                local_dir=target,
            )
        )
        actual_size = downloaded.stat().st_size
        if actual_size != expected["size_bytes"]:
            raise SystemExit(
                f"size mismatch for {filename}: expected {expected['size_bytes']}, got {actual_size}"
            )
        actual_hash = sha256_file(downloaded)
        if actual_hash != expected["sha256"]:
            raise SystemExit(
                f"SHA-256 mismatch for {filename}: expected {expected['sha256']}, got {actual_hash}"
            )
        print(f"verified {filename}: {actual_hash}")

    # Publish the identity only after all requested artifacts have verified.
    shutil.copyfile(manifest_source, target / "manifest.json")
    print(f"prepared local bundle: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

