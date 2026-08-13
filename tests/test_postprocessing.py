from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.errors import PostprocessingConfigError
from local_transcriber.postprocessing import postprocess_segments


class PostprocessingTests(unittest.TestCase):
    def test_preserves_asr_text_and_adds_sentence_casing_and_punctuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary = self._write_glossary(Path(temporary_dir), terms=[], phrases=[])

            result = postprocess_segments(
                [
                    {"start": 0.1, "end": 1.0, "text": "  первая   фраза  "},
                    {"start": 1.2, "end": 2.0, "text": "вторая фраза!"},
                ],
                glossary_path=glossary,
            )

            self.assertEqual(result.text, "Первая фраза вторая фраза!")
            self.assertEqual(result.raw_text, "первая   фраза вторая фраза!")
            self.assertEqual(result.segments[0]["asr_text"], "первая   фраза")
            self.assertEqual(result.segments[0]["text"], "Первая фраза")
            self.assertEqual(result.metadata["term_replacements"], 0)

    def test_applies_terms_before_phrases_and_preserves_brand_casing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary = self._write_glossary(
                Path(temporary_dir),
                terms=[
                    {"from": "си трейдер", "to": "cTrader"},
                    {"from": "метод трейдер пять", "to": "MetaTrader 5"},
                ],
                phrases=[
                    {
                        "from": "доступены на платформе и cTrader и MetaTrader 5",
                        "to": "доступен на платформах cTrader и MetaTrader 5",
                    }
                ],
            )

            result = postprocess_segments(
                [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "доступены на платформе и си трейдер и метод трейдер пять",
                    }
                ],
                glossary_path=glossary,
            )

            self.assertEqual(result.text, "Доступен на платформах cTrader и MetaTrader 5.")
            self.assertEqual(result.metadata["term_replacements"], 2)
            self.assertEqual(result.metadata["phrase_replacements"], 1)

    def test_segment_boundaries_preserve_punctuation_and_sentence_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary = self._write_glossary(Path(temporary_dir), terms=[], phrases=[])

            result = postprocess_segments(
                [
                    {"start": 0.0, "end": 1.0, "text": "начало фразы,"},
                    {"start": 1.0, "end": 2.0, "text": "продолжение фразы."},
                    {"start": 2.0, "end": 3.0, "text": "новое предложение:"},
                    {"start": 3.0, "end": 4.0, "text": "пояснение."},
                ],
                glossary_path=glossary,
            )

            self.assertEqual(
                [segment["text"] for segment in result.segments],
                [
                    "Начало фразы,",
                    "продолжение фразы.",
                    "Новое предложение:",
                    "пояснение.",
                ],
            )
            self.assertEqual(
                result.text,
                "Начало фразы, продолжение фразы. Новое предложение: пояснение.",
            )
            self.assertNotIn(",.", result.text)
            self.assertNotIn(":.", result.text)

    def test_terminal_non_sentence_punctuation_becomes_a_sentence_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary = self._write_glossary(Path(temporary_dir), terms=[], phrases=[])

            for raw_text, expected in (
                ("фраза,", "Фраза."),
                ("фраза:", "Фраза."),
                ("фраза;", "Фраза."),
                ("«фраза»", "«Фраза»."),
                ("фраза)", "Фраза)."),
            ):
                with self.subTest(raw_text=raw_text):
                    result = postprocess_segments(
                        [{"start": 0.0, "end": 1.0, "text": raw_text}],
                        glossary_path=glossary,
                    )
                    self.assertEqual(result.text, expected)

    def test_longer_rule_wins_and_reprocessing_uses_original_asr_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary = self._write_glossary(
                Path(temporary_dir),
                terms=[
                    {"from": "тест", "to": "один"},
                    {"from": "тест тест", "to": "два"},
                ],
                phrases=[],
            )

            first = postprocess_segments(
                [{"start": 0.0, "end": 1.0, "text": "тест тест"}],
                glossary_path=glossary,
            )
            second = postprocess_segments(first.segments, glossary_path=glossary)

            self.assertEqual(first.text, "Два.")
            self.assertEqual(second.text, first.text)
            self.assertEqual(second.raw_text, "тест тест")

    def test_rejects_duplicate_rule_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary = self._write_glossary(
                Path(temporary_dir),
                terms=[
                    {"from": "Термин", "to": "one"},
                    {"from": "термин", "to": "two"},
                ],
                phrases=[],
            )

            with self.assertRaises(PostprocessingConfigError):
                postprocess_segments([], glossary_path=glossary)

    @staticmethod
    def _write_glossary(
        root: Path,
        *,
        terms: list[dict[str, str]],
        phrases: list[dict[str, str]],
    ) -> Path:
        path = root / "glossary.json"
        path.write_text(
            json.dumps(
                {"version": "test", "terms": terms, "phrases": phrases},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
