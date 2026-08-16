#!/usr/bin/env python3
"""Build a deterministic source pilot archive without local data or model weights."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0"
ROOT_FILES = (
    ".gitattributes",
    ".gitignore",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "crm_api.py",
    "pyproject.toml",
    "transcribe.py",
    "worker.py",
    "Запустить транскрибатор.cmd",
    "Запустить транскрибатор.command",
    "Запустить транскрибатор.sh",
    "Проверить готовность.cmd",
)
SOURCE_DIRS = (
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
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
ZIP_TIMESTAMP = (2026, 8, 14, 0, 0, 0)


def collect_files(project_root: Path = PROJECT_ROOT) -> list[Path]:
    root_files = [project_root / name for name in ROOT_FILES]
    missing = [str(path.relative_to(project_root)) for path in root_files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required release files are missing: {', '.join(missing)}")
    files = list(root_files)
    for directory in SOURCE_DIRS:
        root = project_root / directory
        if root.is_dir():
            files.extend(root.rglob("*"))
    selected = [
        path
        for path in files
        if path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in EXCLUDED_SUFFIXES
        and not {"whisper-large-v3", "whisper-vad", "t-one"}.intersection(
            path.relative_to(project_root).parts
        )
    ]
    return sorted(set(selected), key=lambda path: path.relative_to(project_root).as_posix())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_archive(output: Path, project_root: Path = PROJECT_ROOT) -> dict[str, object]:
    output = output.resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    inventory: list[dict[str, object]] = []
    for path in collect_files(project_root):
        relative = path.relative_to(project_root).as_posix()
        content = path.read_bytes()
        entries.append((relative, content))
        inventory.append(
            {"path": relative, "size_bytes": len(content), "sha256": _sha256(content)}
        )
    manifest = {
        "schema_version": "1.0",
        "product": "local-call-transcriber",
        "version": VERSION,
        "contains_audio_or_model_weights": False,
        "files": inventory,
    }
    entries.append(
        (
            "RELEASE_MANIFEST.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in sorted(entries):
            info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o100755 if Path(relative).suffix in {".sh", ".command"} else 0o100644
            info.external_attr = mode << 16
            archive.writestr(info, content, compresslevel=9)
    digest = _sha256(output.read_bytes())
    checksum_path = output.with_suffix(output.suffix + ".sha256")
    checksum_path.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return {"archive": str(output), "sha256": digest, "files": len(inventory)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "dist" / f"local-call-transcriber-{VERSION}-pilot.zip",
    )
    args = parser.parse_args(argv)
    print(json.dumps(build_archive(args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
