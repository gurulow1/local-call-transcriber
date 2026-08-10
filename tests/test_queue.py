from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.engine import EngineModelInfo, EngineResult, Segment
from local_transcriber.queue import QueueConflictError, QueueStore
from local_transcriber.worker import FolderWorker, WorkerConfig


class CountingEngine:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_info(self) -> EngineModelInfo:
        return EngineModelInfo(
            name="T-one",
            version="fake",
            source_revision="fake-model",
            source_code_revision="fake-code",
            decoder="greedy",
            local_path="models/fake",
        )

    def transcribe(self, input_path: Path) -> EngineResult:
        self.calls += 1
        return EngineResult(
            duration_seconds=1.0,
            segments=(Segment(start=0.0, end=0.9, text="секретный тест"),),
        )


class QueueStoreTests(unittest.TestCase):
    def test_file_must_be_stable_before_atomic_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"audio")
            output = root / "1234.json"
            with QueueStore(root / "queue.sqlite3") as store:
                self.assertEqual(
                    store.observe(source, output, now=100.0, stable_seconds=2.0, max_attempts=3),
                    "new",
                )
                self.assertEqual(
                    store.observe(source, output, now=101.0, stable_seconds=2.0, max_attempts=3),
                    "new",
                )
                self.assertEqual(
                    store.observe(source, output, now=102.0, stable_seconds=2.0, max_attempts=3),
                    "queued",
                )
                first = store.claim("worker-1", now=102.0, lease_seconds=10.0)
                self.assertIsNotNone(first)
                assert first is not None
                self.assertEqual(first.status, "processing")
                self.assertEqual(first.attempts, 1)
                with QueueStore(root / "queue.sqlite3") as second_store:
                    self.assertIsNone(second_store.claim("worker-2", now=102.0, lease_seconds=10.0))
                store.complete(first, now=103.0)
                self.assertEqual(store.status_for("1234"), "completed")

    def test_changed_file_returns_to_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"first")
            output = root / "1234.json"
            with QueueStore(root / "queue.sqlite3") as store:
                store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=3)
                self.assertEqual(
                    store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=3),
                    "queued",
                )
                source.write_bytes(b"changed-size")
                self.assertEqual(
                    store.observe(source, output, now=1.0, stable_seconds=0.0, max_attempts=3),
                    "new",
                )

    def test_expired_lease_requeues_and_retry_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"audio")
            output = root / "1234.json"
            with QueueStore(root / "queue.sqlite3") as store:
                store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=2)
                store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=2)
                first = store.claim("worker-1", now=0.0, lease_seconds=1.0)
                assert first is not None
                self.assertEqual(store.recover_expired(now=2.0), (1, 0))
                second = store.claim("worker-2", now=2.0, lease_seconds=1.0)
                assert second is not None
                self.assertEqual(second.attempts, 2)
                final_status = store.fail(
                    second,
                    error_type="InferenceError",
                    error_message="temporary but retry limit reached",
                    retryable=True,
                    now=2.5,
                    retry_base_seconds=5.0,
                )
                self.assertEqual(final_status, "failed")
                self.assertEqual(store.status_for("1234"), "failed")
                self.assertTrue(store.requeue_failed("1234", now=3.0))
                self.assertEqual(store.status_for("1234"), "queued")
                self.assertFalse(store.requeue_failed("1234", now=4.0))

    def test_existing_completed_json_prevents_reprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "1234.wav"
            source.write_bytes(b"audio")
            output = root / "1234.json"
            output.write_text(
                json.dumps({"call_id": "1234", "status": "completed"}),
                encoding="utf-8",
            )
            with QueueStore(root / "queue.sqlite3") as store:
                self.assertEqual(
                    store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=3),
                    "completed",
                )
                self.assertIsNone(store.claim("worker", now=1.0, lease_seconds=10.0))

    def test_stale_completed_row_without_result_can_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input" / "1234.wav"
            source.parent.mkdir()
            source.write_bytes(b"audio")
            old_output = root / "output" / "1234.json"
            old_output.parent.mkdir()
            old_output.write_text(
                json.dumps({"call_id": "1234", "status": "completed"}),
                encoding="utf-8",
            )
            new_output = root / "calls" / "1234" / "1234.json"
            with QueueStore(root / "queue.sqlite3") as store:
                self.assertEqual(
                    store.observe(source, old_output, now=0.0, stable_seconds=0.0, max_attempts=3),
                    "completed",
                )
                old_output.unlink()
                self.assertEqual(
                    store.observe(source, new_output, now=1.0, stable_seconds=0.0, max_attempts=3),
                    "new",
                )
                self.assertEqual(
                    store.observe(source, new_output, now=1.0, stable_seconds=0.0, max_attempts=3),
                    "queued",
                )
                job = store.claim("worker", now=1.0, lease_seconds=10.0)
                assert job is not None
                self.assertEqual(job.output_path, new_output.resolve(strict=False))

    def test_duplicate_call_id_from_different_extensions_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            first = root / "1234.wav"
            second = root / "1234.flac"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with QueueStore(root / "queue.sqlite3") as store:
                store.observe(first, root / "1234.json", now=0.0, stable_seconds=0.0, max_attempts=3)
                with self.assertRaises(QueueConflictError):
                    store.observe(
                        second,
                        root / "1234.json",
                        now=1.0,
                        stable_seconds=0.0,
                        max_attempts=3,
                    )

    def test_one_hundred_jobs_pass_queue_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            with QueueStore(root / "queue.sqlite3") as store:
                for index in range(100):
                    source = root / f"q{index:03d}.wav"
                    source.write_bytes(b"synthetic")
                    output = root / f"q{index:03d}.json"
                    store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=3)
                    store.observe(source, output, now=0.0, stable_seconds=0.0, max_attempts=3)

                seen: set[str] = set()
                while True:
                    job = store.claim("single-worker", now=1.0, lease_seconds=60.0)
                    if job is None:
                        break
                    self.assertNotIn(job.call_id, seen)
                    seen.add(job.call_id)
                    store.complete(job, now=2.0)

                self.assertEqual(len(seen), 100)
                self.assertTrue(all(store.status_for(call_id) == "completed" for call_id in seen))


class FolderWorkerTests(unittest.TestCase):
    def test_worker_reuses_one_engine_and_completes_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            input_dir = root / "input"
            calls_dir = root / "calls"
            input_dir.mkdir()
            (input_dir / "1001.wav").write_bytes(b"one")
            (input_dir / "1002.flac").write_bytes(b"two")
            config = WorkerConfig(
                input_dir=input_dir,
                calls_dir=calls_dir,
                failed_dir=root / "failed",
                database_path=root / "queue.sqlite3",
                model_dir=root / "models",
                decoder="greedy",
                stable_seconds=0.0,
                lease_seconds=60.0,
            )
            engine = CountingEngine()
            worker = FolderWorker(config, engine=engine, worker_id="test-worker")
            try:
                worker.scan(now=0.0)
                worker.scan(now=0.0)
                self.assertEqual(worker.drain(), 2)
                self.assertEqual(engine.calls, 2)
                self.assertEqual(worker.store.status_for("1001"), "completed")
                self.assertEqual(worker.store.status_for("1002"), "completed")
                self.assertFalse((input_dir / "1001.wav").exists())
                self.assertEqual((calls_dir / "1001" / "1001.wav").read_bytes(), b"one")
                self.assertEqual(
                    json.loads(
                        (calls_dir / "1001" / "1001.json").read_text(encoding="utf-8")
                    )["status"],
                    "completed",
                )
                markdown = (calls_dir / "1001" / "1001.md").read_text(encoding="utf-8")
                self.assertIn("# Расшифровка: 1001", markdown)
                self.assertIn("Секретный тест.", markdown)
            finally:
                worker.close()

    def test_worker_recovers_if_audio_was_moved_before_queue_path_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            input_dir = root / "input"
            calls_dir = root / "calls"
            input_dir.mkdir()
            source = input_dir / "1001.wav"
            source.write_bytes(b"audio")
            config = WorkerConfig(
                input_dir=input_dir,
                calls_dir=calls_dir,
                failed_dir=root / "failed",
                database_path=root / "queue.sqlite3",
                model_dir=root / "models",
                decoder="greedy",
                stable_seconds=0.0,
                lease_seconds=60.0,
            )
            worker = FolderWorker(config, engine=CountingEngine(), worker_id="test-worker")
            try:
                worker.scan(now=0.0)
                worker.scan(now=0.0)
                call_dir = calls_dir / "1001"
                call_dir.mkdir(parents=True, exist_ok=True)
                source.replace(call_dir / source.name)

                self.assertTrue(worker.process_one(now=1.0))
                stored_job = worker.store.job_for("1001")
                assert stored_job is not None
                self.assertEqual(stored_job.input_path, (call_dir / "1001.wav").resolve())
                self.assertEqual(stored_job.status, "completed")
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
