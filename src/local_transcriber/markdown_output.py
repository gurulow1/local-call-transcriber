"""Human-readable Markdown rendering for one completed transcription."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote


def render_transcript_markdown(result: Mapping[str, Any]) -> str:
    """Render a compact report without changing or reinterpreting transcript text."""

    if result.get("status") != "completed":
        raise ValueError("Markdown can only be rendered for a completed transcription")

    call_id = str(result.get("call_id", ""))
    source_audio = str(result.get("source_audio", ""))
    duration = _format_duration(result.get("duration_seconds"))
    model = result.get("model") if isinstance(result.get("model"), dict) else {}
    model_name = _table_value(model.get("name", ""))
    model_version = _table_value(model.get("version", ""))
    decoder = _table_value(model.get("decoder", ""))
    text = str(result.get("text", "")).strip()
    segments = result.get("segments") if isinstance(result.get("segments"), list) else []

    lines = [
        f"# Расшифровка: {call_id}",
        "",
        f"[Открыть исходное аудио](./{quote(source_audio)})",
        "",
        "| Сведения | Значение |",
        "|---|---|",
        f"| Аудио | `{_inline_code(source_audio)}` |",
        f"| Длительность | {duration} |",
        f"| Модель | {model_name} {model_version} |".rstrip(),
        f"| Декодер | {decoder} |",
        "",
        "## Текст",
        "",
        text or "_Речь не распознана._",
    ]

    if segments:
        lines.extend(["", "## Фрагменты"])
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            start = _format_timestamp(segment.get("start"))
            end = _format_timestamp(segment.get("end"))
            segment_text = str(segment.get("text", "")).strip()
            if not segment_text:
                continue
            lines.extend(["", f"**{start} — {end}**", "", segment_text])

    lines.extend(
        [
            "",
            "---",
            "",
            "_Автоматическая расшифровка. Суммы, даты, имена и другие критичные данные нужно проверять по аудио._",
            "",
        ]
    )
    return "\n".join(lines)


def _format_duration(value: object) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "неизвестно"
    return f"{_format_timestamp(seconds)} ({seconds:.1f} с)"


def _format_timestamp(value: object) -> str:
    try:
        total_seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _table_value(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _inline_code(value: str) -> str:
    return value.replace("`", "'")
