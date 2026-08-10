#!/usr/bin/env python3
"""Entry point for folder, watch and batch processing modes."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.worker_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

