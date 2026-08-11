#!/usr/bin/env python3
"""First-run staging and local worker launch for end-user archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / ".poetry-cache"
RUNTIME_MARKER = CACHE_DIR / "runtime.json"
SETUP_VERSION = "archive-bootstrap-v1"
TONE_COMMIT = "3c5b6c015038173840e62cea99e10cdb1c759116"
TONE_SHA256 = "771a91608873daf0a7cb68f8fdc3dbb9bc029f2e14d3eea0985968f527d70f69"
TONE_URL = f"https://github.com/voicekit-team/T-one/archive/{TONE_COMMIT}.tar.gz"

CORE_PACKAGES = (
    "av==18.0.0",
    "miniaudio==1.61",
)
TONE_PACKAGES = CORE_PACKAGES + (
    "huggingface-hub==0.33.0",
    "numpy==1.26.4",
    "onnxruntime==1.22.0",
    "poetry-core==2.1.1",
    "pyctcdecode==0.5.0",
)


def _run(command: Sequence[str | Path]) -> None:
    printable = [str(item) for item in command]
    completed = subprocess.run(printable, cwd=PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"Command failed with exit code {completed.returncode}: {printable[0]}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(url: str, destination: Path, expected_sha256: str) -> None:
    if destination.is_file() and _sha256(destination) == expected_sha256:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    print(f"Downloading {destination.name}...")
    with urllib.request.urlopen(url) as source, temporary.open("wb") as target:
        shutil.copyfileobj(source, target, 8 * 1024 * 1024)
    actual_sha256 = _sha256(temporary)
    if actual_sha256 != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"SHA-256 mismatch for {destination.name}: {actual_sha256}")
    temporary.replace(destination)


def _prepare_tone_source() -> Path:
    destination = PROJECT_ROOT / "third_party" / "T-one"
    revision_marker = destination / ".local-transcriber-source-revision"
    if revision_marker.is_file() and revision_marker.read_text(encoding="ascii").strip() == TONE_COMMIT:
        return destination

    archive = CACHE_DIR / "downloads" / f"Tone-{TONE_COMMIT}.tar.gz"
    _download_verified(TONE_URL, archive, TONE_SHA256)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE_DIR, prefix="tone-stage-") as stage_name:
        stage = Path(stage_name)
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(stage, filter="data")
        extracted = stage / f"T-one-{TONE_COMMIT}"
        if not (extracted / "pyproject.toml").is_file():
            raise SystemExit("Pinned T-one archive does not contain pyproject.toml")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(extracted), destination)
    revision_marker.write_text(TONE_COMMIT + "\n", encoding="ascii")
    return destination


def nvidia_available() -> bool:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return False
    completed = subprocess.run(
        [executable, "--query-gpu=name", "--format=csv,noheader"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def choose_engine(profile: str) -> str:
    if profile == "windows-auto" and platform.system() == "Windows" and nvidia_available():
        return "whisper"
    return "t-one"


def _imports_work(python: Path, modules: Sequence[str]) -> bool:
    statement = "; ".join(f"import {module}" for module in modules)
    return subprocess.run(
        [str(python), "-c", statement],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _runtime_ready(python: Path, engine: str) -> bool:
    try:
        marker = json.loads(RUNTIME_MARKER.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if marker != {"setup_version": SETUP_VERSION, "engine": engine}:
        return False
    if engine == "whisper":
        runtime = PROJECT_ROOT / "third_party" / "whisper.cpp"
        return (
            (PROJECT_ROOT / "models" / "whisper-large-v3" / "ggml-large-v3.bin").is_file()
            and (PROJECT_ROOT / "models" / "whisper-large-v3" / "manifest.json").is_file()
            and any(runtime.rglob("whisper-cli.exe"))
            and _imports_work(python, ("av", "miniaudio"))
        )
    return (
        (PROJECT_ROOT / "models" / "t-one" / "model.onnx").is_file()
        and (PROJECT_ROOT / "models" / "t-one" / "manifest.json").is_file()
        and _imports_work(python, ("av", "miniaudio", "onnxruntime", "tone"))
    )


def _install_packages(uv: Path, python: Path, packages: Sequence[str]) -> None:
    _run((uv, "pip", "install", "--python", python, "--only-binary", ":all:", *packages))


def _prepare_whisper(uv: Path, python: Path) -> None:
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("The pinned Whisper/CUDA bundle requires Windows x64")
    print("Preparing Whisper large-v3 for NVIDIA (about 3.8 GB)...")
    _install_packages(uv, python, CORE_PACKAGES)
    _run((python, "scripts/prepare_whisper_cpp.py", "--allow-network-download"))
    _run((python, "scripts/verify_model.py", "models/whisper-large-v3"))


def _prepare_tone(uv: Path, python: Path) -> None:
    print("Preparing the cross-platform T-one CPU runtime...")
    source = _prepare_tone_source()
    _install_packages(uv, python, TONE_PACKAGES)
    _run((uv, "pip", "install", "--python", python, "--no-deps", "--no-build-isolation", source))
    _run((python, "scripts/prepare_model.py", "--allow-network-download"))
    _run((python, "scripts/verify_model.py", "models/t-one", "--greedy-only"))


def _write_marker(engine: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_MARKER.write_text(
        json.dumps({"setup_version": SETUP_VERSION, "engine": engine}, indent=2) + "\n",
        encoding="utf-8",
    )


def _open_input_folder() -> None:
    input_dir = PROJECT_ROOT / "data" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system() == "Windows":
            os.startfile(input_dir)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(input_dir)])
        elif opener := shutil.which("xdg-open"):
            subprocess.Popen([opener, str(input_dir)])
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--profile", choices=("windows-auto", "cpu"), required=True)
    args = parser.parse_args()
    uv = args.uv.resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    engine = choose_engine(args.profile)

    if not _runtime_ready(python, engine):
        print("First launch: downloading and verifying the local runtime.")
        if engine == "whisper":
            _prepare_whisper(uv, python)
        else:
            _prepare_tone(uv, python)
        _write_marker(engine)
        print("Installation completed.")

    decoder = "beam_search" if engine == "whisper" else "greedy"
    print(f"Transcriber started: engine={engine}, decoder={decoder}")
    print("Put audio files into data/input. Results will appear in data/calls.")
    print("Press Control+C to stop.")
    _open_input_folder()
    try:
        return subprocess.run(
            [
                str(python),
                str(PROJECT_ROOT / "worker.py"),
                "--mode",
                "poll",
                "--engine",
                engine,
                "--decoder",
                decoder,
            ],
            cwd=PROJECT_ROOT,
            check=False,
        ).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
