#!/usr/bin/env python3
"""Generate deterministic local non-speech WAV fixtures for hallucination checks."""

from __future__ import annotations

import argparse
import math
import random
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
DURATION_SECONDS = 10


def _write_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(SAMPLE_RATE)
        destination.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    count = SAMPLE_RATE * DURATION_SECONDS
    random_source = random.Random(20260813)
    fixtures = {
        "silence.wav": [0] * count,
        "quiet-noise.wav": [random_source.randint(-400, 400) for _ in range(count)],
        "loud-noise.wav": [random_source.randint(-4000, 4000) for _ in range(count)],
        "tone.wav": [
            round(5000 * math.sin(2 * math.pi * 440 * index / SAMPLE_RATE))
            for index in range(count)
        ],
    }
    for filename, samples in fixtures.items():
        _write_wav(args.output_dir / filename, samples)
        print(args.output_dir / filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
