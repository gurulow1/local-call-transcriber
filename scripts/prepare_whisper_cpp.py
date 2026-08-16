#!/usr/bin/env python3
"""Stage pinned whisper.cpp CUDA runtime and large-v3 weights."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.2/"
    "whisper-cublas-12.4.0-bin-x64.zip"
)
RUNTIME_SIZE = 670_611_449
RUNTIME_SHA256 = "443110ddaad70d4290ab2e77179e31cf712035bbc4fad56bb4519a90c917b39c"
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
    "362722b3fdcd2300b58a8286933ead1c48619667/ggml-large-v3.bin"
)
MODEL_SIZE = 3_095_033_483
MODEL_SHA256 = "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2"
VAD_URL = (
    "https://huggingface.co/ggml-org/whisper-vad/resolve/"
    "e5614ed76a5dd4b03fad5068c89efcd2617a9d1e/ggml-silero-v5.1.2.bin"
)
VAD_SIZE = 885_098
VAD_SHA256 = "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf"
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def _download(url: str, destination: Path, size: int, sha256: str) -> None:
    if destination.is_file() and destination.stat().st_size == size:
        if _sha256(destination) == sha256:
            print(f"already verified: {destination}", flush=True)
            return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    if temporary.is_file() and temporary.stat().st_size >= size:
        if temporary.stat().st_size == size and _sha256(temporary) == sha256:
            temporary.replace(destination)
            print(f"verified: {destination}", flush=True)
            return
        temporary.unlink()

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            copied = temporary.stat().st_size if temporary.is_file() else 0
            headers = {"User-Agent": "local-call-transcriber/0.3"}
            if copied:
                headers["Range"] = f"bytes={copied}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as source:
                status = getattr(source, "status", None)
                if copied and status != 206:
                    copied = 0
                mode = "ab" if copied else "wb"
                next_report = ((copied // (256 * 1024 * 1024)) + 1) * (256 * 1024 * 1024)
                with temporary.open(mode) as target:
                    while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                        target.write(chunk)
                        copied += len(chunk)
                        if copied >= next_report:
                            print(
                                f"downloaded {copied / 1024 / 1024:.0f} MiB: {destination.name}",
                                flush=True,
                            )
                            next_report += 256 * 1024 * 1024
            if temporary.stat().st_size != size:
                raise OSError(f"size mismatch for {destination.name}")
            actual_hash = _sha256(temporary)
            if actual_hash != sha256:
                temporary.unlink()
                raise OSError(f"SHA-256 mismatch for {destination.name}: {actual_hash}")
            break
        except (OSError, http.client.HTTPException) as error:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise SystemExit(
                    f"download failed after {DOWNLOAD_ATTEMPTS} attempts for {destination.name}: {error}"
                ) from error
            print(
                f"download interrupted; retrying {destination.name} "
                f"({attempt}/{DOWNLOAD_ATTEMPTS})...",
                flush=True,
            )
    temporary.replace(destination)
    print(f"verified: {destination}", flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (root / member.filename).resolve()
            if root not in target.parents and target != root:
                raise SystemExit(f"unsafe path in runtime archive: {member.filename}")
        package.extractall(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-network-download", action="store_true")
    parser.add_argument(
        "--runtime-target",
        type=Path,
        default=PROJECT_ROOT / "third_party" / "whisper.cpp",
    )
    parser.add_argument(
        "--model-target",
        type=Path,
        default=PROJECT_ROOT / "models" / "whisper-large-v3",
    )
    parser.add_argument(
        "--vad-target",
        type=Path,
        default=PROJECT_ROOT / "models" / "whisper-vad",
    )
    args = parser.parse_args()
    if not args.allow_network_download:
        parser.error("refusing network staging without --allow-network-download")
    if os.name != "nt":
        parser.error("the pinned CUDA runtime bundle is currently supported only on Windows x64")

    runtime_target = args.runtime_target.resolve(strict=False)
    model_target = args.model_target.resolve(strict=False)
    vad_target = args.vad_target.resolve(strict=False)
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT, prefix=".whisper-stage-") as staging:
        runtime_archive = Path(staging) / "whisper-runtime.zip"
        _download(RUNTIME_URL, runtime_archive, RUNTIME_SIZE, RUNTIME_SHA256)
        _safe_extract(runtime_archive, runtime_target)

    model_path = model_target / "ggml-large-v3.bin"
    _download(MODEL_URL, model_path, MODEL_SIZE, MODEL_SHA256)
    shutil.copyfile(
        PROJECT_ROOT / "models" / "whisper-large-v3.manifest.example.json",
        model_target / "manifest.json",
    )
    vad_path = vad_target / "ggml-silero-v5.1.2.bin"
    _download(VAD_URL, vad_path, VAD_SIZE, VAD_SHA256)
    shutil.copyfile(
        PROJECT_ROOT / "models" / "whisper-vad.manifest.example.json",
        vad_target / "manifest.json",
    )
    executable = next(runtime_target.rglob("whisper-cli.exe"), None)
    if executable is None:
        raise SystemExit("whisper-cli.exe was not found in the verified runtime archive")
    print(f"runtime: {executable}")
    print(f"model: {model_target}")
    print(f"vad: {vad_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
