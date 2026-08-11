from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherTests(unittest.TestCase):
    def test_clickable_launcher_invokes_the_pinned_setup_script(self) -> None:
        launcher = (PROJECT_ROOT / "START_TRANSCRIBER.cmd").read_text(encoding="utf-8")

        self.assertIn("setup_and_run_windows.ps1", launcher)
        self.assertIn("-NoProfile", launcher)
        self.assertIn("-ExecutionPolicy Bypass", launcher)
        self.assertIn("pause", launcher.lower())

    def test_setup_pins_python_and_orders_network_before_firewall(self) -> None:
        setup = (PROJECT_ROOT / "scripts" / "setup_and_run_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe",
            setup,
        )
        self.assertIn(
            "5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde",
            setup,
        )
        self.assertIn("26216840", setup)
        self.assertLess(setup.index("pip\", \"download"), setup.index("prepare_whisper_cpp.py"))
        self.assertLess(
            setup.index("prepare_whisper_cpp.py"), setup.index("windows-deny-network.ps1")
        )
        self.assertLess(setup.index("windows-deny-network.ps1"), setup.index("worker.py"))
        self.assertRegex(setup, r"--mode poll --engine whisper --decoder beam_search")
        self.assertNotIn("SkipFirewall", setup)

    def test_windows_whisper_requirements_are_exactly_hash_pinned(self) -> None:
        requirements = (
            PROJECT_ROOT / "requirements" / "windows-whisper.txt"
        ).read_text(encoding="utf-8")

        pins = re.findall(
            r"^(miniaudio|numpy|av)==([^ ]+)", requirements, flags=re.MULTILINE
        )
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", requirements)
        self.assertEqual(
            pins,
            [("miniaudio", "1.61"), ("numpy", "1.26.4"), ("av", "18.0.0")],
        )
        self.assertEqual(
            hashes,
            [
                "71066552e216d80531d18b87543e1efa68e014a2f8e6064023ef544dc41a1c1e",
                "cd25bcecc4974d09257ffcd1f098ee778f7834c3ad767fe5db785be9a4aa9cb2",
                "aaf4d354d2beaa6651e4f92e54409a578bde64f79c0beef9a30b388d06f7c629",
            ],
        )

    def test_firewall_rejects_disabled_or_partial_existing_rules(self) -> None:
        firewall = (
            PROJECT_ROOT / "security" / "windows-deny-network.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$existing.Enabled -ne "True"', firewall)
        self.assertIn('$existing.Profile -ne "Any"', firewall)
        self.assertIn('-Direction Outbound', firewall)
        self.assertIn('-Action Block', firewall)


if __name__ == "__main__":
    unittest.main()
