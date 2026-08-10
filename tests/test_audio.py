from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from local_transcriber.audio import read_local_audio

PYAV_AVAILABLE = importlib.util.find_spec("av") is not None


class LocalAudioTests(unittest.TestCase):
    def test_non_aac_uses_existing_decoder(self) -> None:
        expected = np.array([1, -2, 3], dtype=np.int32)
        seen: list[Path | str] = []

        def fallback(path: Path | str) -> np.ndarray:
            seen.append(path)
            return expected

        result = read_local_audio(Path("sample.flac"), fallback=fallback)

        self.assertIs(result, expected)
        self.assertEqual(seen, [Path("sample.flac")])

    @unittest.skipUnless(PYAV_AVAILABLE, "optional PyAV AAC decoder is not installed")
    def test_synthetic_aac_is_decoded_to_mono_8khz_int32(self) -> None:
        import av

        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "sample.aac"
            container = av.open(str(source), mode="w", format="adts")
            stream = container.add_stream("aac", rate=48000)
            stream.layout = "stereo"
            stream.bit_rate = 64000
            frame = av.AudioFrame.from_ndarray(
                np.zeros((2, 48000), dtype=np.float32),
                format="fltp",
                layout="stereo",
            )
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
            container.close()

            result = read_local_audio(
                source,
                fallback=lambda _: self.fail("AAC must not use the miniaudio fallback"),
            )

            self.assertEqual(result.dtype, np.int32)
            self.assertEqual(result.ndim, 1)
            self.assertGreaterEqual(len(result), 8000)
            self.assertLessEqual(len(result), 8400)


if __name__ == "__main__":
    unittest.main()
