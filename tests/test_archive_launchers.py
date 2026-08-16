from __future__ import annotations

import importlib.util
import hashlib
import io
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
PREFLIGHT_PATH = PROJECT_ROOT / "scripts" / "preflight.py"
WHISPER_PREPARE_PATH = PROJECT_ROOT / "scripts" / "prepare_whisper_cpp.py"
FIREWALL_PATH = PROJECT_ROOT / "security" / "windows-deny-network.ps1"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap_runtime", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load bootstrap_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_preflight():
    spec = importlib.util.spec_from_file_location("preflight", PREFLIGHT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load preflight.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_whisper_prepare():
    spec = importlib.util.spec_from_file_location("prepare_whisper_cpp", WHISPER_PREPARE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load prepare_whisper_cpp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _DownloadResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ArchiveLauncherTests(unittest.TestCase):
    def test_primary_launchers_create_python_and_call_common_bootstrap(self) -> None:
        windows = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        powershell = (PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1").read_text(encoding="utf-8")
        linux = LINUX_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("bootstrap_windows.ps1", windows)
        self.assertIn("function Get-Sha256", powershell)
        self.assertNotIn("Get-FileHash", powershell)
        self.assertIn("0953ac2ef4fbe47ad469bfa80b658a577a02c4d73a2fb9c4c7c70dda432efded", powershell)
        self.assertIn('Join-Path $StageDir "uv.exe"', powershell)
        self.assertIn('Join-Path $CacheDir "venv"', powershell)
        self.assertIn("--profile windows-auto", powershell)
        self.assertIn("--prepare-only", powershell)
        self.assertIn("--run-only", powershell)
        self.assertIn("Start-Process", powershell)
        self.assertIn("-Verb RunAs", powershell)
        self.assertLess(powershell.index("--prepare-only"), powershell.index("windows-deny-network.ps1"))
        self.assertLess(powershell.index("windows-deny-network.ps1"), powershell.index("Stop-Transcript"))
        self.assertLess(powershell.index("Stop-Transcript"), powershell.index("--run-only"))
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

    def test_whisper_bootstrap_installs_self_test_dependencies(self) -> None:
        bootstrap = _load_bootstrap()

        self.assertIn("numpy==1.26.4", bootstrap.CORE_PACKAGES)

    def test_whisper_download_retries_after_a_stalled_request(self) -> None:
        prepare = _load_whisper_prepare()
        payload = b"verified runtime"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            prepare.urllib.request,
            "urlopen",
            side_effect=[TimeoutError("stalled"), _DownloadResponse(payload, 200)],
        ) as urlopen:
            destination = Path(directory) / "runtime.zip"
            prepare._download(
                "https://example.invalid/runtime.zip",
                destination,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(urlopen.call_args.kwargs["timeout"], prepare.DOWNLOAD_TIMEOUT_SECONDS)

    def test_whisper_download_resumes_a_partial_file(self) -> None:
        prepare = _load_whisper_prepare()
        payload = b"verified runtime"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "runtime.zip"
            partial = destination.with_name(".runtime.zip.part")
            partial.write_bytes(payload[:8])
            with mock.patch.object(
                prepare.urllib.request,
                "urlopen",
                return_value=_DownloadResponse(payload[8:], 206),
            ) as urlopen:
                prepare._download(
                    "https://example.invalid/runtime.zip",
                    destination,
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                )

            request = urlopen.call_args.args[0]
            self.assertEqual(request.headers["Range"], "bytes=8-")
            self.assertEqual(destination.read_bytes(), payload)

    def test_macos_remains_an_additional_self_installing_launcher(self) -> None:
        script = MACOS_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("uv/releases/download/", script)
        self.assertIn("scripts/bootstrap_runtime.py", script)
        self.assertIn("--profile cpu", script)

    def test_firewall_targets_each_project_python_runtime_exactly(self) -> None:
        script = FIREWALL_PATH.read_text(encoding="utf-8")

        self.assertIn(r".poetry-cache\venv\Scripts\python.exe", script)
        self.assertIn(r".venv\Scripts\python.exe", script)
        self.assertIn("Test-Path -LiteralPath $candidate.Path -PathType Leaf", script)
        self.assertIn("-Program $target.Program", script)
        self.assertIn("[switch]$CheckOnly", script)
        self.assertIn("HNetCfg.FwPolicy2", script)
        self.assertIn("$existing.ApplicationName -ne $target.Program", script)
        self.assertNotIn("-Program $PSScriptRoot", script)
        self.assertNotIn("#requires -RunAsAdministrator", script)
        self.assertIn("if (-not $CheckOnly)", script)

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
            ), mock.patch.object(bootstrap, "_imports_work", return_value=True), mock.patch.object(
                bootstrap, "_model_verifies", return_value=True
            ):
                self.assertFalse(bootstrap._runtime_ready(Path(sys.executable), "t-one"))
                (model_dir / "manifest.json").write_text("{}", encoding="utf-8")
                self.assertTrue(bootstrap._runtime_ready(Path(sys.executable), "t-one"))

    def test_ready_runtime_rejects_a_model_that_fails_hash_verification(self) -> None:
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "models" / "t-one"
            model_dir.mkdir(parents=True)
            (model_dir / "model.onnx").touch()
            (model_dir / "manifest.json").write_text("{}", encoding="utf-8")
            marker = root / "runtime.json"
            marker.write_text(
                '{"setup_version": "archive-bootstrap-v1", "engine": "t-one"}',
                encoding="utf-8",
            )
            with mock.patch.object(bootstrap, "PROJECT_ROOT", root), mock.patch.object(
                bootstrap, "RUNTIME_MARKER", marker
            ), mock.patch.object(bootstrap, "_imports_work", return_value=True), mock.patch.object(
                bootstrap, "_model_verifies", return_value=False
            ):
                self.assertFalse(bootstrap._runtime_ready(Path(sys.executable), "t-one"))

    def test_ready_whisper_runtime_requires_the_vad_artifact(self) -> None:
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "models" / "whisper-large-v3"
            model_dir.mkdir(parents=True)
            (model_dir / "ggml-large-v3.bin").touch()
            (model_dir / "manifest.json").write_text("{}", encoding="utf-8")
            runtime_dir = root / "third_party" / "whisper.cpp"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "whisper-cli.exe").touch()
            marker = root / "runtime.json"
            marker.write_text(
                '{"setup_version": "archive-bootstrap-v1", "engine": "whisper"}',
                encoding="utf-8",
            )
            with mock.patch.object(bootstrap, "PROJECT_ROOT", root), mock.patch.object(
                bootstrap, "RUNTIME_MARKER", marker
            ), mock.patch.object(bootstrap, "_imports_work", return_value=True), mock.patch.object(
                bootstrap, "_model_verifies", return_value=True
            ):
                self.assertFalse(bootstrap._runtime_ready(Path(sys.executable), "whisper"))
                vad_dir = root / "models" / "whisper-vad"
                vad_dir.mkdir()
                (vad_dir / "ggml-silero-v5.1.2.bin").touch()
                (vad_dir / "manifest.json").write_text("{}", encoding="utf-8")
                self.assertTrue(bootstrap._runtime_ready(Path(sys.executable), "whisper"))

    def test_first_setup_runs_self_tests_before_publishing_marker(self) -> None:
        bootstrap = _load_bootstrap()
        events: list[str] = []
        with mock.patch.object(bootstrap, "_runtime_ready", return_value=False), mock.patch.object(
            bootstrap, "_prepare_tone", side_effect=lambda *args: events.append("prepare")
        ), mock.patch.object(bootstrap, "_run_tests", side_effect=lambda *args: events.append("tests")), mock.patch.object(
            bootstrap, "_write_marker", side_effect=lambda *args: events.append("marker")
        ):
            bootstrap._ensure_runtime(Path("uv"), Path(sys.executable), "t-one")

        self.assertEqual(events, ["prepare", "tests", "marker"])

    def test_prepare_only_never_starts_the_worker(self) -> None:
        bootstrap = _load_bootstrap()
        with tempfile.NamedTemporaryFile() as uv, mock.patch.object(
            sys,
            "argv",
            ["bootstrap_runtime.py", "--uv", uv.name, "--profile", "cpu", "--prepare-only"],
        ), mock.patch.object(bootstrap, "_ensure_runtime") as ensure_runtime, mock.patch.object(
            bootstrap.subprocess, "run"
        ) as run:
            self.assertEqual(bootstrap.main(), 0)

        ensure_runtime.assert_called_once()
        run.assert_not_called()

    def test_windows_firewall_targets_the_managed_archive_python(self) -> None:
        firewall = (PROJECT_ROOT / "security" / "windows-deny-network.ps1").read_text(encoding="utf-8")

        self.assertIn("$PythonPath", firewall)
        self.assertIn(".poetry-cache\\venv\\Scripts\\python.exe", firewall)
        self.assertIn("whisper-cli.exe", firewall)
        self.assertIn("Remove-NetFirewallRule", firewall)

    def test_run_only_refuses_an_unprepared_runtime(self) -> None:
        bootstrap = _load_bootstrap()
        with mock.patch.object(
            sys,
            "argv",
            ["bootstrap_runtime.py", "--uv", sys.executable, "--profile", "cpu", "--run-only"],
        ), mock.patch.object(bootstrap, "_runtime_ready", return_value=False):
            with self.assertRaises(SystemExit) as raised:
                bootstrap.main()

        self.assertIn("not ready", str(raised.exception))

    def test_preflight_detects_the_staged_engine_and_required_imports(self) -> None:
        preflight = _load_preflight()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "runtime.json"
            marker.write_text('{"engine":"t-one"}', encoding="utf-8")
            with mock.patch.object(preflight, "RUNTIME_MARKER", marker):
                self.assertEqual(preflight.detected_engine(), "t-one")

        with mock.patch("builtins.__import__", side_effect=ImportError):
            whisper = preflight._imports_check("whisper")
        self.assertEqual(whisper.name, "runtime imports")
        self.assertEqual(whisper.status, "FAIL")
        self.assertEqual(whisper.detail, "missing: av, miniaudio")

    @unittest.skipIf(sys.platform == "win32", "Linux launcher syntax is verified on Linux")
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
    def test_windows_scripts_have_valid_powershell_syntax(self) -> None:
        scripts = (PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1", FIREWALL_PATH)
        for script in scripts:
            with self.subTest(script=script.name):
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
