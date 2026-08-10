from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from evaluate_transcript import evaluate, normalize


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


if __name__ == "__main__":
    unittest.main()

