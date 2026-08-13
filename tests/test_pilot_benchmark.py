from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from run_pilot_benchmark import build_report, main, source_tree_sha256


class PilotBenchmarkTests(unittest.TestCase):
    def test_source_tree_hash_includes_github_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            workflow = root / ".github" / "workflows" / "tests.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: first\n", encoding="utf-8")
            before = source_tree_sha256(root)

            workflow.write_text("name: second\n", encoding="utf-8")

            self.assertNotEqual(before, source_tree_sha256(root))

    def test_aggregates_quality_hallucinations_and_timestamps_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            references = root / "refs"
            results = root / "results"
            audio = root / "audio"
            references.mkdir()
            results.mkdir()
            audio.mkdir()
            (references / "one.txt").write_text("кот", encoding="utf-8")
            (references / "two.txt").write_text("мама мыла", encoding="utf-8")
            (audio / "one.wav").write_bytes(b"private-audio-payload")
            self._write_result(results / "one.json", raw_text="кит", text="кот")
            self._write_result(results / "two.json", raw_text="мама мыла", text="мама мыла")
            self._write_result(
                results / "silence.json",
                raw_text="ложный текст",
                text="ложный текст",
                duration=10.0,
                segments=[{"start": 0.0, "end": 11.0, "text": "secret-segment"}],
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "samples": [
                            {
                                "id": "speech-one",
                                "kind": "speech",
                                "reference": "refs/one.txt",
                                "transcript": "results/one.json",
                                "audio": "audio/one.wav",
                            },
                            {
                                "id": "speech-two",
                                "kind": "speech",
                                "reference": "refs/two.txt",
                                "transcript": "results/two.json",
                            },
                            {
                                "id": "silence",
                                "kind": "nonspeech",
                                "transcript": "results/silence.json",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output = root / "report.json"

            with patch(
                "run_pilot_benchmark.git_identity",
                return_value={"revision": "test-revision", "dirty": True},
            ):
                self.assertEqual(main(["--manifest", str(manifest), "--output", str(output)]), 0)

            report_text = output.read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(
                {key: report["source_tree"][key] for key in ("revision", "dirty")},
                {"revision": "test-revision", "dirty": True},
            )
            self.assertRegex(report["source_tree"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(report["manifest_sha256"], hashlib.sha256(manifest.read_bytes()).hexdigest())
            evidence = {item["id"]: item for item in report["evidence"]}
            self.assertEqual(
                evidence["speech-one"]["transcript"],
                self._evidence(results / "one.json"),
            )
            self.assertEqual(
                evidence["speech-one"]["reference"],
                self._evidence(references / "one.txt"),
            )
            self.assertEqual(
                evidence["speech-one"]["audio"],
                self._evidence(audio / "one.wav"),
            )
            self.assertNotIn("audio", evidence["speech-two"])
            self.assertNotIn("reference", evidence["silence"])
            self.assertEqual(
                report["samples"],
                {"total": 3, "speech": 2, "nonspeech": 1, "diagnostic": 0},
            )
            self.assertEqual(report["raw_asr"]["micro_word_error_rate"], 0.3333)
            self.assertEqual(report["raw_asr"]["macro_word_error_rate"], 0.5)
            self.assertEqual(report["raw_asr"]["micro_character_error_rate"], 0.0833)
            self.assertEqual(report["raw_asr"]["macro_character_error_rate"], 0.1667)
            self.assertEqual(report["final"]["micro_word_error_rate"], 0.0)
            self.assertEqual(report["final"]["micro_character_error_rate"], 0.0)
            self.assertEqual(report["hallucinated_words"], {"raw_asr": 2, "final": 2})
            self.assertEqual(report["timestamp_violations"], {"count": 1, "samples": 1})
            self.assertTrue(report["generated_at"].endswith("Z"))
            datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
            for private_text in (
                "кот",
                "кит",
                "ложный",
                "secret-segment",
                "private-audio-payload",
            ):
                self.assertNotIn(private_text, report_text)

    def test_diagnostic_evidence_does_not_change_speech_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            (root / "speech.txt").write_text("верно", encoding="utf-8")
            (root / "diagnostic.txt").write_text("четыре ноля", encoding="utf-8")
            (root / "diagnostic.wav").write_bytes(b"diagnostic-audio")
            self._write_result(root / "speech.json", raw_text="верно", text="верно")
            self._write_result(root / "diagnostic.json", raw_text="400", text="400")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "samples": [
                            {
                                "id": "speech",
                                "kind": "speech",
                                "reference": "speech.txt",
                                "transcript": "speech.json",
                            },
                            {
                                "id": "critical-fields",
                                "kind": "diagnostic",
                                "reference": "diagnostic.txt",
                                "transcript": "diagnostic.json",
                                "audio": "diagnostic.wav",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_report(manifest)

            self.assertEqual(report["raw_asr"]["micro_word_error_rate"], 0.0)
            self.assertEqual(report["samples"]["diagnostic"], 1)
            diagnostic = next(item for item in report["evidence"] if item["id"] == "critical-fields")
            self.assertEqual(diagnostic["reference"], self._evidence(root / "diagnostic.txt"))
            self.assertEqual(diagnostic["audio"], self._evidence(root / "diagnostic.wav"))

    def test_content_mutation_changes_evidence_hash_without_requiring_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            reference = root / "reference.txt"
            transcript = root / "transcript.json"
            reference.write_text("кот", encoding="utf-8")
            self._write_result(transcript, raw_text="кот", text="кот")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "samples": [
                            {
                                "id": "speech",
                                "kind": "speech",
                                "reference": "reference.txt",
                                "transcript": "transcript.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            before = build_report(manifest)["evidence"][0]
            reference.write_text("кит", encoding="utf-8")
            self._write_result(transcript, raw_text="кит", text="кит")
            after = build_report(manifest)["evidence"][0]

            for field in ("reference", "transcript"):
                self.assertEqual(before[field]["size_bytes"], after[field]["size_bytes"])
                self.assertNotEqual(before[field]["sha256"], after[field]["sha256"])

    def test_declared_missing_audio_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            (root / "reference.txt").write_text("кот", encoding="utf-8")
            self._write_result(root / "transcript.json", raw_text="кот", text="кот")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "samples": [
                            {
                                "id": "speech",
                                "kind": "speech",
                                "reference": "reference.txt",
                                "transcript": "transcript.json",
                                "audio": "missing.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "'audio' must be a regular file"):
                build_report(manifest)

    @staticmethod
    def _evidence(path: Path) -> dict[str, int | str]:
        content = path.read_bytes()
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }

    @staticmethod
    def _write_result(
        path: Path,
        *,
        raw_text: str,
        text: str,
        duration: float = 1.0,
        segments: list[dict[str, object]] | None = None,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "duration_seconds": duration,
                    "raw_text": raw_text,
                    "text": text,
                    "segments": [] if segments is None else segments,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
