"""Durable SQLite queue for completed audio files."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .service import extract_call_id

VALID_STATUSES = ("new", "queued", "processing", "completed", "failed")


class QueueConflictError(RuntimeError):
    """The same call_id was observed at two different input paths."""


@dataclass(frozen=True)
class QueueJob:
    id: int
    call_id: str
    input_path: Path
    output_path: Path
    status: str
    attempts: int
    max_attempts: int
    observed_size: int
    observed_mtime_ns: int
    lease_owner: str | None


def _safe_error_message(message: str, limit: int = 500) -> str:
    return " ".join(message.split())[:limit]


def _is_completed_output(path: Path, call_id: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload.get("call_id") == call_id and payload.get("status") == "completed"


class QueueStore:
    """Transactional queue state shared safely by multiple worker processes."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path.resolve(strict=False)
        self.connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "QueueStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL UNIQUE,
                input_path TEXT NOT NULL UNIQUE,
                output_path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN ('new','queued','processing','completed','failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                observed_size INTEGER NOT NULL,
                observed_mtime_ns INTEGER NOT NULL,
                stable_since REAL NOT NULL,
                available_at REAL NOT NULL,
                lease_owner TEXT,
                lease_expires_at REAL,
                last_error_type TEXT,
                last_error_message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, available_at, id)"
        )

    def observe(
        self,
        input_path: Path,
        output_path: Path,
        *,
        now: float,
        stable_seconds: float,
        max_attempts: int,
    ) -> str:
        """Record an observation and queue the file only after it is stable."""

        input_path = input_path.resolve(strict=True)
        output_path = output_path.resolve(strict=False)
        call_id = extract_call_id(input_path)
        stat = input_path.stat()
        completed_output = _is_completed_output(output_path, call_id)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                status = "completed" if completed_output else "new"
                self.connection.execute(
                    """
                    INSERT INTO jobs (
                        call_id, input_path, output_path, status, attempts, max_attempts,
                        observed_size, observed_mtime_ns, stable_since, available_at,
                        created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        call_id,
                        str(input_path),
                        str(output_path),
                        status,
                        max_attempts,
                        stat.st_size,
                        stat.st_mtime_ns,
                        now,
                        now,
                        now,
                        now,
                        now if completed_output else None,
                    ),
                )
                self.connection.execute("COMMIT")
                return status

            if row["input_path"] != str(input_path):
                raise QueueConflictError(
                    f"call_id {call_id!r} already belongs to a different input file"
                )
            if completed_output and row["status"] != "completed":
                self.connection.execute(
                    """
                    UPDATE jobs SET status='completed', completed_at=?, updated_at=?,
                        lease_owner=NULL, lease_expires_at=NULL,
                        last_error_type=NULL, last_error_message=NULL
                    WHERE id=?
                    """,
                    (now, now, row["id"]),
                )
                self.connection.execute("COMMIT")
                return "completed"
            if row["status"] in {"processing", "completed"}:
                self.connection.execute("COMMIT")
                return str(row["status"])

            unchanged = (
                row["observed_size"] == stat.st_size
                and row["observed_mtime_ns"] == stat.st_mtime_ns
            )
            if not unchanged:
                self.connection.execute(
                    """
                    UPDATE jobs SET status='new', attempts=0, max_attempts=?,
                        observed_size=?, observed_mtime_ns=?, stable_since=?, available_at=?,
                        lease_owner=NULL, lease_expires_at=NULL,
                        last_error_type=NULL, last_error_message=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        max_attempts,
                        stat.st_size,
                        stat.st_mtime_ns,
                        now,
                        now,
                        now,
                        row["id"],
                    ),
                )
                self.connection.execute("COMMIT")
                return "new"

            status = str(row["status"])
            if status == "new" and now - float(row["stable_since"]) >= stable_seconds:
                status = "queued"
                self.connection.execute(
                    "UPDATE jobs SET status='queued', available_at=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
            else:
                self.connection.execute(
                    "UPDATE jobs SET updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
            self.connection.execute("COMMIT")
            return status
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def recover_expired(self, *, now: float) -> tuple[int, int]:
        """Requeue or permanently fail jobs whose worker lease expired."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            retryable = self.connection.execute(
                """
                UPDATE jobs SET status='queued', available_at=?, updated_at=?,
                    lease_owner=NULL, lease_expires_at=NULL,
                    last_error_type='WorkerLeaseExpired',
                    last_error_message='Worker lease expired; task requeued'
                WHERE status='processing' AND lease_expires_at <= ? AND attempts < max_attempts
                """,
                (now, now, now),
            ).rowcount
            failed = self.connection.execute(
                """
                UPDATE jobs SET status='failed', updated_at=?,
                    lease_owner=NULL, lease_expires_at=NULL,
                    last_error_type='WorkerLeaseExpired',
                    last_error_message='Worker lease expired; retry limit reached'
                WHERE status='processing' AND lease_expires_at <= ? AND attempts >= max_attempts
                """,
                (now, now),
            ).rowcount
            self.connection.execute("COMMIT")
            return retryable, failed
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def claim(self, worker_id: str, *, now: float, lease_seconds: float) -> QueueJob | None:
        """Atomically claim at most one available job."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE status='queued' AND available_at <= ?
                ORDER BY available_at, id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            updated = self.connection.execute(
                """
                UPDATE jobs SET status='processing', attempts=attempts+1,
                    lease_owner=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (worker_id, now + lease_seconds, now, row["id"]),
            ).rowcount
            if updated != 1:
                self.connection.execute("ROLLBACK")
                return None
            claimed = self.connection.execute(
                "SELECT * FROM jobs WHERE id=?",
                (row["id"],),
            ).fetchone()
            self.connection.execute("COMMIT")
            return self._to_job(claimed)
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def release_changed_file(self, job: QueueJob, *, now: float) -> None:
        stat = job.input_path.stat()
        self.connection.execute(
            """
            UPDATE jobs SET status='new', attempts=MAX(attempts-1, 0),
                observed_size=?, observed_mtime_ns=?, stable_since=?, available_at=?,
                lease_owner=NULL, lease_expires_at=NULL, updated_at=?
            WHERE id=? AND status='processing' AND lease_owner=?
            """,
            (
                stat.st_size,
                stat.st_mtime_ns,
                now,
                now,
                now,
                job.id,
                job.lease_owner,
            ),
        )

    def complete(self, job: QueueJob, *, now: float) -> None:
        self.connection.execute(
            """
            UPDATE jobs SET status='completed', completed_at=?, updated_at=?,
                lease_owner=NULL, lease_expires_at=NULL,
                last_error_type=NULL, last_error_message=NULL
            WHERE id=? AND status='processing' AND lease_owner=?
            """,
            (now, now, job.id, job.lease_owner),
        )

    def renew_lease(
        self,
        job_id: int,
        worker_id: str,
        *,
        now: float,
        lease_seconds: float,
    ) -> bool:
        updated = self.connection.execute(
            """
            UPDATE jobs SET lease_expires_at=?, updated_at=?
            WHERE id=? AND status='processing' AND lease_owner=?
            """,
            (now + lease_seconds, now, job_id, worker_id),
        ).rowcount
        return updated == 1

    def fail(
        self,
        job: QueueJob,
        *,
        error_type: str,
        error_message: str,
        retryable: bool,
        now: float,
        retry_base_seconds: float,
    ) -> str:
        should_retry = retryable and job.attempts < job.max_attempts
        status = "queued" if should_retry else "failed"
        available_at = now + retry_base_seconds * (2 ** max(0, job.attempts - 1))
        self.connection.execute(
            """
            UPDATE jobs SET status=?, available_at=?, updated_at=?,
                lease_owner=NULL, lease_expires_at=NULL,
                last_error_type=?, last_error_message=?
            WHERE id=? AND status='processing' AND lease_owner=?
            """,
            (
                status,
                available_at,
                now,
                _safe_error_message(error_type, 100),
                _safe_error_message(error_message),
                job.id,
                job.lease_owner,
            ),
        )
        return status

    def status_for(self, call_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM jobs WHERE call_id=?",
            (call_id,),
        ).fetchone()
        return str(row["status"]) if row else None

    def requeue_failed(self, call_id: str, *, now: float) -> bool:
        """Explicit administrative retry; completed jobs are never changed."""

        updated = self.connection.execute(
            """
            UPDATE jobs SET status='queued', attempts=0, available_at=?, updated_at=?,
                lease_owner=NULL, lease_expires_at=NULL,
                last_error_type=NULL, last_error_message=NULL
            WHERE call_id=? AND status='failed'
            """,
            (now, now, call_id),
        ).rowcount
        return updated == 1

    def job_for(self, call_id: str) -> QueueJob | None:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE call_id=?",
            (call_id,),
        ).fetchone()
        return self._to_job(row) if row else None

    @staticmethod
    def _to_job(row: sqlite3.Row) -> QueueJob:
        return QueueJob(
            id=int(row["id"]),
            call_id=str(row["call_id"]),
            input_path=Path(row["input_path"]),
            output_path=Path(row["output_path"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            observed_size=int(row["observed_size"]),
            observed_mtime_ns=int(row["observed_mtime_ns"]),
            lease_owner=row["lease_owner"],
        )
