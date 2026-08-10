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
        self.assertEqual(args.calls_dir, PROJECT_ROOT / "data" / "calls")

    def test_legacy_output_dir_flag_maps_to_calls_dir(self) -> None:
        destination = Path("custom-calls")
        args = build_parser().parse_args(["--output-dir", str(destination)])
        self.assertEqual(args.calls_dir, destination)


if __name__ == "__main__":
    unittest.main()
