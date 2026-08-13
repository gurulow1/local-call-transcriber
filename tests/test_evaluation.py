from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from evaluate_transcript import evaluate, evaluate_critical_fields, load_critical_fields, main, normalize


class EvaluationTests(unittest.TestCase):
    def test_normalization_handles_case_punctuation_and_yo(self) -> None:
        self.assertEqual(normalize("Расчётный СЧЁТ!"), ["расчетный", "счет"])

    def test_wer_counts_substitution_deletion_and_insertion(self) -> None:
        result = evaluate("один два три", "один пять четыре три")
        self.assertEqual(result["reference_words"], 3)
        self.assertEqual(result["substitutions"], 1)
        self.assertEqual(result["insertions"], 1)
        self.assertEqual(result["deletions"], 0)
        self.assertEqual(result["word_error_rate"], 0.6667)

    def test_cer_counts_character_substitution(self) -> None:
        result = evaluate("кот", "кит")

        self.assertEqual(result["reference_characters"], 3)
        self.assertEqual(result["hypothesis_characters"], 3)
        self.assertEqual(result["character_error_rate"], 0.3333)

    def test_critical_fields_use_explicit_accepted_variants(self) -> None:
        fields = [
            ("bik", ["044525225", "ноль четыре четыре пять два пять два два пять"]),
            ("account", ["четыре ноля и один"]),
        ]
        result = evaluate_critical_fields(fields, "БИК 044525225, счёт 4001")

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["exact_match_rate"], 0.5)
        self.assertEqual(
            result["fields"],
            [{"name": "bik", "matched": True}, {"name": "account", "matched": False}],
        )

    def test_critical_field_file_rejects_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "fields.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "fields": [
                            {"name": "date", "accepted": ["девятое августа"]},
                            {"name": "date", "accepted": ["9 августа"]},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unique"):
                load_critical_fields(path)

    def test_cli_reports_raw_asr_and_final_metrics_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            reference = root / "reference.txt"
            transcript = root / "transcript.json"
            output = root / "evaluation.json"
            critical_fields = root / "critical-fields.json"
            reference.write_text("верный термин", encoding="utf-8")
            transcript.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "raw_text": "ошибочный термин",
                        "text": "верный термин",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            critical_fields.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "fields": [{"name": "term", "accepted": ["верный термин"]}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "evaluate_transcript.py",
                        "--reference",
                        str(reference),
                        "--transcript",
                        str(transcript),
                        "--output",
                        str(output),
                        "--critical-fields",
                        str(critical_fields),
                    ],
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)

            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertGreater(result["raw_asr"]["word_error_rate"], 0)
            self.assertGreater(result["raw_asr"]["character_error_rate"], 0)
            self.assertEqual(result["final"]["word_error_rate"], 0.0)
            self.assertEqual(result["final"]["character_error_rate"], 0.0)
            self.assertEqual(result["critical_fields"]["raw_asr"]["exact_match_rate"], 0.0)
            self.assertEqual(result["critical_fields"]["final"]["exact_match_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
