"""Strict local adapter for the audited T-one inference API."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .errors import (
    AudioDecodeError,
    DependencyUnavailableError,
    InferenceError,
    ModelValidationError,
)
from .model_manifest import ModelManifest
from .network import deny_python_network


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
        self._manifest = ModelManifest.load(self._model_dir)
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
            local_path=str(self._model_dir),
        )

    def _load(self) -> None:
        if self._pipeline is not None:
            return
        with deny_python_network():
            try:
                if self._decoder_name == "greedy":
                    logging.getLogger("pyctcdecode").setLevel(logging.ERROR)
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
            self._read_audio = read_audio

    def transcribe(self, input_path: Path) -> EngineResult:
        self._load()
        assert self._pipeline is not None
        assert callable(self._read_audio)
        with deny_python_network():
            try:
                audio = self._read_audio(input_path)
            except Exception as exc:
                raise AudioDecodeError(f"Cannot decode source audio: {exc}") from exc
            try:
                sample_count = len(audio)
                duration_seconds = sample_count / 8000.0
                phrases = self._pipeline.forward_offline(audio)  # type: ignore[attr-defined]
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
