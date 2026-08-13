"""Loopback-only reference HTTP adapter for future CRM integration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Mapping
from urllib.parse import unquote, urlsplit

from .queue import QueueSnapshot, QueueStore
from .service import CALL_ID_PATTERN, SUPPORTED_EXTENSIONS

LOGGER = logging.getLogger("local_transcriber.crm_api")
API_VERSION = "1.0"
DEFAULT_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
UPLOAD_RESERVATION_TTL_SECONDS = 15 * 60
CONTENT_TYPES = {
    ".wav": {"audio/wav", "audio/x-wav", "application/octet-stream"},
    ".mp3": {"audio/mpeg", "audio/mp3", "application/octet-stream"},
    ".flac": {"audio/flac", "audio/x-flac", "application/octet-stream"},
    ".ogg": {"audio/ogg", "application/ogg", "application/octet-stream"},
    ".aac": {"audio/aac", "audio/aacp", "application/octet-stream"},
}


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json_response(
    status: int,
    payload: Mapping[str, object],
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> ApiResponse:
    body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }
    if extra_headers:
        headers.update(extra_headers)
    return ApiResponse(status=status, body=body, headers=headers)


def _problem_response(problem: ApiProblem) -> ApiResponse:
    return _json_response(
        problem.status,
        {
            "api_version": API_VERSION,
            "error": {
                "code": problem.code,
                "message": problem.message,
            },
        },
    )


class CrmApiApplication:
    """Pure request application used by both the HTTP server and tests."""

    def __init__(
        self,
        *,
        input_dir: Path,
        calls_dir: Path,
        database_path: Path,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        self.input_dir = input_dir.absolute()
        self.calls_dir = calls_dir.absolute()
        self.database_path = database_path.absolute()
        self.max_upload_bytes = max_upload_bytes

    def handle_get(self, target: str) -> ApiResponse:
        try:
            path = self._path_without_query(target)
            if path == "/healthz":
                return _json_response(
                    HTTPStatus.OK,
                    {
                        "api_version": API_VERSION,
                        "service": "local-call-transcriber",
                        "status": "ok",
                    },
                )
            route, call_id, _extension = self._parse_job_route(path)
            if route == "status":
                payload = self._status_payload(call_id)
                if payload is None:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "job_not_found", "Job was not found")
                return _json_response(HTTPStatus.OK, payload)
            if route == "result":
                return _json_response(HTTPStatus.OK, self._completed_result(call_id))
            raise ApiProblem(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Use PUT")
        except ApiProblem as problem:
            return _problem_response(problem)
        except OSError:
            LOGGER.exception("api_storage_error method=GET")
            return _problem_response(
                ApiProblem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "storage_error",
                    "Local storage is unavailable",
                )
            )
        except Exception:
            LOGGER.exception("api_internal_error method=GET")
            return _problem_response(
                ApiProblem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "Request could not be completed",
                )
            )

    def handle_put(
        self,
        target: str,
        headers: Mapping[str, str],
        body: BinaryIO,
    ) -> ApiResponse:
        try:
            path = self._path_without_query(target)
            route, call_id, extension = self._parse_job_route(path)
            if route != "upload" or extension is None:
                raise ApiProblem(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "PUT is supported only for an audio upload URL",
                )
            normalized_headers = {key.lower(): value for key, value in headers.items()}
            if normalized_headers.get("transfer-encoding"):
                raise ApiProblem(
                    HTTPStatus.BAD_REQUEST,
                    "chunked_upload_not_supported",
                    "Content-Length is required; chunked upload is not supported",
                )
            length = self._content_length(normalized_headers)
            self._validate_content_type(normalized_headers, extension)
            filename = f"{call_id}{extension}"
            destination = self.input_dir / filename
            self._ensure_new_call(call_id)
            self._reserve_call_id(call_id)
            try:
                digest = self._publish_upload(destination, body, length)
            finally:
                try:
                    self._release_call_id_reservation(call_id)
                except OSError:
                    LOGGER.exception("api_reservation_release_error")
            status_url = f"/v1/jobs/{call_id}"
            return _json_response(
                HTTPStatus.ACCEPTED,
                {
                    "api_version": API_VERSION,
                    "call_id": call_id,
                    "status": "new",
                    "source_audio": filename,
                    "bytes_received": length,
                    "sha256": digest,
                    "status_url": status_url,
                    "result_url": f"{status_url}/result",
                },
                extra_headers={"Location": status_url},
            )
        except ApiProblem as problem:
            return _problem_response(problem)
        except OSError:
            LOGGER.exception("api_storage_error method=PUT")
            return _problem_response(
                ApiProblem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "storage_error",
                    "Audio could not be stored locally",
                )
            )
        except Exception:
            LOGGER.exception("api_internal_error method=PUT")
            return _problem_response(
                ApiProblem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "Request could not be completed",
                )
            )

    @staticmethod
    def _path_without_query(target: str) -> str:
        parsed = urlsplit(target)
        if parsed.query or parsed.fragment:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "query_not_supported", "Query parameters are not supported")
        try:
            return unquote(parsed.path, errors="strict")
        except UnicodeError as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_path", "Request path is invalid") from exc

    @staticmethod
    def _parse_job_route(path: str) -> tuple[str, str, str | None]:
        parts = path.split("/")
        if len(parts) not in {4, 5} or parts[:3] != ["", "v1", "jobs"]:
            raise ApiProblem(HTTPStatus.NOT_FOUND, "route_not_found", "Route was not found")
        call_id = parts[3]
        if not CALL_ID_PATTERN.fullmatch(call_id):
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_call_id", "call_id has an invalid format")
        if len(parts) == 4:
            return "status", call_id, None
        suffix = parts[4]
        if suffix == "result":
            return "result", call_id, None
        if suffix.startswith("audio"):
            extension = suffix.removeprefix("audio").lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise ApiProblem(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "unsupported_audio_format",
                    "Supported formats: WAV, MP3, FLAC, OGG and AAC",
                )
            return "upload", call_id, extension
        raise ApiProblem(HTTPStatus.NOT_FOUND, "route_not_found", "Route was not found")

    def _content_length(self, headers: Mapping[str, str]) -> int:
        raw_length = headers.get("content-length")
        if raw_length is None:
            raise ApiProblem(HTTPStatus.LENGTH_REQUIRED, "content_length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length is invalid") from exc
        if length < 1:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "empty_audio", "Audio body must not be empty")
        if length > self.max_upload_bytes:
            raise ApiProblem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "audio_too_large", "Audio exceeds the configured size limit")
        return length

    @staticmethod
    def _validate_content_type(headers: Mapping[str, str], extension: str) -> None:
        content_type = headers.get("content-type", "application/octet-stream")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in CONTENT_TYPES[extension]:
            raise ApiProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "content_type_mismatch",
                "Content-Type does not match the audio extension",
            )

    def _ensure_new_call(self, call_id: str) -> None:
        if self.input_dir.is_symlink() or self.calls_dir.is_symlink():
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "unsafe_storage",
                "Configured storage must not be a symlink",
            )
        if any(
            (self.input_dir / f"{call_id}{extension}").exists()
            or (self.input_dir / f"{call_id}{extension}").is_symlink()
            for extension in SUPPORTED_EXTENSIONS
        ):
            raise ApiProblem(HTTPStatus.CONFLICT, "call_already_exists", "call_id already exists")
        call_dir = self.calls_dir / call_id
        if call_dir.is_symlink():
            raise ApiProblem(HTTPStatus.CONFLICT, "call_already_exists", "call_id already exists")
        if call_dir.is_dir() and any(call_dir.iterdir()):
            raise ApiProblem(HTTPStatus.CONFLICT, "call_already_exists", "call_id already exists")

    def _reserve_call_id(self, call_id: str, *, now: float | None = None) -> None:
        """Atomically reserve one immutable call identity across concurrent uploads."""

        reserved_at = time.time() if now is None else now
        if self.database_path.is_symlink():
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "unsafe_storage",
                "Configured database must not be a symlink",
            )
        connection: sqlite3.Connection | None = None
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.database_path,
                timeout=30,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_upload_reservations (
                        call_id TEXT PRIMARY KEY,
                        reserved_at REAL NOT NULL
                    )
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(api_upload_reservations)")
                }
                if "reserved_at" not in columns:
                    connection.execute(
                        "ALTER TABLE api_upload_reservations "
                        "ADD COLUMN reserved_at REAL NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    "DELETE FROM api_upload_reservations WHERE reserved_at <= ?",
                    (reserved_at - UPLOAD_RESERVATION_TTL_SECONDS,),
                )
                # The first check happens before opening SQLite. Repeat it while
                # holding the reservation write lock so a just-finished upload
                # cannot slip through after releasing its short-lived row.
                self._ensure_new_call(call_id)
                jobs_exist = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'jobs'
                    """
                ).fetchone()
                queued = None
                if jobs_exist is not None:
                    queued = connection.execute(
                        "SELECT 1 FROM jobs WHERE call_id = ?",
                        (call_id,),
                    ).fetchone()
                reserved = connection.execute(
                    "SELECT 1 FROM api_upload_reservations WHERE call_id = ?",
                    (call_id,),
                ).fetchone()
                if queued is not None or reserved is not None:
                    raise ApiProblem(
                        HTTPStatus.CONFLICT,
                        "call_already_exists",
                        "call_id already exists",
                    )
                connection.execute(
                    "INSERT INTO api_upload_reservations(call_id, reserved_at) VALUES (?, ?)",
                    (call_id, reserved_at),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise OSError("Call reservation storage is unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    def _release_call_id_reservation(self, call_id: str) -> None:
        connection: sqlite3.Connection | None = None
        try:
            if self.database_path.is_symlink():
                raise OSError("Call reservation storage is unsafe")
            connection = sqlite3.connect(
                self.database_path,
                timeout=30,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute(
                "DELETE FROM api_upload_reservations WHERE call_id = ?",
                (call_id,),
            )
        except sqlite3.Error as exc:
            raise OSError("Call reservation storage is unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    def _publish_upload(self, destination: Path, body: BinaryIO, length: int) -> str:
        self.input_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.input_dir,
            prefix=".crm-upload-",
            suffix=".part",
        )
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        remaining = length
        try:
            with os.fdopen(descriptor, "wb") as output:
                while remaining:
                    chunk = body.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiProblem(
                            HTTPStatus.BAD_REQUEST,
                            "incomplete_upload",
                            "Request body ended before Content-Length bytes were received",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary_path, destination)
            except FileExistsError as exc:
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "call_already_exists",
                    "call_id already exists",
                ) from exc
            return digest.hexdigest()
        finally:
            temporary_path.unlink(missing_ok=True)

    def _status_payload(self, call_id: str) -> dict[str, object] | None:
        result = self._read_completed_result(call_id)
        staged_audio_exists = self._staged_audio_exists(call_id)
        snapshot: QueueSnapshot | None = None
        if self.database_path.is_symlink():
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "unsafe_storage",
                "Configured database must not be a symlink",
            )
        if self.database_path.exists():
            with QueueStore(self.database_path) as store:
                snapshot = store.snapshot_for(call_id)
        if result is not None:
            status = "completed"
        elif snapshot is not None:
            if snapshot.status == "completed" and staged_audio_exists:
                status = "new"
            elif snapshot.status == "completed":
                status = "failed"
            else:
                status = snapshot.status
        elif staged_audio_exists:
            status = "new"
        else:
            return None

        error: dict[str, str] | None = None
        if snapshot is not None and snapshot.error_type is not None:
            error = {
                "type": snapshot.error_type,
                "message": "Processing failed; consult local technical logs",
            }
        if (
            result is None
            and snapshot is not None
            and snapshot.status == "completed"
            and not staged_audio_exists
        ):
            error = {
                "type": "ResultMissing",
                "message": "Queue is completed but the canonical result is missing",
            }
        status_url = f"/v1/jobs/{call_id}"
        return {
            "api_version": API_VERSION,
            "call_id": call_id,
            "status": status,
            "attempts": snapshot.attempts if snapshot is not None else 0,
            "max_attempts": snapshot.max_attempts if snapshot is not None else None,
            "created_at": _timestamp(snapshot.created_at) if snapshot is not None else None,
            "updated_at": _timestamp(snapshot.updated_at) if snapshot is not None else None,
            "completed_at": _timestamp(snapshot.completed_at) if snapshot is not None else None,
            "result_url": f"{status_url}/result" if status == "completed" else None,
            "error": error,
        }

    def _completed_result(self, call_id: str) -> dict[str, object]:
        result = self._read_completed_result(call_id)
        if result is not None:
            return result
        status = self._status_payload(call_id)
        if status is None:
            raise ApiProblem(HTTPStatus.NOT_FOUND, "job_not_found", "Job was not found")
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "result_not_ready",
            f"Result is not ready; current status is {status['status']}",
        )

    def _read_completed_result(self, call_id: str) -> dict[str, object] | None:
        call_dir = self.calls_dir / call_id
        if self.calls_dir.is_symlink() or call_dir.is_symlink():
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "unsafe_storage",
                "Configured result storage must not be a symlink",
            )
        path = call_dir / f"{call_id}.json"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "invalid_result",
                "Stored result is unreadable",
            ) from exc
        if not isinstance(payload, dict) or payload.get("call_id") != call_id:
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "invalid_result",
                "Stored result does not match call_id",
            )
        if payload.get("status") != "completed":
            return None
        return payload

    def _staged_audio_exists(self, call_id: str) -> bool:
        if self.input_dir.is_symlink():
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "unsafe_storage",
                "Configured staging storage must not be a symlink",
            )
        return any(
            (self.input_dir / f"{call_id}{extension}").is_file()
            and not (self.input_dir / f"{call_id}{extension}").is_symlink()
            for extension in SUPPORTED_EXTENSIONS
        )


class CrmApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], application: CrmApiApplication) -> None:
        self.application = application
        super().__init__(server_address, CrmApiRequestHandler)


class CrmApiRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalCallTranscriberAPI/1.0"

    @property
    def application(self) -> CrmApiApplication:
        server = self.server
        assert isinstance(server, CrmApiServer)
        return server.application

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(60.0)

    def do_GET(self) -> None:
        if not self._host_is_valid():
            self._send_invalid_host()
            return
        self._send(self.application.handle_get(self.path))

    def do_PUT(self) -> None:
        if not self._host_is_valid():
            self._send_invalid_host()
            return
        self._send(self.application.handle_put(self.path, self.headers, self.rfile))

    def do_POST(self) -> None:
        if not self._host_is_valid():
            self._send_invalid_host()
            return
        self._send(
            _problem_response(
                ApiProblem(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "Use PUT for uploads and GET for status or result",
                )
            )
        )

    def _host_is_valid(self) -> bool:
        values = self.headers.get_all("Host") or []
        port = self.server.server_address[1]
        expected = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return len(values) == 1 and values[0].strip().lower() in expected

    def _send_invalid_host(self) -> None:
        self._send(
            _problem_response(
                ApiProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_host",
                    "Host must identify this loopback server and its actual port",
                )
            )
        )

    def _send(self, response: ApiResponse) -> None:
        self.close_connection = True
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response.body)
        LOGGER.info("api_request method=%s status=%d", self.command, response.status)

    def log_message(self, format: str, *args: object) -> None:
        return
