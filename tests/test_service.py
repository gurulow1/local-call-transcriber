from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.engine import EngineModelInfo, EngineResult, Segment
from local_transcriber.errors import InferenceError
from local_transcriber.service import SCHEMA_VERSION, TranscriptionRequest, transcribe_file


class FakeEngine:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    @property
    def model_info(self) -> EngineModelInfo:
        return EngineModelInfo(
            name="T-one",
            version="test-version",
            source_revision="test-model-revision",
            source_code_revision="test-code-revision",
            decoder="greedy",
            local_path="models/test",
        )

    def transcribe(self, input_path: Path) -> EngineResult:
        if self._fail:
            raise InferenceError("synthetic inference failure")
        return EngineResult(
            duration_seconds=2.5,
            segments=(
                Segment(start=0.1, end=0.8, text="тестовая"),
                Segment(start=1.0, end=2.1, text="запись"),
            ),
        )


class TranscriptionServiceTests(unittest.TestCase):
    def test_successful_result_is_atomic_and_source_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"synthetic-audio-placeholder")
            original_bytes = source.read_bytes()
            original_stat = source.stat()
            output = root / "output" / "1234.json"
            markdown_output = root / "output" / "1234.md"

            result = transcribe_file(
                TranscriptionRequest(
                    source,
                    output,
                    root / "models",
                    decoder="greedy",
                    markdown_output_path=markdown_output,
                ),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(result["call_id"], "1234")
            self.assertEqual(result["source_audio"], "1234.wav")
            self.assertEqual(result["language"], "ru")
            self.assertEqual(result["duration_seconds"], 2.5)
            self.assertEqual(result["text"], "Тестовая. Запись.")
            self.assertEqual(result["raw_text"], "тестовая запись")
            self.assertEqual(len(result["segments"]), 2)
            self.assertEqual(result["segments"][0]["asr_text"], "тестовая")
            self.assertEqual(result["segments"][0]["text"], "Тестовая.")
            self.assertEqual(result["postprocessing"]["method"], "deterministic_glossary_v1")
            self.assertEqual(result["model"]["name"], "T-one")
            self.assertIsNone(result["error"])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
            markdown = markdown_output.read_text(encoding="utf-8")
            self.assertIn("# Расшифровка: 1234", markdown)
            self.assertIn("[Открыть исходное аудио](./1234.wav)", markdown)
            self.assertIn("Тестовая. Запись.", markdown)
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(source.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_common_compressed_extensions_are_accepted(self) -> None:
        for extension in (".mp3", ".aac"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary_dir:
                root = Path(temporary_dir)
                source = root / f"call-42{extension}"
                source.write_bytes(b"synthetic-compressed-placeholder")
                output = root / "call-42.json"

                result = transcribe_file(
                    TranscriptionRequest(source, output, root / "models", decoder="greedy"),
                    engine=FakeEngine(),
                )

                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["call_id"], "call-42")

    def test_output_name_must_match_call_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "different.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "OutputValidationError")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "failed")

    def test_unsafe_call_id_fails_without_invoking_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "звонок 1.wav"
            source.write_bytes(b"placeholder")
            output = root / "звонок 1.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "InputValidationError")

    def test_existing_result_is_not_overwritten_implicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"
            output.write_text("existing-result", encoding="utf-8")

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(output.read_text(encoding="utf-8"), "existing-result")

    def test_markdown_symlink_is_not_followed_during_explicit_reprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"
            protected = root / "protected.md"
            protected.write_text("do-not-overwrite", encoding="utf-8")
            markdown_output = root / "1234.md"
            try:
                markdown_output.symlink_to(protected)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable on this platform")

            result = transcribe_file(
                TranscriptionRequest(
                    source,
                    output,
                    root / "models",
                    markdown_output_path=markdown_output,
                    overwrite=True,
                ),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "OutputValidationError")
            self.assertEqual(protected.read_text(encoding="utf-8"), "do-not-overwrite")

    def test_failed_explicit_reprocessing_preserves_previous_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"
            output.write_text("previous-completed-result", encoding="utf-8")

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models", overwrite=True),
                engine=FakeEngine(fail=True),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous-completed-result")

    def test_inference_failure_is_serialized_without_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.flac"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(fail=True),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "InferenceError")
            self.assertEqual(result["text"], "")
            self.assertEqual(result["raw_text"], "")
            self.assertEqual(result["segments"], [])
            self.assertIsNone(result["postprocessing"])
            self.assertNotIn("тестовая", output.read_text(encoding="utf-8"))

    def test_failed_envelope_can_be_refreshed_on_explicit_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"
            output.write_text(
                json.dumps({"call_id": "1234", "status": "failed", "error": {"type": "Old"}}),
                encoding="utf-8",
            )

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models", overwrite=True),
                engine=FakeEngine(fail=True),
            )

            self.assertEqual(result["status"], "failed")
            refreshed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(refreshed["error"]["type"], "InferenceError")


if __name__ == "__main__":
    unittest.main()
