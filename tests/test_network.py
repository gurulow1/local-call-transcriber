from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.network import deny_python_network


class NetworkGuardTests(unittest.TestCase):
    def test_common_python_network_calls_are_denied(self) -> None:
        with deny_python_network():
            with self.assertRaises(PermissionError):
                socket.getaddrinfo("example.invalid", 443)
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["HF_HUB_DISABLE_TELEMETRY"], "1")
            self.assertEqual(os.environ["DO_NOT_TRACK"], "1")


if __name__ == "__main__":
    unittest.main()

