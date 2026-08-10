from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.worker_cli import build_parser


class WorkerCliTests(unittest.TestCase):
    def test_parser_is_available_on_the_current_platform(self) -> None:
        args = build_parser().parse_args(["--mode", "once", "--decoder", "greedy"])
        self.assertEqual((args.mode, args.decoder), ("once", "greedy"))


if __name__ == "__main__":
    unittest.main()
