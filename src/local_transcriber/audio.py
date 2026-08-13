"""Local audio decoding adapters not provided by the audited T-one helper."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .errors import AudioDecodeError, DependencyUnavailableError

LOGGER = logging.getLogger("local_transcriber.audio")
AudioReader = Callable[[Path | str], np.ndarray]
MAX_AUDIO_DURATION_SECONDS = 4 * 60 * 60


def read_local_audio(path_to_file: Path | str, *, fallback: AudioReader) -> np.ndarray:
    """Decode a supported local file to mono signed 16-bit samples at 8 kHz."""

    path = Path(path_to_file)
    if path.suffix.lower() != ".aac":
        return fallback(path)
    return _read_aac(path)


def _read_aac(path: Path) -> np.ndarray:
    try:
        import av
    except ImportError as exc:
        raise DependencyUnavailableError(
            "AAC decoding requires the pinned local PyAV wheel. "
            "Install requirements/aac.txt during staging; runtime will not download it."
        ) from exc

    chunks: list[np.ndarray] = []
    sample_count = 0
    max_samples = MAX_AUDIO_DURATION_SECONDS * 8000
    skipped_packets = 0
    resolved_path = path.resolve(strict=True)

    try:
        with av.open(str(resolved_path), mode="r", format="aac") as container:
            if not container.streams.audio:
                raise AudioDecodeError("AAC source contains no audio stream")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)

            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError:
                    skipped_packets += 1
                    continue
                for frame in frames:
                    for converted in resampler.resample(frame):
                        chunk = converted.to_ndarray().reshape(-1)
                        if sample_count + len(chunk) > max_samples:
                            raise AudioDecodeError(
                                f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                            )
                        chunks.append(chunk)
                        sample_count += len(chunk)

            for converted in resampler.resample(None):
                chunk = converted.to_ndarray().reshape(-1)
                if sample_count + len(chunk) > max_samples:
                    raise AudioDecodeError(
                        f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                    )
                chunks.append(chunk)
                sample_count += len(chunk)
    except AudioDecodeError:
        raise
    except Exception as exc:
        raise AudioDecodeError(f"Cannot decode local AAC source: {exc}") from exc

    if not chunks:
        raise AudioDecodeError("AAC source contains no decodable audio frames")
    if skipped_packets:
        LOGGER.warning(
            "aac_decode_skipped_invalid_packets file=%s count=%d",
            path.name,
            skipped_packets,
        )

    return np.concatenate(chunks).astype(np.int32, copy=False)
