#!/usr/bin/env python3
"""Build a deterministic, self-contained pilot evidence archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.0"
DEFAULT_SOURCE = PROJECT_ROOT / "data" / "output" / "pilot-acceptance"
REQUIRED_FILES = ("ATTRIBUTION.md", "manifest.json", "report.json")
ZIP_TIMESTAMP = (2026, 8, 14, 0, 0, 0)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def collect_files(source: Path) -> list[Path]:
    if source.is_symlink():
        raise ValueError("Evidence directory must not be a symlink")
    source = source.resolve(strict=True)
    missing = [name for name in REQUIRED_FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Evidence directory is missing: {', '.join(missing)}")

    files: list[Path] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".tmp" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"Evidence archive refuses symlink: {relative.as_posix()}")
        if path.is_file() and path.suffix.lower() not in {".pyc", ".pyo"}:
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(source).as_posix())


def build_archive(source: Path, output: Path) -> dict[str, object]:
    source = source.resolve(strict=True)
    output = output.resolve(strict=False)
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("Evidence archive output must be outside the evidence directory")

    output.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    inventory: list[dict[str, object]] = []
    for path in collect_files(source):
        relative = path.relative_to(source).as_posix()
        content = path.read_bytes()
        entries.append((relative, content))
        inventory.append(
            {"path": relative, "size_bytes": len(content), "sha256": _sha256(content)}
        )

    package_manifest = {
        "schema_version": "1.0",
        "package": "local-call-transcriber-pilot-evidence",
        "version": VERSION,
        "contains_audio": True,
        "files": inventory,
    }
    entries.append(
        (
            "EVIDENCE_PACKAGE_MANIFEST.json",
            (json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in sorted(entries):
            info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compresslevel=9)

    digest = _sha256(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii"
    )
    return {"archive": str(output), "sha256": digest, "files": len(inventory)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "dist" / f"local-call-transcriber-{VERSION}-pilot-evidence.zip",
    )
    args = parser.parse_args(argv)
    print(json.dumps(build_archive(args.source, args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
