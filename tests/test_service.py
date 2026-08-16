from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.engine import EngineModelInfo, EngineResult, Segment
from local_transcriber.errors import InferenceError
from local_transcriber.service import SCHEMA_VERSION, TranscriptionRequest, transcribe_file


class FakeEngine:
    def __init__(self, *, fail: bool = False, result: EngineResult | None = None) -> None:
        self._fail = fail
        self._result = result

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
        return self._result or EngineResult(
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
            self.assertEqual(result["text"], "Тестовая запись.")
            self.assertEqual(result["raw_text"], "тестовая запись")
            self.assertEqual(len(result["segments"]), 2)
            self.assertEqual(result["segments"][0]["asr_text"], "тестовая")
            self.assertEqual(result["segments"][0]["text"], "Тестовая")
            self.assertEqual(result["postprocessing"]["method"], "deterministic_glossary_v2")
            self.assertEqual(result["model"]["name"], "T-one")
            self.assertIsNone(result["model"]["vad_name"])
            self.assertIsNone(result["error"])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
            markdown = markdown_output.read_text(encoding="utf-8")
            self.assertIn("# Расшифровка: 1234", markdown)
            self.assertIn("[Открыть исходное аудио](../1234.wav)", markdown)
            self.assertIn("Тестовая запись.", markdown)
            self.assertIn("RTF", markdown)
            self.assertIn("test-model-revision", markdown)
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

    def test_unsupported_extension_does_not_publish_an_invalid_failure_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "call-42.m4a"
            source.write_bytes(b"placeholder")
            output = root / "call-42.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "InputValidationError")
            self.assertFalse(output.exists())

    def test_segment_end_is_limited_to_audio_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"
            engine_result = EngineResult(
                duration_seconds=2.5,
                segments=(Segment(start=0.1, end=30.0, text="фраза"),),
            )

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(result=engine_result),
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["segments"][0]["end"], 2.5)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)

    def test_stereo_speaker_label_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(
                    result=EngineResult(
                        duration_seconds=2.5,
                        segments=(
                            Segment(
                                start=0.1,
                                end=0.8,
                                text="первый канал",
                                speaker="SPEAKER_00",
                            ),
                        ),
                    )
                ),
            )

            self.assertEqual(result["segments"][0]["speaker"], "SPEAKER_00")

    def test_invalid_speaker_label_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(
                    result=EngineResult(
                        duration_seconds=2.5,
                        segments=(Segment(0.1, 0.8, "фраза", speaker="MANAGER"),),
                    )
                ),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "OutputValidationError")

    def test_duration_below_contract_precision_is_not_published_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "tiny.wav"
            source.write_bytes(b"placeholder")
            output = root / "tiny.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(
                    result=EngineResult(duration_seconds=0.0004, segments=()),
                ),
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["type"], "InputValidationError")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "failed")

    def test_uppercase_supported_extension_matches_the_result_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "CALL.WaV"
            source.write_bytes(b"placeholder")
            output = root / "CALL.json"

            result = transcribe_file(
                TranscriptionRequest(source, output, root / "models"),
                engine=FakeEngine(),
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["source_audio"], "CALL.WaV")

    def test_invalid_segment_timestamps_fail_with_strict_json(self) -> None:
        invalid_segments = (
            Segment(start=float("nan"), end=1.0, text="nan"),
            Segment(start=-0.1, end=1.0, text="negative"),
            Segment(start=1.0, end=0.5, text="reversed"),
        )
        for index, segment in enumerate(invalid_segments):
            with self.subTest(segment=segment), tempfile.TemporaryDirectory() as temporary_dir:
                root = Path(temporary_dir)
                source = root / f"call-{index}.wav"
                source.write_bytes(b"placeholder")
                output = root / f"call-{index}.json"

                result = transcribe_file(
                    TranscriptionRequest(source, output, root / "models"),
                    engine=FakeEngine(
                        result=EngineResult(duration_seconds=2.5, segments=(segment,)),
                    ),
                )

                serialized = output.read_text(encoding="utf-8")
                payload = json.loads(
                    serialized,
                    parse_constant=lambda value: self.fail(f"non-finite JSON number: {value}"),
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["segments"], [])

    def test_vad_model_path_is_forwarded_to_engine_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"placeholder")
            output = root / "1234.json"
            vad_model = root / "vad.bin"
            vad_model.write_bytes(b"test-vad")

            with patch("local_transcriber.service.create_engine", return_value=FakeEngine()) as create:
                result = transcribe_file(
                    TranscriptionRequest(
                        source,
                        output,
                        root / "models",
                        vad_model_path=vad_model,
                    ),
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(create.call_args.kwargs["vad_model_path"], vad_model)

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
            self.assertFalse(output.exists())

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
            self.assertEqual(result["call_id"], "unknown")
            self.assertFalse(output.exists())

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
