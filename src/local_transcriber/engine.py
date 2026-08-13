"""Strict local adapters for the supported offline ASR engines."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .errors import (
    AudioDecodeError,
    DependencyUnavailableError,
    InferenceError,
    ModelValidationError,
)
from .model_manifest import ModelManifest
from .network import deny_python_network

LOGGER = logging.getLogger("local_transcriber.engine")
MAX_AUDIO_DURATION_SECONDS = 4 * 60 * 60


@dataclass(frozen=True)
class Segment:
    """One phrase-level ASR result."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class EngineResult:
    """Engine-neutral result used by the contract layer."""

    duration_seconds: float
    segments: Sequence[Segment]


@dataclass(frozen=True)
class EngineModelInfo:
    """Reproducible identity of the local model and decoder."""

    name: str
    version: str
    source_revision: str
    source_code_revision: str
    decoder: str
    local_path: str
    vad_name: str | None = None
    vad_version: str | None = None
    vad_source_revision: str | None = None
    vad_sha256: str | None = None
    vad_threshold: float | None = None


class AsrEngine(Protocol):
    """Minimal protocol that keeps contract tests independent of a heavy model."""

    @property
    def model_info(self) -> EngineModelInfo: ...

    def transcribe(self, input_path: Path) -> EngineResult: ...


class ToneEngine:
    """T-one engine that can only load pre-provisioned local artifacts."""

    SUPPORTED_DECODERS = {"greedy", "beam_search"}

    def __init__(
        self,
        model_dir: Path,
        *,
        decoder: str = "beam_search",
        verify_model_hashes: bool = False,
    ) -> None:
        self._model_dir = model_dir.resolve(strict=False)
        if decoder not in self.SUPPORTED_DECODERS:
            raise ModelValidationError(f"Unsupported decoder: {decoder!r}")
        self._decoder_name = decoder
        self._manifest = ModelManifest.load(self._model_dir, expected_name="T-one")
        self._manifest.validate_artifact(self._model_dir, "model.onnx", verify_hash=verify_model_hashes)
        if decoder == "beam_search":
            self._manifest.validate_artifact(self._model_dir, "kenlm.bin", verify_hash=verify_model_hashes)
        self._pipeline: object | None = None
        self._read_audio: object | None = None

    @property
    def model_info(self) -> EngineModelInfo:
        return EngineModelInfo(
            name=self._manifest.name,
            version=self._manifest.version,
            source_revision=self._manifest.source_revision,
            source_code_revision=self._manifest.source_code_revision,
            decoder=self._decoder_name,
            local_path=self._model_dir.name,
        )

    def _load(self) -> None:
        if self._pipeline is not None:
            return
        with deny_python_network():
            try:
                if self._decoder_name == "greedy":
                    logging.getLogger("pyctcdecode").setLevel(logging.ERROR)
                from .audio import read_local_audio
                from tone import DecoderType, StreamingCTCPipeline, read_audio
            except ImportError as exc:
                raise DependencyUnavailableError(
                    "Audited T-one and its runtime dependencies are not installed. "
                    "Install the pinned local checkout; runtime will not download them."
                ) from exc

            decoder_type = DecoderType.GREEDY if self._decoder_name == "greedy" else DecoderType.BEAM_SEARCH
            try:
                self._pipeline = StreamingCTCPipeline.from_local(
                    self._model_dir,
                    decoder_type=decoder_type,
                )
            except Exception as exc:
                raise ModelValidationError(f"Cannot load local T-one model: {exc}") from exc
            self._read_audio = lambda path: read_local_audio(path, fallback=read_audio)

    def transcribe(self, input_path: Path) -> EngineResult:
        self._load()
        assert self._pipeline is not None
        assert callable(self._read_audio)
        with deny_python_network():
            try:
                if input_path.suffix.lower() != ".aac":
                    declared_duration = _audio_duration(input_path.resolve(strict=True))
                    if declared_duration > MAX_AUDIO_DURATION_SECONDS:
                        raise AudioDecodeError(
                            f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                        )
                audio = self._read_audio(input_path)
            except (AudioDecodeError, DependencyUnavailableError):
                raise
            except Exception as exc:
                raise AudioDecodeError(f"Cannot decode source audio: {exc}") from exc
            try:
                sample_count = len(audio)
                duration_seconds = sample_count / 8000.0
                if duration_seconds > MAX_AUDIO_DURATION_SECONDS:
                    raise AudioDecodeError(
                        f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                    )
                phrases = self._pipeline.forward_offline(audio)  # type: ignore[attr-defined]
            except AudioDecodeError:
                raise
            except Exception as exc:
                raise InferenceError(f"T-one inference failed: {exc}") from exc

        segments = tuple(
            Segment(
                start=float(phrase.start_time),
                end=float(phrase.end_time),
                text=str(phrase.text).strip(),
            )
            for phrase in phrases
            if str(phrase.text).strip()
        )
        return EngineResult(duration_seconds=duration_seconds, segments=segments)


class WhisperCppEngine:
    """Offline whisper.cpp adapter for the pinned multilingual large-v3 model."""

    MODEL_NAME = "Whisper large-v3"
    MODEL_FILENAME = "ggml-large-v3.bin"
    SUPPORTED_DECODERS = {"greedy", "beam_search"}

    def __init__(
        self,
        model_dir: Path,
        *,
        cli_path: Path,
        vad_model_path: Path,
        scratch_dir: Path,
        decoder: str = "beam_search",
        initial_prompt: str | None = None,
        verify_model_hashes: bool = False,
    ) -> None:
        if decoder not in self.SUPPORTED_DECODERS:
            raise ModelValidationError(f"Unsupported decoder: {decoder!r}")
        self._model_dir = model_dir.resolve(strict=False)
        self._cli_path = cli_path.resolve(strict=False)
        if self._cli_path.is_symlink() or not self._cli_path.is_file():
            raise DependencyUnavailableError(
                f"Local whisper.cpp CLI is missing: {self._cli_path}. "
                "Run scripts/prepare_whisper_cpp.py in the staging environment."
            )
        self._vad_model_path = vad_model_path.resolve(strict=False)
        self._vad_manifest = ModelManifest.load(
            self._vad_model_path.parent,
            expected_name="Silero VAD",
        )
        self._vad_model_path = self._vad_manifest.validate_artifact(
            self._vad_model_path.parent,
            self._vad_model_path.name,
            verify_hash=True,
        )
        self._scratch_dir = scratch_dir.resolve(strict=False)
        if self._scratch_dir.exists() and (self._scratch_dir.is_symlink() or not self._scratch_dir.is_dir()):
            raise ModelValidationError(f"Whisper scratch path must be a regular directory: {self._scratch_dir}")
        self._scratch_dir.mkdir(parents=True, exist_ok=True)
        self._decoder_name = decoder
        self._initial_prompt = " ".join(initial_prompt.split()) if initial_prompt else None
        self._manifest = ModelManifest.load(self._model_dir, expected_name=self.MODEL_NAME)
        self._model_path = self._manifest.validate_artifact(
            self._model_dir,
            self.MODEL_FILENAME,
            verify_hash=verify_model_hashes,
        )

    @property
    def model_info(self) -> EngineModelInfo:
        return EngineModelInfo(
            name=self._manifest.name,
            version=self._manifest.version,
            source_revision=self._manifest.source_revision,
            source_code_revision=self._manifest.source_code_revision,
            decoder=self._decoder_name,
            local_path=self._model_dir.name,
            vad_name=self._vad_manifest.name,
            vad_version=self._vad_manifest.version,
            vad_source_revision=self._vad_manifest.source_revision,
            vad_sha256=self._vad_manifest.artifacts[self._vad_model_path.name].sha256,
            vad_threshold=0.5,
        )

    def transcribe(self, input_path: Path) -> EngineResult:
        beam_size = "5" if self._decoder_name == "beam_search" else "1"
        with tempfile.TemporaryDirectory(dir=self._scratch_dir, prefix="whisper-") as temporary_dir:
            whisper_input, duration_seconds = _prepare_whisper_input(
                input_path,
                Path(temporary_dir),
            )
            if duration_seconds > MAX_AUDIO_DURATION_SECONDS:
                raise AudioDecodeError(
                    f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                )
            output_prefix = Path(temporary_dir) / "result"
            command = [
                str(self._cli_path),
                "--model",
                str(self._model_path),
                "--file",
                str(whisper_input),
                "--language",
                "ru",
                "--vad",
                "--vad-model",
                str(self._vad_model_path),
                "--vad-threshold",
                "0.50",
                "--threads",
                str(min(8, os.cpu_count() or 4)),
                "--beam-size",
                beam_size,
                "--best-of",
                beam_size,
                "--flash-attn",
                "--output-json",
                "--output-file",
                str(output_prefix),
                "--no-prints",
            ]
            if self._initial_prompt:
                command.extend(("--prompt", self._initial_prompt))
            response_file = Path(temporary_dir) / "arguments.rsp"
            response_file.write_text("\n".join(command[1:]) + "\n", encoding="utf-8")
            with deny_python_network():
                try:
                    completed = subprocess.run(
                        [command[0], "@arguments.rsp"],
                        cwd=temporary_dir,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                        timeout=max(300.0, duration_seconds * 5.0),
                    )
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError(
                        "Local whisper.cpp inference exceeded its duration-based timeout"
                    ) from exc
                except OSError as exc:
                    raise DependencyUnavailableError(f"Cannot start local whisper.cpp CLI: {exc}") from exc
            if completed.returncode != 0:
                raise InferenceError(
                    f"whisper.cpp inference failed with exit code {completed.returncode}; "
                    "technical output was suppressed to avoid logging transcript data"
                )
            result_path = Path(f"{output_prefix}.json")
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise InferenceError(f"Cannot read whisper.cpp JSON result: {type(exc).__name__}") from exc
        return EngineResult(
            duration_seconds=duration_seconds,
            segments=_parse_whisper_segments(payload),
        )


def create_engine(
    engine_name: str,
    model_dir: Path,
    *,
    decoder: str,
    scratch_dir: Path,
    whisper_cli_path: Path | None = None,
    vad_model_path: Path | None = None,
    initial_prompt: str | None = None,
    verify_model_hashes: bool = False,
) -> AsrEngine:
    if engine_name == "t-one":
        return ToneEngine(
            model_dir,
            decoder=decoder,
            verify_model_hashes=verify_model_hashes,
        )
    if engine_name == "whisper":
        if whisper_cli_path is None:
            raise ModelValidationError("whisper_cli_path is required for the whisper engine")
        if vad_model_path is None:
            raise ModelValidationError("vad_model_path is required for the whisper engine")
        return WhisperCppEngine(
            model_dir,
            cli_path=whisper_cli_path,
            vad_model_path=vad_model_path,
            scratch_dir=scratch_dir,
            decoder=decoder,
            initial_prompt=initial_prompt,
            verify_model_hashes=verify_model_hashes,
        )
    raise ModelValidationError(f"Unsupported ASR engine: {engine_name!r}")


def _audio_duration(input_path: Path) -> float:
    try:
        import miniaudio
    except ImportError as exc:
        raise DependencyUnavailableError(
            "miniaudio==1.61 is required to inspect local audio without FFmpeg"
        ) from exc
    try:
        duration = float(miniaudio.get_file_info(str(input_path)).duration)
    except Exception as exc:
        raise AudioDecodeError(f"Cannot inspect source audio: {exc}") from exc
    if duration <= 0:
        raise AudioDecodeError("Decoded audio duration must be greater than zero")
    return duration


def _prepare_whisper_input(input_path: Path, temporary_dir: Path) -> tuple[Path, float]:
    resolved_input = input_path.resolve(strict=True)
    if input_path.suffix.lower() != ".aac":
        return resolved_input, _audio_duration(resolved_input)
    converted_path = temporary_dir / "input.wav"
    duration_seconds = _transcode_aac_to_wav(resolved_input, converted_path)
    return converted_path, duration_seconds


def _transcode_aac_to_wav(input_path: Path, output_path: Path) -> float:
    """Decode local AAC ADTS to the 16 kHz mono PCM expected by whisper.cpp."""

    try:
        import av
    except ImportError as exc:
        raise DependencyUnavailableError(
            "AAC decoding requires the pinned local PyAV wheel. "
            "Install requirements/aac.txt during staging; runtime will not download it."
        ) from exc

    sample_rate = 16000
    max_samples = MAX_AUDIO_DURATION_SECONDS * sample_rate
    sample_count = 0
    skipped_packets = 0
    try:
        with av.open(str(input_path), mode="r", format="aac") as container:
            if not container.streams.audio:
                raise AudioDecodeError("AAC source contains no audio stream")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
            with wave.open(str(output_path), "wb") as destination:
                destination.setnchannels(1)
                destination.setsampwidth(2)
                destination.setframerate(sample_rate)
                for packet in container.demux(stream):
                    try:
                        frames = packet.decode()
                    except av.error.InvalidDataError:
                        skipped_packets += 1
                        continue
                    for frame in frames:
                        for converted in resampler.resample(frame):
                            if sample_count + converted.samples > max_samples:
                                raise AudioDecodeError(
                                    f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                                )
                            frame_bytes = bytes(converted.planes[0])[: converted.samples * 2]
                            destination.writeframesraw(frame_bytes)
                            sample_count += converted.samples
                for converted in resampler.resample(None):
                    if sample_count + converted.samples > max_samples:
                        raise AudioDecodeError(
                            f"Audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS // 3600}-hour limit"
                        )
                    frame_bytes = bytes(converted.planes[0])[: converted.samples * 2]
                    destination.writeframesraw(frame_bytes)
                    sample_count += converted.samples
    except AudioDecodeError:
        output_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        raise AudioDecodeError(f"Cannot decode local AAC source: {exc}") from exc
    if sample_count <= 0:
        output_path.unlink(missing_ok=True)
        raise AudioDecodeError("AAC source contains no decodable audio frames")
    if skipped_packets:
        LOGGER.warning(
            "aac_packets_skipped file=%s packet_count=%d",
            input_path.name,
            skipped_packets,
        )
    return sample_count / sample_rate


def _parse_whisper_segments(payload: Any) -> tuple[Segment, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list):
        raise InferenceError("whisper.cpp JSON result has no transcription array")
    parsed: list[Segment] = []
    for item in payload["transcription"]:
        if not isinstance(item, dict) or not isinstance(item.get("offsets"), dict):
            raise InferenceError("whisper.cpp returned a malformed segment")
        text = item.get("text")
        start_ms = item["offsets"].get("from")
        end_ms = item["offsets"].get("to")
        if not isinstance(text, str) or not isinstance(start_ms, int) or not isinstance(end_ms, int):
            raise InferenceError("whisper.cpp returned invalid segment fields")
        if start_ms < 0 or end_ms < start_ms:
            raise InferenceError("whisper.cpp returned invalid segment timestamps")
        if text.strip():
            parsed.append(Segment(start=start_ms / 1000.0, end=end_ms / 1000.0, text=text.strip()))
    return tuple(parsed)
