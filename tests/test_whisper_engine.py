from __future__ import annotations

import hashlib
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

from local_transcriber.engine import WhisperCppEngine, _prepare_whisper_input


class WhisperCppEngineTests(unittest.TestCase):
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
                return subprocess.CompletedProcess(command, 0)

            engine = WhisperCppEngine(
                model_dir,
                cli_path=executable,
                scratch_dir=root / "scratch",
                initial_prompt="БИК, ИНН",
                verify_model_hashes=True,
            )
            with patch("local_transcriber.engine.subprocess.run", side_effect=fake_run):
                result = engine.transcribe(audio)

            self.assertEqual(result.duration_seconds, 1.0)
            self.assertEqual(result.segments[0].text, "сложный термин")
            self.assertEqual((result.segments[0].start, result.segments[0].end), (0.1, 0.9))


if __name__ == "__main__":
    unittest.main()
