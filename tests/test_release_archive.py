from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from build_pilot_archive import build_archive


class PilotArchiveTests(unittest.TestCase):
    def test_archive_is_deterministic_and_excludes_private_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"
            second = Path(directory) / "second.zip"
            first_result = build_archive(first)
            second_result = build_archive(second)

            self.assertEqual(first_result["sha256"], second_result["sha256"])
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("RELEASE_MANIFEST.json"))
                self.assertIn("README.md", names)
                self.assertIn(".github/workflows/tests.yml", names)
                self.assertIn("scripts/bootstrap_windows.ps1", names)
                self.assertIn("models/whisper-vad.manifest.example.json", names)
                self.assertFalse(any(name.startswith(("data/", "logs/", ".venv/")) for name in names))
                self.assertFalse(any(name.endswith((".bin", ".onnx", ".wav", ".aac")) for name in names))
                self.assertEqual(
                    archive.getinfo("Запустить транскрибатор.sh").external_attr >> 16,
                    0o100755,
                )
                for item in manifest["files"]:
                    content = archive.read(item["path"])
                    self.assertEqual(item["size_bytes"], len(content))
                    self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
