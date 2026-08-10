"""Stable application errors that are safe to serialize."""

from __future__ import annotations


class TranscriberError(Exception):
    """Base class for expected transcription failures."""


class InputValidationError(TranscriberError):
    """The requested input or identifier violates the contract."""


class OutputValidationError(TranscriberError):
    """The output target violates the contract."""


class ModelValidationError(TranscriberError):
    """Local model artifacts are absent, unexpected, or corrupted."""


class DependencyUnavailableError(TranscriberError):
    """A pinned runtime dependency is unavailable."""


class AudioDecodeError(TranscriberError):
    """The input cannot be decoded by the selected local decoder."""


class InferenceError(TranscriberError):
    """The local ASR engine failed to produce a result."""


class PostprocessingConfigError(TranscriberError):
    """The local deterministic postprocessing configuration is invalid."""
