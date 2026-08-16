from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.markdown_output import render_transcript_markdown


class MarkdownOutputTests(unittest.TestCase):
    def test_renders_metadata_text_and_timestamped_segments(self) -> None:
        markdown = render_transcript_markdown(
            {
                "call_id": "call-42",
                "status": "completed",
                "source_audio": "call-42.aac",
                "duration_seconds": 65.4,
                "model": {"name": "T-one", "version": "test", "decoder": "greedy"},
                "text": "Первая фраза.",
                "segments": [
                    {
                        "start": 1.2,
                        "end": 65.4,
                        "text": "Первая фраза.",
                        "speaker": "SPEAKER_00",
                    },
                ],
            }
        )

        self.assertIn("# Расшифровка: call-42", markdown)
        self.assertIn("[Открыть исходное аудио](./call-42.aac)", markdown)
        self.assertIn("01:05 (65.4 с)", markdown)
        self.assertIn("**Говорящий 1 · 00:01 — 01:05**", markdown)
        self.assertTrue(markdown.endswith("\n"))

    def test_rejects_failed_result(self) -> None:
        with self.assertRaises(ValueError):
            render_transcript_markdown({"status": "failed"})


if __name__ == "__main__":
    unittest.main()
