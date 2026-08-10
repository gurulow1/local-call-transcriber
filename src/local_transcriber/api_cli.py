"""Command line entry point for the loopback CRM reference API."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .crm_api import DEFAULT_MAX_UPLOAD_BYTES, CrmApiApplication, CrmApiServer
from .worker import configure_logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Loopback-only reference API for CRM integration",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "data" / "input")
    parser.add_argument("--calls-dir", type=Path, default=PROJECT_ROOT / "data" / "calls")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "queue.sqlite3")
    parser.add_argument("--log", type=Path, default=PROJECT_ROOT / "logs" / "crm-api.log")
    parser.add_argument("--max-upload-bytes", type=int, default=DEFAULT_MAX_UPLOAD_BYTES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if args.max_upload_bytes < 1:
        raise SystemExit("max-upload-bytes must be positive")
    configure_logging(args.log)
    application = CrmApiApplication(
        input_dir=args.input_dir,
        calls_dir=args.calls_dir,
        database_path=args.database,
        max_upload_bytes=args.max_upload_bytes,
    )
    server = CrmApiServer(("127.0.0.1", args.port), application)
    print(f"crm_api=http://127.0.0.1:{args.port}")
    print("reference_only=true authentication=not_implemented exposure=loopback")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0
