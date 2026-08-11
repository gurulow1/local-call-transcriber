from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_LAUNCHER = PROJECT_ROOT / "Запустить транскрибатор.cmd"
LINUX_LAUNCHER = PROJECT_ROOT / "Запустить транскрибатор.sh"
MACOS_LAUNCHER = PROJECT_ROOT / "Запустить транскрибатор.command"
BOOTSTRAP_PATH = PROJECT_ROOT / "scripts" / "bootstrap_runtime.py"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap_runtime", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load bootstrap_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchiveLauncherTests(unittest.TestCase):
    def test_primary_launchers_create_python_and_call_common_bootstrap(self) -> None:
        windows = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        powershell = (PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1").read_text(encoding="utf-8")
        linux = LINUX_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("bootstrap_windows.ps1", windows)
        self.assertIn("0953ac2ef4fbe47ad469bfa80b658a577a02c4d73a2fb9c4c7c70dda432efded", powershell)
        self.assertIn('Join-Path $StageDir "uv.exe"', powershell)
        self.assertIn('Join-Path $CacheDir "venv"', powershell)
        self.assertIn("--profile windows-auto", powershell)
        self.assertIn("f830ea3d38ae1492acf53cb7f2cd0f81d6ae22b42d2d7310a6c7d42c451e1a43", linux)
        self.assertIn(".poetry-cache/venv", linux)
        self.assertIn("--profile cpu", linux)

    def test_common_bootstrap_prefers_whisper_only_on_windows_with_nvidia(self) -> None:
        bootstrap = _load_bootstrap()

        with mock.patch.object(bootstrap.platform, "system", return_value="Windows"), mock.patch.object(
            bootstrap, "nvidia_available", return_value=True
        ):
            self.assertEqual(bootstrap.choose_engine("windows-auto"), "whisper")
        with mock.patch.object(bootstrap.platform, "system", return_value="Windows"), mock.patch.object(
            bootstrap, "nvidia_available", return_value=False
        ):
            self.assertEqual(bootstrap.choose_engine("windows-auto"), "t-one")
        self.assertEqual(bootstrap.choose_engine("cpu"), "t-one")

    def test_macos_remains_an_additional_self_installing_launcher(self) -> None:
        script = MACOS_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("uv/releases/download/", script)
        self.assertIn("scripts/bootstrap_runtime.py", script)
        self.assertIn("--profile cpu", script)

    def test_ready_runtime_requires_the_published_model_manifest(self) -> None:
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "models" / "t-one"
            model_dir.mkdir(parents=True)
            (model_dir / "model.onnx").touch()
            marker = root / "runtime.json"
            marker.write_text(
                '{"setup_version": "archive-bootstrap-v1", "engine": "t-one"}',
                encoding="utf-8",
            )
            with mock.patch.object(bootstrap, "PROJECT_ROOT", root), mock.patch.object(
                bootstrap, "RUNTIME_MARKER", marker
            ), mock.patch.object(bootstrap, "_imports_work", return_value=True):
                self.assertFalse(bootstrap._runtime_ready(Path(sys.executable), "t-one"))
                (model_dir / "manifest.json").write_text("{}", encoding="utf-8")
                self.assertTrue(bootstrap._runtime_ready(Path(sys.executable), "t-one"))

    @unittest.skipUnless(shutil.which("bash"), "bash is unavailable on this platform")
    def test_linux_launcher_has_valid_bash_syntax(self) -> None:
        probe = subprocess.run(["bash", "-c", "exit 0"], check=False, capture_output=True)
        if probe.returncode != 0:
            self.skipTest("bash is present but unavailable")
        completed = subprocess.run(
            ["bash", "-n", str(LINUX_LAUNCHER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is unavailable")
    def test_windows_bootstrap_has_valid_powershell_syntax(self) -> None:
        script = PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1"
        escaped = str(script).replace("'", "''")
        command = (
            "$tokens=$null; $errors=$null; "
            f"[void][Management.Automation.Language.Parser]::ParseFile('{escaped}',[ref]$tokens,[ref]$errors); "
            "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is unavailable on this platform")
    def test_macos_launcher_has_valid_zsh_syntax(self) -> None:
        probe = subprocess.run(["zsh", "-c", "exit 0"], check=False, capture_output=True)
        if probe.returncode != 0:
            self.skipTest("zsh is present but unavailable")
        completed = subprocess.run(
            ["zsh", "-n", str(MACOS_LAUNCHER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
