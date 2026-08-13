"""Folder scanner and single-model queue worker."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .engine import AsrEngine, create_engine
from .errors import InputValidationError, TranscriberError
from .queue import QueueConflictError, QueueJob, QueueStore
from .service import SUPPORTED_EXTENSIONS, TranscriptionRequest, extract_call_id, transcribe_file

LOGGER = logging.getLogger("local_transcriber.worker")
RETRYABLE_ERRORS = {"AudioDecodeError", "InferenceError", "OSError", "TimeoutError"}


@dataclass(frozen=True)
class WorkerConfig:
    input_dir: Path
    calls_dir: Path
    failed_dir: Path
    database_path: Path
    model_dir: Path
    engine_name: str = "whisper"
    whisper_cli_path: Path | None = None
    vad_model_path: Path | None = None
    initial_prompt: str | None = None
    decoder: str = "beam_search"
    stable_seconds: float = 5.0
    poll_seconds: float = 2.0
    lease_seconds: float = 3600.0
    max_attempts: int = 3
    retry_base_seconds: float = 30.0


class LeaseHeartbeat:
    """Renew a processing lease from an independent SQLite connection."""

    def __init__(self, database_path: Path, job: QueueJob, lease_seconds: float) -> None:
        self.database_path = database_path
        self.job = job
        self.lease_seconds = lease_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"lease-{job.call_id}",
            daemon=True,
        )

    def __enter__(self) -> "LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(1.0, min(10.0, self.lease_seconds)))

    def _run(self) -> None:
        interval = max(0.1, min(30.0, self.lease_seconds / 3.0))
        try:
            with QueueStore(self.database_path) as heartbeat_store:
                while not self.stop_event.wait(interval):
                    renewed = heartbeat_store.renew_lease(
                        self.job.id,
                        str(self.job.lease_owner),
                        now=time.time(),
                        lease_seconds=self.lease_seconds,
                    )
                    if not renewed:
                        LOGGER.error("lease_heartbeat_lost call_id=%s", self.job.call_id)
                        return
        except Exception as exc:
            LOGGER.error(
                "lease_heartbeat_error call_id=%s error_type=%s",
                self.job.call_id,
                type(exc).__name__,
            )


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


class FolderWorker:
    """One worker process; its injected engine is reused for every job."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        store: QueueStore | None = None,
        engine: AsrEngine | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.config = config
        self.store = store or QueueStore(config.database_path)
        self.engine = engine or create_engine(
            config.engine_name,
            config.model_dir,
            decoder=config.decoder,
            scratch_dir=config.calls_dir / ".tmp",
            whisper_cli_path=config.whisper_cli_path,
            vad_model_path=config.vad_model_path,
            initial_prompt=config.initial_prompt,
        )
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"

    def close(self) -> None:
        self.store.close()

    def scan(self, *, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        counts = {status: 0 for status in ("new", "queued", "processing", "completed", "failed")}
        self.config.input_dir.mkdir(parents=True, exist_ok=True)
        self.config.calls_dir.mkdir(parents=True, exist_ok=True)
        for input_path in sorted(self.config.input_dir.iterdir()):
            if input_path.is_symlink() or not input_path.is_file():
                continue
            if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                if not (
                    input_path.name.startswith(".crm-upload-")
                    and input_path.suffix.lower() == ".part"
                ):
                    self._record_rejected_source(
                        input_path,
                        "UnsupportedAudioExtension",
                        "Unsupported audio extension; use WAV, MP3, FLAC, OGG or AAC",
                    )
                continue
            try:
                call_id = extract_call_id(input_path)
            except InputValidationError:
                self._record_rejected_source(
                    input_path,
                    "InvalidCallId",
                    "Filename does not provide a safe call_id",
                )
                continue
            try:
                call_dir = self.config.calls_dir / call_id
                call_dir.mkdir(parents=True, exist_ok=True)
                output_path = call_dir / f"{call_id}.json"
                status = self.store.observe(
                    input_path,
                    output_path,
                    now=now,
                    stable_seconds=self.config.stable_seconds,
                    max_attempts=self.config.max_attempts,
                )
                counts[status] += 1
            except (QueueConflictError, TranscriberError, ValueError, OSError) as exc:
                LOGGER.error("scan_rejected file=%s error_type=%s", input_path.name, type(exc).__name__)
        return counts

    def process_one(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        job = self.store.claim(
            self.worker_id,
            now=now,
            lease_seconds=self.config.lease_seconds,
        )
        if job is None:
            return False
        LOGGER.info("job_processing call_id=%s attempt=%d", job.call_id, job.attempts)
        try:
            located_input = self._locate_input(job)
            stat = located_input.stat()
            if stat.st_size != job.observed_size or stat.st_mtime_ns != job.observed_mtime_ns:
                if located_input.resolve(strict=False) == job.input_path.resolve(strict=False):
                    self.store.release_changed_file(job, now=time.time())
                    LOGGER.info("job_returned_new call_id=%s reason=file_changed", job.call_id)
                    return True
                raise QueueConflictError("Audio changed after it was moved into the call folder")
            job = self._organize_input(job)
            with LeaseHeartbeat(self.store.database_path, job, self.config.lease_seconds):
                result = transcribe_file(
                    TranscriptionRequest(
                        input_path=job.input_path,
                        output_path=job.output_path,
                        model_dir=self.config.model_dir,
                        decoder=self.config.decoder,
                        markdown_output_path=job.output_path.with_suffix(".md"),
                        overwrite=True,
                    ),
                    engine=self.engine,
                )
            finished_at = time.time()
            if result["status"] == "completed":
                self.store.complete(job, now=finished_at)
                (self.config.failed_dir / f"{job.call_id}.json").unlink(missing_ok=True)
                LOGGER.info(
                    "job_completed call_id=%s duration_seconds=%s processing_seconds=%s rtf=%s",
                    job.call_id,
                    result["duration_seconds"],
                    result["processing_seconds"],
                    result["real_time_factor"],
                )
                return True

            error = result.get("error") or {"type": "UnknownError", "message": "unknown failure"}
            error_type = str(error.get("type", "UnknownError"))
            error_message = str(error.get("message", "unknown failure"))
            final_status = self.store.fail(
                job,
                error_type=error_type,
                error_message=error_message,
                retryable=error_type in RETRYABLE_ERRORS,
                now=finished_at,
                retry_base_seconds=self.config.retry_base_seconds,
            )
            LOGGER.error(
                "job_failed call_id=%s status=%s attempt=%d error_type=%s",
                job.call_id,
                final_status,
                job.attempts,
                error_type,
            )
            if final_status == "failed":
                self._write_failed_marker(job, error_type, error_message, finished_at)
            return True
        except Exception as exc:
            finished_at = time.time()
            final_status = self.store.fail(
                job,
                error_type=type(exc).__name__,
                error_message=str(exc),
                retryable=isinstance(exc, OSError),
                now=finished_at,
                retry_base_seconds=self.config.retry_base_seconds,
            )
            LOGGER.exception(
                "job_worker_error call_id=%s status=%s error_type=%s",
                job.call_id,
                final_status,
                type(exc).__name__,
            )
            if final_status == "failed":
                self._write_failed_marker(job, type(exc).__name__, str(exc), finished_at)
            return True

    def _locate_input(self, job: QueueJob) -> Path:
        current = job.input_path.resolve(strict=False)
        if current.is_file() and not current.is_symlink():
            return current
        canonical = (self.config.calls_dir / job.call_id / job.input_path.name).resolve(strict=False)
        if canonical.is_file() and not canonical.is_symlink():
            return canonical
        raise FileNotFoundError(f"Queued input is missing: {job.input_path.name}")

    def _organize_input(self, job: QueueJob) -> QueueJob:
        """Move a stable source into its call folder without changing its bytes."""

        call_dir = self.config.calls_dir / job.call_id
        call_dir.mkdir(parents=True, exist_ok=True)
        destination = call_dir / job.input_path.name
        current = job.input_path.resolve(strict=False)
        canonical = destination.resolve(strict=False)
        if current == canonical:
            return job

        if current.exists():
            if current.is_symlink() or not current.is_file():
                raise QueueConflictError("Queued input is no longer a regular file")
            if canonical.exists():
                raise QueueConflictError("Call folder already contains an audio file with this name")
            current.replace(canonical)
        elif not canonical.is_file() or canonical.is_symlink():
            raise FileNotFoundError(f"Queued input is missing: {job.input_path.name}")

        return self.store.relocate_input(job, canonical, now=time.time())

    def drain(self) -> int:
        processed = 0
        while True:
            retried, failed = self.store.recover_expired(now=time.time())
            if retried or failed:
                LOGGER.warning("lease_recovery requeued=%d failed=%d", retried, failed)
            if not self.process_one():
                break
            processed += 1
        return processed

    def run_one_shot(self) -> int:
        self.scan()
        if self.config.stable_seconds > 0:
            time.sleep(self.config.stable_seconds)
        self.scan()
        return self.drain()

    def run_loop(self) -> None:
        while True:
            self.scan()
            self.drain()
            time.sleep(self.config.poll_seconds)

    def _record_rejected_source(
        self,
        input_path: Path,
        error_type: str,
        message: str,
    ) -> None:
        try:
            self._write_rejected_marker(input_path, error_type, message)
        except OSError as exc:
            LOGGER.error("rejected_marker_error error_type=%s", type(exc).__name__)

    def _write_rejected_marker(
        self,
        input_path: Path,
        error_type: str,
        message: str,
    ) -> None:
        if self.config.failed_dir.is_symlink():
            raise OSError("Failed marker directory must not be a symlink")
        self.config.failed_dir.mkdir(parents=True, exist_ok=True)
        token = hashlib.sha256(input_path.name.encode("utf-8")).hexdigest()[:12]
        final_path = self.config.failed_dir / f"rejected-{token}.json"
        if final_path.is_symlink() or (final_path.exists() and not final_path.is_file()):
            raise OSError("Failed marker path is unsafe")
        payload = {
            "status": "failed",
            "filename": input_path.name,
            "error_type": error_type[:100],
            "message": " ".join(message.split())[:500],
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if final_path.is_file():
            try:
                if final_path.read_text(encoding="utf-8") == serialized:
                    return
            except (OSError, UnicodeError):
                pass
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.config.failed_dir,
            prefix=f".rejected-{token}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                destination.write(serialized)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary_path, final_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _write_failed_marker(
        self,
        job: QueueJob,
        error_type: str,
        error_message: str,
        failed_at: float,
    ) -> None:
        self.config.failed_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "call_id": job.call_id,
            "status": "failed",
            "source_audio": job.input_path.name,
            "attempts": job.attempts,
            "failed_at_unix": failed_at,
            "error": {
                "type": error_type[:100],
                "message": " ".join(error_message.split())[:500],
            },
        }
        final_path = self.config.failed_dir / f"{job.call_id}.json"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.config.failed_dir,
            prefix=f".{job.call_id}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(payload, destination, ensure_ascii=False, indent=2)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary_path, final_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
