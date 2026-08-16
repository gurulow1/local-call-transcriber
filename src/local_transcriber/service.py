"""Contract validation and atomic JSON publication for one audio file."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import AsrEngine, create_engine
from .errors import InputValidationError, OutputValidationError
from .markdown_output import render_transcript_markdown
from .postprocessing import postprocess_segments

LOGGER = logging.getLogger("local_transcriber.service")

SCHEMA_VERSION = "1.2"
CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SUPPORTED_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".ogg", ".aac"})


@dataclass(frozen=True)
class TranscriptionRequest:
    """Validated intent for one manual transcription."""

    input_path: Path
    output_path: Path
    model_dir: Path
    decoder: str = "beam_search"
    engine_name: str = "whisper"
    whisper_cli_path: Path | None = None
    vad_model_path: Path | None = None
    initial_prompt: str | None = None
    txt_output_path: Path | None = None
    markdown_output_path: Path | None = None
    overwrite: bool = False
    verify_model_hashes: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def extract_call_id(input_path: Path) -> str:
    call_id = input_path.stem
    if not CALL_ID_PATTERN.fullmatch(call_id):
        raise InputValidationError(
            "call_id from filename must be 1-128 ASCII characters: letters, digits, '_' or '-', "
            "and must start with a letter or digit"
        )
    return call_id


def validate_request(request: TranscriptionRequest) -> str:
    input_path = request.input_path
    output_path = request.output_path
    if input_path.is_symlink() or not input_path.is_file():
        raise InputValidationError("Input must be an existing regular file, not a symlink")
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise InputValidationError(f"Unsupported audio extension; expected one of: {supported}")
    call_id = extract_call_id(input_path)
    if output_path.suffix.lower() != ".json":
        raise OutputValidationError("Output filename must end with .json")
    if output_path.stem != call_id:
        raise OutputValidationError("Output filename must use the same call_id as the input")
    if input_path.resolve(strict=True) == output_path.resolve(strict=False):
        raise OutputValidationError("Input and output paths must be different")
    if output_path.exists() and not request.overwrite:
        raise OutputValidationError("Output already exists; use --overwrite for an explicit reprocessing")
    if output_path.exists() and (output_path.is_symlink() or not output_path.is_file()):
        raise OutputValidationError("Existing output must be a regular file, not a symlink")
    if request.txt_output_path is not None:
        txt_path = request.txt_output_path
        if txt_path.suffix.lower() != ".txt" or txt_path.stem != call_id:
            raise OutputValidationError("TXT output must use the same call_id and end with .txt")
        if txt_path.exists() and not request.overwrite:
            raise OutputValidationError("TXT output already exists; use --overwrite")
        if txt_path.exists() and (txt_path.is_symlink() or not txt_path.is_file()):
            raise OutputValidationError("Existing TXT output must be a regular file, not a symlink")
    if request.markdown_output_path is not None:
        markdown_path = request.markdown_output_path
        if markdown_path.suffix.lower() != ".md" or markdown_path.stem != call_id:
            raise OutputValidationError("Markdown output must use the same call_id and end with .md")
        if markdown_path.exists() and not request.overwrite:
            raise OutputValidationError("Markdown output already exists; use --overwrite")
        if markdown_path.exists() and (markdown_path.is_symlink() or not markdown_path.is_file()):
            raise OutputValidationError("Existing Markdown output must be a regular file, not a symlink")
    return call_id


def transcribe_file(request: TranscriptionRequest, *, engine: AsrEngine | None = None) -> dict[str, Any]:
    """Process one file and publish a complete or failed JSON envelope."""

    created_at = utc_now()
    started = time.perf_counter()
    candidate_call_id = request.input_path.stem[:128]
    call_id = candidate_call_id if CALL_ID_PATTERN.fullmatch(candidate_call_id) else "unknown"

    try:
        call_id = validate_request(request)
        selected_engine = engine or _create_engine(request)
        engine_result = selected_engine.transcribe(request.input_path)
        processing_seconds = time.perf_counter() - started
        duration_seconds = float(engine_result.duration_seconds)
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise InputValidationError("Decoded audio duration must be greater than zero")
        asr_segments = _validated_segments(engine_result.segments, duration_seconds)
        postprocessed = postprocess_segments(asr_segments)
        model = asdict(selected_engine.model_info)
        published_duration = round(duration_seconds, 3)
        if published_duration <= 0:
            raise InputValidationError("Decoded audio duration is below the 1 ms contract precision")
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "call_id": call_id,
            "status": "completed",
            "source_audio": request.input_path.name,
            "language": "ru",
            "duration_seconds": published_duration,
            "processing_seconds": round(processing_seconds, 3),
            "real_time_factor": round(processing_seconds / duration_seconds, 4),
            "model": model,
            "text": postprocessed.text,
            "raw_text": postprocessed.raw_text,
            "segments": postprocessed.segments,
            "postprocessing": postprocessed.metadata,
            "created_at": created_at,
            "completed_at": utc_now(),
            "error": None,
        }
        if request.txt_output_path is not None:
            _atomic_write_text(
                request.txt_output_path,
                postprocessed.text + ("\n" if postprocessed.text else ""),
            )
        if request.markdown_output_path is not None:
            _atomic_write_text(
                request.markdown_output_path,
                render_transcript_markdown(
                    result,
                    audio_href=_markdown_audio_href(
                        request.input_path,
                        request.markdown_output_path,
                    ),
                ),
            )
        _atomic_write_json(request.output_path, result)
        return result
    except Exception as exc:
        processing_seconds = time.perf_counter() - started
        result = {
            "schema_version": SCHEMA_VERSION,
            "call_id": call_id,
            "status": "failed",
            "source_audio": request.input_path.name,
            "language": "ru",
            "duration_seconds": 0.0,
            "processing_seconds": round(processing_seconds, 3),
            "real_time_factor": 0.0,
            "model": None,
            "text": "",
            "raw_text": "",
            "segments": [],
            "postprocessing": None,
            "created_at": created_at,
            "completed_at": utc_now(),
            "error": {
                "type": type(exc).__name__,
                "message": _safe_error_message(str(exc)),
            },
        }
        _write_failure_if_safe(request, result)
        return result


def _create_engine(request: TranscriptionRequest) -> AsrEngine:
    return create_engine(
        request.engine_name,
        request.model_dir,
        decoder=request.decoder,
        scratch_dir=request.output_path.parent / ".tmp",
        whisper_cli_path=request.whisper_cli_path,
        vad_model_path=request.vad_model_path,
        initial_prompt=request.initial_prompt,
        verify_model_hashes=request.verify_model_hashes,
    )


def _write_failure_if_safe(request: TranscriptionRequest, result: dict[str, Any]) -> None:
    output = request.output_path
    if output.suffix.lower() != ".json":
        return
    call_id = result.get("call_id")
    if (
        not isinstance(call_id, str)
        or not CALL_ID_PATTERN.fullmatch(call_id)
        or call_id == "unknown"
        or request.input_path.stem != call_id
        or request.input_path.suffix.lower() not in SUPPORTED_EXTENSIONS
        or output.stem != call_id
    ):
        # The returned in-memory error remains available to the CLI, but no
        # non-canonical or schema-invalid result is published on disk.
        return
    if output.exists():
        # A failed explicit reprocessing must not destroy an older successful or
        # unrecognized result. A previous failed envelope may be refreshed.
        if not request.overwrite or not _is_matching_failed_output(output, result["call_id"]):
            return
    try:
        _atomic_write_json(output, result)
    except OSError:
        return


def _is_matching_failed_output(path: Path, call_id: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload.get("call_id") == call_id and payload.get("status") == "failed"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        allow_nan=False,
    ) + "\n"
    _atomic_write_text(path, serialized)


def _validated_segments(segments: Any, duration_seconds: float) -> list[dict[str, Any]]:
    """Validate engine-neutral timestamps and clip only a harmless end overhang."""

    validated: list[dict[str, Any]] = []
    previous_start = 0.0
    for segment in segments:
        start = float(segment.start)
        end = float(segment.end)
        if not math.isfinite(start) or not math.isfinite(end):
            raise OutputValidationError("ASR returned a non-finite segment timestamp")
        if start < 0 or end < start:
            raise OutputValidationError("ASR returned an invalid segment timestamp range")
        if start < previous_start:
            raise OutputValidationError("ASR returned non-monotonic segment timestamps")
        if start > duration_seconds:
            raise OutputValidationError("ASR segment starts after the source audio ends")
        if end > duration_seconds:
            LOGGER.warning(
                "segment_end_clipped start=%.3f end=%.3f duration=%.3f",
                start,
                end,
                duration_seconds,
            )
            end = duration_seconds
        speaker = getattr(segment, "speaker", None)
        if speaker not in {None, "SPEAKER_00", "SPEAKER_01"}:
            raise OutputValidationError("ASR returned an invalid speaker label")
        validated_segment = {
            "start": round(start, 3),
            "end": round(end, 3),
            "text": str(segment.text),
        }
        if speaker is not None:
            validated_segment["speaker"] = speaker
        validated.append(validated_segment)
        previous_start = start
    return validated


def _safe_error_message(message: str, limit: int = 500) -> str:
    return " ".join(message.split())[:limit] or "Transcription failed"


def _markdown_audio_href(input_path: Path, markdown_path: Path) -> str:
    try:
        relative = os.path.relpath(input_path.resolve(strict=True), markdown_path.parent.resolve())
    except ValueError:
        return input_path.resolve(strict=True).as_uri()
    return relative.replace(os.sep, "/")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as destination:
            destination.write(text)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
