"""Local, offline-first call transcription wrapper."""

from .service import TranscriptionRequest, transcribe_file

__all__ = ["TranscriptionRequest", "transcribe_file"]
__version__ = "0.2.0"
