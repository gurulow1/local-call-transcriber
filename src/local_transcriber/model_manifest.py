"""Validation of a local, pinned ASR model bundle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ModelValidationError

EXPECTED_MANIFEST_SCHEMA = "1.0"


@dataclass(frozen=True)
class Artifact:
    """One immutable file in the model bundle."""

    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ModelManifest:
    """Validated model identity and expected local artifacts."""

    name: str
    version: str
    source_revision: str
    source_code_revision: str
    artifacts: dict[str, Artifact]

    @classmethod
    def load(cls, model_dir: Path, *, expected_name: str | None = None) -> "ModelManifest":
        manifest_path = model_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ModelValidationError(
                f"Local model manifest is missing: {manifest_path}. "
                "Prepare the model bundle before transcription."
            )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelValidationError(f"Cannot read local model manifest: {exc}") from exc

        if not isinstance(payload, dict):
            raise ModelValidationError("Model manifest root must be a JSON object")
        if payload.get("schema_version") != EXPECTED_MANIFEST_SCHEMA:
            raise ModelValidationError(
                f"Unsupported model manifest schema: {payload.get('schema_version')!r}"
            )
        name = cls._required_string(payload, "name")
        if expected_name is not None and name != expected_name:
            raise ModelValidationError(f"Unexpected model name: {payload.get('name')!r}")

        version = cls._required_string(payload, "version")
        source_revision = cls._required_string(payload, "source_revision")
        source_code_revision = cls._required_string(payload, "source_code_revision")
        raw_files = payload.get("files")
        if not isinstance(raw_files, dict) or not raw_files:
            raise ModelValidationError("Model manifest must contain a non-empty 'files' object")

        artifacts: dict[str, Artifact] = {}
        for filename, details in raw_files.items():
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ModelValidationError(f"Unsafe model artifact name: {filename!r}")
            if not isinstance(details, dict):
                raise ModelValidationError(f"Invalid manifest entry for {filename!r}")
            size_bytes = details.get("size_bytes")
            sha256 = details.get("sha256")
            if not isinstance(size_bytes, int) or size_bytes <= 0:
                raise ModelValidationError(f"Invalid size for model artifact {filename!r}")
            if (
                not isinstance(sha256, str)
                or len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256)
            ):
                raise ModelValidationError(f"Invalid SHA-256 for model artifact {filename!r}")
            artifacts[filename] = Artifact(filename, size_bytes, sha256)

        return cls(
            name=name,
            version=version,
            source_revision=source_revision,
            source_code_revision=source_code_revision,
            artifacts=artifacts,
        )

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ModelValidationError(f"Model manifest field {key!r} must be a non-empty string")
        return value.strip()

    def validate_artifact(self, model_dir: Path, filename: str, *, verify_hash: bool) -> Path:
        artifact = self.artifacts.get(filename)
        if artifact is None:
            raise ModelValidationError(f"Model manifest does not declare required file: {filename}")
        path = model_dir / filename
        if path.is_symlink() or not path.is_file():
            raise ModelValidationError(f"Required local model file is missing or not regular: {path}")
        actual_size = path.stat().st_size
        if actual_size != artifact.size_bytes:
            raise ModelValidationError(
                f"Size mismatch for {filename}: expected {artifact.size_bytes}, got {actual_size}"
            )
        if verify_hash:
            actual_hash = sha256_file(path)
            if actual_hash != artifact.sha256:
                raise ModelValidationError(
                    f"SHA-256 mismatch for {filename}: expected {artifact.sha256}, got {actual_hash}"
                )
        return path


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a local artifact without reading it all into RAM."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
