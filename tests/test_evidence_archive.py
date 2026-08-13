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

from build_pilot_evidence import build_archive


class PilotEvidenceArchiveTests(unittest.TestCase):
    def test_archive_is_deterministic_attributed_and_self_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "ATTRIBUTION.md").write_text("source and license\n", encoding="utf-8")
            audio = b"open-test-audio"
            (evidence / "audio.wav").write_bytes(audio)
            manifest = json.dumps(
                {
                    "schema_version": "1.0",
                    "samples": [{"id": "speech", "audio": "audio.wav"}],
                }
            ).encode()
            (evidence / "manifest.json").write_bytes(manifest)
            (evidence / "report.json").write_text(
                json.dumps(
                    {
                        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                        "evidence": [
                            {
                                "id": "speech",
                                "audio": {
                                    "sha256": hashlib.sha256(audio).hexdigest(),
                                    "size_bytes": len(audio),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (evidence / ".tmp").mkdir()
            (evidence / ".tmp" / "scratch.json").write_text("private scratch")

            first = root / "first.zip"
            second = root / "second.zip"
            first_result = build_archive(evidence, first)
            second_result = build_archive(evidence, second)

            self.assertEqual(first_result["sha256"], second_result["sha256"])
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
                self.assertIn("ATTRIBUTION.md", names)
                self.assertIn("audio.wav", names)
                self.assertNotIn(".tmp/scratch.json", names)
                manifest = json.loads(archive.read("EVIDENCE_PACKAGE_MANIFEST.json"))
                for item in manifest["files"]:
                    content = archive.read(item["path"])
                    self.assertEqual(item["size_bytes"], len(content))
                    self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())

            (evidence / "audio.wav").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                build_archive(evidence, root / "stale.zip")

    def test_attribution_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "manifest.json").write_text("{}", encoding="utf-8")
            (evidence / "report.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, "ATTRIBUTION.md"):
                build_archive(evidence, root / "evidence.zip")


if __name__ == "__main__":
    unittest.main()
