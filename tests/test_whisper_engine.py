from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.engine import (
    WhisperCppEngine,
    _prepare_whisper_input,
    _transcode_aac_to_wav,
)
from local_transcriber.errors import AudioDecodeError, DependencyUnavailableError, ModelValidationError
from local_transcriber.service import TranscriptionRequest, transcribe_file


class WhisperCppEngineTests(unittest.TestCase):
    @staticmethod
    def _write_runtime(root: Path) -> tuple[Path, Path, Path]:
        model_dir = root / "model"
        model_dir.mkdir()
        model = model_dir / "ggml-large-v3.bin"
        model.write_bytes(b"test-model")
        (model_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "Whisper large-v3",
                    "version": "test",
                    "source_revision": "model-revision",
                    "source_code_revision": "code-revision",
                    "files": {
                        model.name: {
                            "size_bytes": model.stat().st_size,
                            "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        executable = root / "whisper-cli.exe"
        executable.write_bytes(b"test-cli")
        vad_dir = root / "vad"
        vad_dir.mkdir()
        vad_model = vad_dir / "vad.bin"
        vad_model.write_bytes(b"test-vad")
        (vad_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "Silero VAD",
                    "version": "test",
                    "source_revision": "vad-model-revision",
                    "source_code_revision": "vad-code-revision",
                    "files": {
                        vad_model.name: {
                            "size_bytes": vad_model.stat().st_size,
                            "sha256": hashlib.sha256(vad_model.read_bytes()).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return model_dir, executable, vad_model

    def test_aac_is_converted_to_temporary_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "call.aac"
            source.write_bytes(b"local-aac")
            scratch = root / "scratch"
            scratch.mkdir()

            with patch(
                "local_transcriber.engine._transcode_aac_to_wav",
                return_value=1.25,
            ) as transcode:
                prepared, duration = _prepare_whisper_input(source, scratch)

            self.assertEqual(prepared, scratch / "input.wav")
            self.assertEqual(duration, 1.25)
            transcode.assert_called_once_with(source.resolve(), prepared)

    def test_local_cli_json_is_converted_to_engine_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            model_dir, executable, vad_model = self._write_runtime(root)
            audio = root / "call.wav"
            with wave.open(str(audio), "wb") as destination:
                destination.setnchannels(1)
                destination.setsampwidth(2)
                destination.setframerate(8000)
                destination.writeframes(b"\0\0" * 8000)

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                response_file = Path(str(_["cwd"])) / command[1][1:]
                arguments = response_file.read_text(encoding="utf-8").splitlines()
                output_prefix = Path(arguments[arguments.index("--output-file") + 1])
                Path(f"{output_prefix}.json").write_text(
                    json.dumps(
                        {
                            "transcription": [
                                {
                                    "offsets": {"from": 100, "to": 900},
                                    "text": " сложный термин ",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertIn("БИК, ИНН", arguments)
                self.assertIn("--vad", arguments)
                self.assertEqual(arguments[arguments.index("--vad-model") + 1], str(vad_model.resolve()))
                self.assertEqual(arguments[arguments.index("--vad-threshold") + 1], "0.50")
                self.assertEqual(_["timeout"], 300.0)
                return subprocess.CompletedProcess(command, 0)

            engine = WhisperCppEngine(
                model_dir,
                cli_path=executable,
                scratch_dir=root / "scratch",
                vad_model_path=vad_model,
                initial_prompt="БИК, ИНН",
                verify_model_hashes=True,
            )
            with patch("local_transcriber.engine.subprocess.run", side_effect=fake_run):
                result = engine.transcribe(audio)

            self.assertEqual(result.duration_seconds, 1.0)
            self.assertEqual(result.segments[0].text, "сложный термин")
            self.assertEqual((result.segments[0].start, result.segments[0].end), (0.1, 0.9))
            self.assertEqual(engine.model_info.local_path, "model")
            self.assertEqual(engine.model_info.vad_name, "Silero VAD")
            self.assertEqual(engine.model_info.vad_version, "test")
            self.assertEqual(engine.model_info.vad_threshold, 0.5)
            self.assertEqual(len(str(engine.model_info.vad_sha256)), 64)

    @unittest.skipUnless(importlib.util.find_spec("av") is not None, "optional PyAV is unavailable")
    def test_aac_decode_stops_before_exceeding_duration_limit(self) -> None:
        import av
        import numpy as np

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "sample.aac"
            output = root / "decoded.wav"
            container = av.open(str(source), mode="w", format="adts")
            stream = container.add_stream("aac", rate=48000)
            stream.layout = "mono"
            frame = av.AudioFrame.from_ndarray(
                np.zeros((1, 48000), dtype=np.float32),
                format="fltp",
                layout="mono",
            )
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
            container.close()

            with patch("local_transcriber.engine.MAX_AUDIO_DURATION_SECONDS", 0):
                with self.assertRaises(AudioDecodeError):
                    _transcode_aac_to_wav(source, output)

            self.assertFalse(output.exists())

    def test_missing_vad_model_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            model_dir, executable, vad_model = self._write_runtime(root)
            vad_model.unlink()

            with self.assertRaises((DependencyUnavailableError, ModelValidationError)):
                WhisperCppEngine(
                    model_dir,
                    cli_path=executable,
                    scratch_dir=root / "scratch",
                    vad_model_path=vad_model,
                )

    def test_subprocess_timeout_is_a_safe_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            model_dir, executable, vad_model = self._write_runtime(root)
            engine = WhisperCppEngine(
                model_dir,
                cli_path=executable,
                scratch_dir=root / "scratch",
                vad_model_path=vad_model,
            )
            observed_timeouts: list[float] = []

            def fake_timeout(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                timeout = float(kwargs["timeout"])
                observed_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output="secret-transcript-fragment",
                    stderr="secret-transcript-fragment",
                )

            for index, duration in enumerate((1.0, 120.0)):
                source = root / f"call-{index}.wav"
                source.write_bytes(b"placeholder")
                output = root / f"call-{index}.json"
                with (
                    patch(
                        "local_transcriber.engine._prepare_whisper_input",
                        return_value=(source.resolve(), duration),
                    ),
                    patch("local_transcriber.engine.subprocess.run", side_effect=fake_timeout),
                ):
                    result = transcribe_file(
                        TranscriptionRequest(
                            source,
                            output,
                            model_dir,
                            vad_model_path=vad_model,
                        ),
                        engine=engine,
                    )

                serialized = output.read_text(encoding="utf-8")
                self.assertEqual(result["status"], "failed")
                self.assertIn(result["error"]["type"], {"TimeoutError", "InferenceError"})
                self.assertEqual(result["text"], "")
                self.assertNotIn("secret-transcript-fragment", serialized)

            self.assertEqual(observed_timeouts, [300.0, 600.0])


if __name__ == "__main__":
    unittest.main()
