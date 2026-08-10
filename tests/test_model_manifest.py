from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.errors import ModelValidationError
from local_transcriber.model_manifest import ModelManifest


class ModelManifestTests(unittest.TestCase):
    def test_declared_local_artifact_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            model_dir = Path(temporary_dir)
            artifact = model_dir / "model.onnx"
            artifact.write_bytes(b"local-model")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            manifest = {
                "schema_version": "1.0",
                "name": "T-one",
                "version": "test",
                "source_revision": "model-revision",
                "source_code_revision": "code-revision",
                "files": {
                    "model.onnx": {
                        "size_bytes": artifact.stat().st_size,
                        "sha256": digest,
                    }
                },
            }
            (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            loaded = ModelManifest.load(model_dir)
            self.assertEqual(
                loaded.validate_artifact(model_dir, "model.onnx", verify_hash=True),
                artifact,
            )

    def test_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            model_dir = Path(temporary_dir)
            artifact = model_dir / "model.onnx"
            artifact.write_bytes(b"local-model")
            manifest = {
                "schema_version": "1.0",
                "name": "T-one",
                "version": "test",
                "source_revision": "model-revision",
                "source_code_revision": "code-revision",
                "files": {
                    "model.onnx": {
                        "size_bytes": artifact.stat().st_size,
                        "sha256": "0" * 64,
                    }
                },
            }
            (model_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(ModelValidationError):
                ModelManifest.load(model_dir).validate_artifact(
                    model_dir,
                    "model.onnx",
                    verify_hash=True,
                )


if __name__ == "__main__":
    unittest.main()

