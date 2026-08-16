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
VERSION = "0.3.0"
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


def _read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read evidence JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Evidence JSON root must be an object: {path.name}")
    return payload


def _validate_evidence(source: Path) -> None:
    manifest_path = source / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_object(manifest_path)
    report = _read_object(source / "report.json")
    if report.get("manifest_sha256") != _sha256(manifest_bytes):
        raise ValueError("Evidence report does not match manifest.json")

    samples = manifest.get("samples")
    reported = report.get("evidence")
    if not isinstance(samples, list) or not isinstance(reported, list):
        raise ValueError("Evidence manifest/report sample lists are missing")
    reported_by_id = {
        item.get("id"): item for item in reported if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(reported_by_id) != len(reported):
        raise ValueError("Evidence report contains invalid or duplicate sample ids")

    seen_ids: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(sample.get("id"), str):
            raise ValueError("Evidence manifest contains an invalid sample")
        sample_id = sample["id"]
        if sample_id in seen_ids or sample_id not in reported_by_id:
            raise ValueError(f"Evidence sample id is duplicate or unreported: {sample_id}")
        seen_ids.add(sample_id)
        report_item = reported_by_id[sample_id]
        for field in ("audio", "reference", "transcript"):
            if field not in sample:
                continue
            value = sample[field]
            if not isinstance(value, str) or not value or Path(value).is_absolute():
                raise ValueError(f"Evidence sample {sample_id!r} has an invalid {field} path")
            candidate = source / value
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"Evidence sample {sample_id!r} {field} must be a regular file")
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(source)
            except ValueError as exc:
                raise ValueError(
                    f"Evidence sample {sample_id!r} {field} escapes the package"
                ) from exc
            content = resolved.read_bytes()
            expected = report_item.get(field)
            actual = {"sha256": _sha256(content), "size_bytes": len(content)}
            if expected != actual:
                raise ValueError(f"Evidence report hash mismatch for {sample_id!r} {field}")


def build_archive(source: Path, output: Path) -> dict[str, object]:
    source = source.resolve(strict=True)
    output = output.resolve(strict=False)
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("Evidence archive output must be outside the evidence directory")

    files = collect_files(source)
    _validate_evidence(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    inventory: list[dict[str, object]] = []
    for path in files:
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
