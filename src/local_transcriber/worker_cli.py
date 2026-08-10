"""CLI for durable folder and batch processing modes."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

try:
    import resource
except ModuleNotFoundError:  # Windows
    resource = None  # type: ignore[assignment]

from .worker import FolderWorker, WorkerConfig, configure_logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local T-one folder worker with a durable SQLite queue")
    parser.add_argument(
        "--mode",
        choices=("once", "poll", "watch", "batch"),
        default="once",
        help="watch currently uses portable polling; batch is suitable for an external night scheduler",
    )
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "data" / "input")
    parser.add_argument(
        "--calls-dir",
        "--output-dir",
        dest="calls_dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "calls",
        help="Folder containing one subfolder per call (--output-dir is a compatibility alias)",
    )
    parser.add_argument("--failed-dir", type=Path, default=PROJECT_ROOT / "data" / "failed")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "queue.sqlite3")
    parser.add_argument("--model-dir", type=Path, default=PROJECT_ROOT / "models" / "t-one")
    parser.add_argument("--log", type=Path, default=PROJECT_ROOT / "logs" / "worker.log")
    parser.add_argument("--decoder", choices=("beam_search", "greedy"), default="beam_search")
    parser.add_argument("--stable-seconds", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=float, default=3600.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-base-seconds", type=float, default=30.0)
    parser.add_argument(
        "--requeue",
        metavar="CALL_ID",
        help="Explicitly reset one failed job before running the selected mode",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    started_at = time.perf_counter()
    cpu_started = time.process_time()
    usage_started = resource.getrusage(resource.RUSAGE_SELF) if resource is not None else None
    args = build_parser().parse_args(argv)
    if args.stable_seconds < 0 or args.poll_seconds <= 0 or args.lease_seconds <= 0:
        raise SystemExit("timing arguments must be positive (stable-seconds may be zero)")
    if args.max_attempts < 1 or args.retry_base_seconds < 0:
        raise SystemExit("max-attempts must be >= 1 and retry-base-seconds must be >= 0")
    configure_logging(args.log)
    config = WorkerConfig(
        input_dir=args.input_dir,
        calls_dir=args.calls_dir,
        failed_dir=args.failed_dir,
        database_path=args.database,
        model_dir=args.model_dir,
        decoder=args.decoder,
        stable_seconds=args.stable_seconds,
        poll_seconds=args.poll_seconds,
        lease_seconds=args.lease_seconds,
        max_attempts=args.max_attempts,
        retry_base_seconds=args.retry_base_seconds,
    )
    worker = FolderWorker(config)
    try:
        if args.requeue is not None:
            if not worker.store.requeue_failed(args.requeue, now=time.time()):
                raise SystemExit(f"failed job not found for explicit requeue: {args.requeue}")
            print(f"requeued={args.requeue}")
        if args.mode in {"once", "batch"}:
            processed = worker.run_one_shot()
            wall_seconds = time.perf_counter() - started_at
            cpu_seconds = time.process_time() - cpu_started
            peak_rss = "unavailable"
            if resource is not None and usage_started is not None:
                usage_finished = resource.getrusage(resource.RUSAGE_SELF)
                max_rss_bytes = int(usage_finished.ru_maxrss)
                if sys.platform != "darwin":
                    max_rss_bytes *= 1024
                cpu_seconds = (
                    usage_finished.ru_utime
                    - usage_started.ru_utime
                    + usage_finished.ru_stime
                    - usage_started.ru_stime
                )
                peak_rss = f"{max_rss_bytes / 1024 / 1024:.1f}"
            print(
                f"processed={processed} wall_seconds={wall_seconds:.3f} "
                f"cpu_seconds={cpu_seconds:.3f} peak_rss_mb={peak_rss}"
            )
            return 0
        worker.run_loop()
    except KeyboardInterrupt:
        return 0
    finally:
        worker.close()
    return 0
