#!/usr/bin/env python3
"""Offline readiness check for the local pilot runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MARKER = PROJECT_ROOT / ".poetry-cache" / "runtime.json"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.cli import default_vad_model_path, default_whisper_cli_path  # noqa: E402
from local_transcriber.model_manifest import ModelManifest  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _manifest_check(
    name: str,
    model_dir: Path,
    expected_name: str,
    filenames: Sequence[str],
) -> Check:
    try:
        manifest = ModelManifest.load(model_dir, expected_name=expected_name)
        for filename in filenames:
            manifest.validate_artifact(model_dir, filename, verify_hash=True)
    except Exception as exc:
        return Check(name, "FAIL", " ".join(str(exc).split())[:300])
    return Check(name, "PASS", f"{manifest.version}; SHA-256 OK")


def _writable_check() -> Check:
    try:
        for path in (PROJECT_ROOT / "data" / "input", PROJECT_ROOT / "data" / "calls"):
            path.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(dir=path, prefix=".preflight-")
            os.close(descriptor)
            Path(temporary_name).unlink()
    except OSError as exc:
        return Check("storage", "FAIL", f"data folders are not writable: {type(exc).__name__}")
    free_gib = shutil.disk_usage(PROJECT_ROOT).free / 1024**3
    status = "PASS" if free_gib >= 5 else "WARN"
    return Check("storage", status, f"writable; {free_gib:.1f} GiB free")


def _whisper_cli_check(path: Path) -> Check:
    if path.is_symlink() or not path.is_file():
        return Check("whisper.cpp", "FAIL", f"CLI is missing: {path}")
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("whisper.cpp", "FAIL", f"CLI cannot start: {type(exc).__name__}")
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or "version: 1.9.2" not in output:
        return Check("whisper.cpp", "FAIL", "expected pinned version 1.9.2")
    return Check("whisper.cpp", "PASS", "version 1.9.2; local runtime starts")


def _gpu_check() -> Check:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return Check("NVIDIA GPU", "FAIL", "nvidia-smi is unavailable")
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        first = completed.stdout.strip().splitlines()[0]
        gpu_name, memory_mib, driver = [item.strip() for item in first.split(",", 2)]
        memory = int(memory_mib)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        return Check("NVIDIA GPU", "FAIL", f"GPU probe failed: {type(exc).__name__}")
    if completed.returncode != 0 or memory < 6 * 1024:
        return Check("NVIDIA GPU", "FAIL", f"{gpu_name}; {memory} MiB VRAM is insufficient")
    return Check("NVIDIA GPU", "PASS", f"{gpu_name}; {memory} MiB VRAM; driver {driver}")


def _firewall_check() -> Check:
    if platform.system() != "Windows":
        return Check("firewall", "WARN", "OS rule check is implemented for Windows only")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = PROJECT_ROOT / "security" / "windows-deny-network.ps1"
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-CheckOnly",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("firewall", "FAIL", f"rule check failed: {type(exc).__name__}")
    if completed.returncode != 0:
        return Check("firewall", "FAIL", "outbound deny rules are missing or do not match runtime paths")
    return Check("firewall", "PASS", "outbound deny rules match local runtime executables")


def _imports_check(engine: str) -> Check:
    modules = ("av", "miniaudio") if engine == "whisper" else (
        "av",
        "miniaudio",
        "onnxruntime",
        "tone",
    )
    missing: list[str] = []
    for module in modules:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        return Check("runtime imports", "FAIL", f"missing: {', '.join(missing)}")
    return Check("runtime imports", "PASS", ", ".join(modules))


def detected_engine() -> str:
    try:
        marker = json.loads(RUNTIME_MARKER.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "whisper"
    engine = marker.get("engine") if isinstance(marker, dict) else None
    return engine if engine in {"whisper", "t-one"} else "whisper"


def run_checks(engine: str, *, check_firewall: bool = True) -> list[Check]:
    checks = [
        Check("Python", "PASS", platform.python_version()),
        _writable_check(),
        _imports_check(engine),
    ]
    if engine == "whisper":
        checks.extend(
            [
                _manifest_check(
                    "Whisper model",
                    PROJECT_ROOT / "models" / "whisper-large-v3",
                    "Whisper large-v3",
                    ("ggml-large-v3.bin",),
                ),
                _manifest_check(
                    "Silero VAD",
                    default_vad_model_path().parent,
                    "Silero VAD",
                    (default_vad_model_path().name,),
                ),
                _whisper_cli_check(default_whisper_cli_path()),
                _gpu_check(),
            ]
        )
    else:
        checks.append(
            _manifest_check(
                "T-one model",
                PROJECT_ROOT / "models" / "t-one",
                "T-one",
                ("model.onnx",),
            )
        )
    if check_firewall:
        checks.append(_firewall_check())
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("whisper", "t-one"), default=detected_engine())
    parser.add_argument("--skip-firewall", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    checks = run_checks(args.engine, check_firewall=not args.skip_firewall)
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {"engine": args.engine, "checks": [asdict(check) for check in checks]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if any(check.status == "FAIL" for check in checks):
        print("\nNO-GO: исправьте проверки FAIL до демонстрации.")
        return 1
    print("\nGO: локальный runtime готов к пилотной демонстрации.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
