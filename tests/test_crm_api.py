from __future__ import annotations

import hashlib
import http.client
import io
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.api_cli import build_parser
from local_transcriber.crm_api import (
    UPLOAD_RESERVATION_TTL_SECONDS,
    CrmApiApplication,
    CrmApiServer,
)
from local_transcriber.engine import EngineModelInfo, EngineResult, Segment
from local_transcriber.queue import QueueStore
from local_transcriber.worker import FolderWorker, WorkerConfig


class SyntheticEngine:
    @property
    def model_info(self) -> EngineModelInfo:
        return EngineModelInfo(
            name="T-one",
            version="test",
            source_revision="test-model",
            source_code_revision="test-code",
            decoder="greedy",
            local_path="models/test",
        )

    def transcribe(self, input_path: Path) -> EngineResult:
        return EngineResult(
            duration_seconds=1.0,
            segments=(Segment(start=0.0, end=0.9, text="синтетический тест"),),
        )


def decode(response_body: bytes) -> dict[str, object]:
    payload = json.loads(response_body.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def completed_result(call_id: str, extension: str = ".wav") -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "call_id": call_id,
        "status": "completed",
        "source_audio": f"{call_id}{extension}",
        "language": "ru",
        "duration_seconds": 1.0,
        "processing_seconds": 0.5,
        "real_time_factor": 0.5,
        "model": {
            "name": "T-one",
            "version": "test",
            "source_revision": "test-model",
            "source_code_revision": "test-code",
            "decoder": "greedy",
            "local_path": "models/test",
            "vad_name": None,
            "vad_version": None,
            "vad_source_revision": None,
            "vad_sha256": None,
            "vad_threshold": None,
        },
        "text": "Синтетический тест.",
        "raw_text": "синтетический тест",
        "segments": [
            {
                "start": 0.0,
                "end": 0.9,
                "text": "Синтетический тест.",
                "asr_text": "синтетический тест",
            }
        ],
        "postprocessing": {
            "method": "deterministic_glossary_v2",
            "glossary_version": "1",
            "term_replacements": 0,
            "phrase_replacements": 0,
        },
        "created_at": "2026-08-10T09:00:00.000Z",
        "completed_at": "2026-08-10T09:00:00.500Z",
        "error": None,
    }


class CrmApiApplicationTests(unittest.TestCase):
    def make_application(self, root: Path, *, max_upload_bytes: int = 1024) -> CrmApiApplication:
        return CrmApiApplication(
            input_dir=root / "input",
            calls_dir=root / "calls",
            database_path=root / "queue.sqlite3",
            max_upload_bytes=max_upload_bytes,
        )

    def test_upload_is_atomic_and_new_status_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = self.make_application(root)
            audio = b"synthetic-aac-placeholder"
            response = application.handle_put(
                "/v1/jobs/demo-001/audio.aac",
                {"Content-Length": str(len(audio)), "Content-Type": "audio/aac"},
                io.BytesIO(audio),
            )

            self.assertEqual(response.status, HTTPStatus.ACCEPTED)
            payload = decode(response.body)
            self.assertEqual(payload["call_id"], "demo-001")
            self.assertEqual(payload["status"], "new")
            self.assertEqual(payload["sha256"], hashlib.sha256(audio).hexdigest())
            self.assertEqual((root / "input" / "demo-001.aac").read_bytes(), audio)
            self.assertEqual(list((root / "input").glob("*.part")), [])
            connection = sqlite3.connect(root / "queue.sqlite3")
            try:
                reservation_count = connection.execute(
                    "SELECT COUNT(*) FROM api_upload_reservations"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(reservation_count, 0)

            status_response = application.handle_get("/v1/jobs/demo-001")
            status_payload = decode(status_response.body)
            self.assertEqual(status_response.status, HTTPStatus.OK)
            self.assertEqual(status_payload["status"], "new")
            self.assertNotIn(str(root), status_response.body.decode("utf-8"))

            result_response = application.handle_get("/v1/jobs/demo-001/result")
            self.assertEqual(result_response.status, HTTPStatus.CONFLICT)
            self.assertEqual(decode(result_response.body)["error"]["code"], "result_not_ready")  # type: ignore[index]

    def test_duplicate_upload_never_overwrites_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = self.make_application(root)
            original = b"original"
            headers = {"Content-Length": str(len(original)), "Content-Type": "audio/wav"}
            first = application.handle_put(
                "/v1/jobs/demo-001/audio.wav",
                headers,
                io.BytesIO(original),
            )
            second_audio = b"replacement"
            second = application.handle_put(
                "/v1/jobs/demo-001/audio.wav",
                {
                    "Content-Length": str(len(second_audio)),
                    "Content-Type": "audio/wav",
                },
                io.BytesIO(second_audio),
            )

            self.assertEqual(first.status, HTTPStatus.ACCEPTED)
            self.assertEqual(second.status, HTTPStatus.CONFLICT)
            self.assertEqual((root / "input" / "demo-001.wav").read_bytes(), original)

            different_extension = application.handle_put(
                "/v1/jobs/demo-001/audio.aac",
                {"Content-Length": "3", "Content-Type": "audio/aac"},
                io.BytesIO(b"aac"),
            )
            self.assertEqual(different_extension.status, HTTPStatus.CONFLICT)
            self.assertFalse((root / "input" / "demo-001.aac").exists())

    def test_concurrent_different_extensions_reserve_one_call_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = self.make_application(root)
            barrier = threading.Barrier(2)
            original_check = application._ensure_new_call
            responses = []
            errors: list[BaseException] = []

            def synchronized_check(call_id: str) -> None:
                original_check(call_id)
                if not getattr(thread_state, "initial_check_done", False):
                    thread_state.initial_check_done = True
                    barrier.wait(timeout=5)

            def upload(extension: str, content_type: str, body: bytes) -> None:
                try:
                    responses.append(
                        application.handle_put(
                            f"/v1/jobs/race/audio{extension}",
                            {
                                "Content-Length": str(len(body)),
                                "Content-Type": content_type,
                            },
                            io.BytesIO(body),
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            thread_state = threading.local()
            with patch.object(application, "_ensure_new_call", side_effect=synchronized_check):
                threads = [
                    threading.Thread(target=upload, args=(".wav", "audio/wav", b"wav")),
                    threading.Thread(target=upload, args=(".aac", "audio/aac", b"aac")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(
                sorted(response.status for response in responses),
                [HTTPStatus.ACCEPTED, HTTPStatus.CONFLICT],
            )
            published = list((root / "input").glob("race.*"))
            self.assertEqual(len(published), 1)

    def test_incomplete_upload_releases_call_id_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = self.make_application(root)
            incomplete = application.handle_put(
                "/v1/jobs/retry/audio.wav",
                {"Content-Length": "4", "Content-Type": "audio/wav"},
                io.BytesIO(b"12"),
            )
            retry = application.handle_put(
                "/v1/jobs/retry/audio.wav",
                {"Content-Length": "2", "Content-Type": "audio/wav"},
                io.BytesIO(b"ok"),
            )

            self.assertEqual(incomplete.status, HTTPStatus.BAD_REQUEST)
            self.assertEqual(retry.status, HTTPStatus.ACCEPTED)
            self.assertEqual((root / "input" / "retry.wav").read_bytes(), b"ok")

    def test_crashed_upload_reservation_expires_and_legacy_table_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            database = root / "queue.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE api_upload_reservations (call_id TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO api_upload_reservations(call_id) VALUES ('retry')"
                )
                connection.commit()
            finally:
                connection.close()

            application = self.make_application(root)
            with patch(
                "local_transcriber.crm_api.time.time",
                return_value=UPLOAD_RESERVATION_TTL_SECONDS + 1,
            ):
                response = application.handle_put(
                    "/v1/jobs/retry/audio.wav",
                    {"Content-Length": "2", "Content-Type": "audio/wav"},
                    io.BytesIO(b"ok"),
                )

            self.assertEqual(response.status, HTTPStatus.ACCEPTED)
            self.assertEqual((root / "input" / "retry.wav").read_bytes(), b"ok")

    def test_invalid_or_unsafe_uploads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = self.make_application(root, max_upload_bytes=4)
            cases = (
                (
                    "/v1/jobs/bad%20id/audio.wav",
                    {"Content-Length": "1", "Content-Type": "audio/wav"},
                    b"x",
                    HTTPStatus.BAD_REQUEST,
                ),
                (
                    "/v1/jobs/demo/audio.m4a",
                    {"Content-Length": "1", "Content-Type": "audio/mp4"},
                    b"x",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                ),
                (
                    "/v1/jobs/demo/audio.wav",
                    {"Content-Length": "1", "Content-Type": "audio/aac"},
                    b"x",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                ),
                (
                    "/v1/jobs/demo/audio.wav",
                    {"Content-Length": "5", "Content-Type": "audio/wav"},
                    b"12345",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                ),
                (
                    "/v1/jobs/demo/audio.wav",
                    {"Content-Length": "4", "Content-Type": "audio/wav"},
                    b"12",
                    HTTPStatus.BAD_REQUEST,
                ),
                (
                    "/v1/jobs/demo/audio.wav?overwrite=true",
                    {"Content-Length": "1", "Content-Type": "audio/wav"},
                    b"x",
                    HTTPStatus.BAD_REQUEST,
                ),
            )
            for target, headers, body, expected_status in cases:
                with self.subTest(target=target, status=expected_status):
                    response = application.handle_put(target, headers, io.BytesIO(body))
                    self.assertEqual(response.status, expected_status)
            self.assertFalse((root / "input" / "demo.wav").exists())

    def test_symlinked_staging_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            real_input = root / "real-input"
            real_input.mkdir()
            linked_input = root / "input"
            try:
                linked_input.symlink_to(real_input, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable on this platform")
            application = CrmApiApplication(
                input_dir=linked_input,
                calls_dir=root / "calls",
                database_path=root / "queue.sqlite3",
            )
            response = application.handle_put(
                "/v1/jobs/demo/audio.wav",
                {"Content-Length": "1", "Content-Type": "audio/wav"},
                io.BytesIO(b"x"),
            )
            self.assertEqual(response.status, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertEqual(decode(response.body)["error"]["code"], "unsafe_storage")  # type: ignore[index]
            self.assertEqual(list(real_input.iterdir()), [])

    def test_queue_status_and_completed_result_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            input_dir = root / "input"
            calls_dir = root / "calls"
            input_dir.mkdir()
            source = input_dir / "demo-001.wav"
            source.write_bytes(b"synthetic")
            output = calls_dir / "demo-001" / "demo-001.json"
            application = self.make_application(root)
            with QueueStore(root / "queue.sqlite3") as store:
                store.observe(source, output, now=1.0, stable_seconds=0.0, max_attempts=3)
                store.observe(source, output, now=1.0, stable_seconds=0.0, max_attempts=3)

            queued = decode(application.handle_get("/v1/jobs/demo-001").body)
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(queued["attempts"], 0)

            output.parent.mkdir(parents=True)
            expected_result = completed_result("demo-001")
            output.write_text(json.dumps(expected_result, ensure_ascii=False), encoding="utf-8")
            result_response = application.handle_get("/v1/jobs/demo-001/result")
            self.assertEqual(result_response.status, HTTPStatus.OK)
            self.assertEqual(decode(result_response.body), expected_result)
            completed = decode(application.handle_get("/v1/jobs/demo-001").body)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result_url"], "/v1/jobs/demo-001/result")

    def test_upload_flows_through_worker_to_api_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = self.make_application(root)
            audio = b"synthetic-audio-placeholder"
            upload = application.handle_put(
                "/v1/jobs/e2e-demo/audio.wav",
                {"Content-Length": str(len(audio)), "Content-Type": "audio/wav"},
                io.BytesIO(audio),
            )
            self.assertEqual(upload.status, HTTPStatus.ACCEPTED)

            worker = FolderWorker(
                WorkerConfig(
                    input_dir=root / "input",
                    calls_dir=root / "calls",
                    failed_dir=root / "failed",
                    database_path=root / "queue.sqlite3",
                    model_dir=root / "models",
                    decoder="greedy",
                    stable_seconds=0.0,
                ),
                engine=SyntheticEngine(),
                worker_id="crm-e2e-worker",
            )
            try:
                worker.scan(now=1.0)
                worker.scan(now=1.0)
                self.assertEqual(worker.drain(), 1)
            finally:
                worker.close()

            status = decode(application.handle_get("/v1/jobs/e2e-demo").body)
            result_response = application.handle_get("/v1/jobs/e2e-demo/result")
            result = decode(result_response.body)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(result_response.status, HTTPStatus.OK)
            self.assertEqual(result["text"], "Синтетический тест.")
            self.assertEqual(
                (root / "calls" / "e2e-demo" / "e2e-demo.wav").read_bytes(),
                audio,
            )

    def test_failed_queue_error_is_safe_and_has_no_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "demo.wav"
            source.write_bytes(b"synthetic")
            output = root / "calls" / "demo" / "demo.json"
            with QueueStore(root / "queue.sqlite3") as store:
                store.observe(source, output, now=1.0, stable_seconds=0.0, max_attempts=1)
                store.observe(source, output, now=1.0, stable_seconds=0.0, max_attempts=1)
                job = store.claim("test-worker", now=1.0, lease_seconds=10.0)
                assert job is not None
                store.fail(
                    job,
                    error_type="AudioDecodeError",
                    error_message="synthetic decode failure",
                    retryable=False,
                    now=2.0,
                    retry_base_seconds=1.0,
                )

            response = self.make_application(root).handle_get("/v1/jobs/demo")
            payload = decode(response.body)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error"]["type"], "AudioDecodeError")  # type: ignore[index]
            self.assertEqual(  # type: ignore[index]
                payload["error"]["message"],
                "Processing failed; consult local technical logs",
            )
            self.assertNotIn(str(root), response.body.decode("utf-8"))


class CrmApiHttpSmokeTests(unittest.TestCase):
    def test_loopback_http_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            application = CrmApiApplication(
                input_dir=root / "input",
                calls_dir=root / "calls",
                database_path=root / "queue.sqlite3",
            )
            try:
                server = CrmApiServer(("127.0.0.1", 0), application)
            except PermissionError:
                self.skipTest("the execution sandbox forbids loopback sockets")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, HTTPStatus.OK)
                self.assertEqual(payload["status"], "ok")

                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.putrequest("GET", "/healthz", skip_host=True)
                connection.putheader("Host", f"localhost:{server.server_port}")
                connection.endheaders()
                localhost_response = connection.getresponse()
                localhost_response.read()
                connection.close()
                self.assertEqual(localhost_response.status, HTTPStatus.OK)

                for invalid_host in (
                    f"example.invalid:{server.server_port}",
                    f"127.0.0.1:{server.server_port + 1}",
                ):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=5
                    )
                    connection.putrequest("GET", "/healthz", skip_host=True)
                    connection.putheader("Host", invalid_host)
                    connection.endheaders()
                    invalid_response = connection.getresponse()
                    invalid_payload = json.loads(invalid_response.read().decode("utf-8"))
                    connection.close()
                    self.assertEqual(invalid_response.status, HTTPStatus.BAD_REQUEST)
                    self.assertEqual(invalid_payload["error"]["code"], "invalid_host")

                audio = b"synthetic-http-upload"
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request(
                    "PUT",
                    "/v1/jobs/http-demo/audio.wav",
                    body=audio,
                    headers={"Content-Type": "audio/wav", "Content-Length": str(len(audio))},
                )
                upload_response = connection.getresponse()
                upload_payload = json.loads(upload_response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(upload_response.status, HTTPStatus.ACCEPTED)
                self.assertEqual(upload_payload["call_id"], "http-demo")
                self.assertEqual((root / "input" / "http-demo.wav").read_bytes(), audio)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_cli_has_no_non_loopback_host_option(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--port", "9876"])
        self.assertEqual(args.port, 9876)
        self.assertFalse(any(action.dest == "host" for action in parser._actions))


class CrmContractArtifactTests(unittest.TestCase):
    def test_schemas_openapi_and_examples_are_valid_json(self) -> None:
        paths = [
            PROJECT_ROOT / "schemas" / "crm-job-status-v1.schema.json",
            PROJECT_ROOT / "schemas" / "transcription-result-v1.1.schema.json",
            PROJECT_ROOT / "docs" / "openapi-v1.json",
            *(PROJECT_ROOT / "examples" / "crm").glob("*.json"),
        ]
        for path in paths:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(payload, dict)

        job_schema = json.loads(paths[0].read_text(encoding="utf-8"))
        queued = json.loads(
            (PROJECT_ROOT / "examples" / "crm" / "job-queued.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(job_schema["required"]), set(queued))

        result_schema = json.loads(paths[1].read_text(encoding="utf-8"))
        completed = json.loads(
            (PROJECT_ROOT / "examples" / "crm" / "transcription-completed.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(result_schema["required"]), set(completed))
        self.assertIsNotNone(
            re.fullmatch(result_schema["properties"]["source_audio"]["pattern"], "demo.WaV")
        )
        model_schema = result_schema["properties"]["model"]["oneOf"][1]
        self.assertTrue(set(model_schema["required"]).issubset(completed["model"]))
        self.assertGreater(completed["duration_seconds"], 0)
        self.assertIn(
            completed["postprocessing"]["method"],
            result_schema["properties"]["postprocessing"]["oneOf"][1]["properties"]["method"]["enum"],
        )


if __name__ == "__main__":
    unittest.main()
