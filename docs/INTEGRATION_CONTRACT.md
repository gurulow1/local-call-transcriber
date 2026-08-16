# Контракт интеграции

CRM в этом проекте не реализуется. Документ задаёт границу обмена для её команды. Исполняемый reference HTTP-адаптер не заменяет production ingress и доступен только на `127.0.0.1`.

## Файловый вариант

```text
/calls/{call_id}/{call_id}.wav
/calls/{call_id}/{call_id}.json
/calls/{call_id}/{call_id}.md
```

Аудио переносится в каноническую папку только после проверки стабильности и не перекодируется. JSON и Markdown публикуются атомарно: запись временного файла, `fsync`, затем rename. Технический потребитель читает JSON; человек — Markdown.

## API-вариант

```http
PUT /v1/jobs/{call_id}/audio.{extension}
GET /v1/jobs/{call_id}
GET /v1/jobs/{call_id}/result
```

Загрузка передаёт сырые байты аудио с `Content-Length`, без base64 и multipart. Ответ `202` возвращает количество байтов, SHA-256, URL статуса и URL результата. Загрузка публикуется в staging только после полной записи и `fsync`.

Минимальный ответ статуса:

```json
{
  "api_version": "1.0",
  "call_id": "1234",
  "status": "processing",
  "attempts": 1,
  "max_attempts": 3,
  "created_at": "2026-08-10T09:00:00.000Z",
  "updated_at": "2026-08-10T09:00:06.000Z",
  "completed_at": null,
  "result_url": null,
  "error": null
}
```

Полный OpenAPI — [`openapi-v1.json`](openapi-v1.json), процедура передачи — [`CRM_HANDOFF.md`](CRM_HANDOFF.md). Сеть, аутентификация, авторизация, TLS и callback для production определяются вместе с CRM-командой.

## Правила

- кодировка JSON и текста: UTF-8;
- `call_id` — строка, не число; ведущие нули значимы;
- допустимый алфавит имени на MVP: ASCII буквы, цифры, `_` и `-`; окончательное правило согласуется с CRM;
- имя output равно безопасному `call_id` плюс `.json`;
- имя Markdown равно `call_id` плюс `.md`, внутри есть относительная ссылка на аудио;
- один `call_id` соответствует одной папке и одному аудио; совпадение не перезаписывает архив;
- статусы: `new`, `queued`, `processing`, `completed`, `failed`;
- `completed` содержит текст и может содержать сегменты;
- `text` — детерминированно очищенная версия, `raw_text` — неизменная склейка выдачи ASR;
- каждый сегмент содержит очищенный `text` и исходный `asr_text`; таймкоды при постобработке не меняются;
- для двухканальной записи Whisper-сегмент дополнительно содержит `speaker`: левый канал — `SPEAKER_00`, правый — `SPEAKER_01`; это метки каналов, а не идентификация личности или роли;
- `postprocessing` фиксирует метод, версию словаря и число применённых замен; текущий pipeline пишет `deterministic_glossary_v2`, схема продолжает читать legacy `v1`;
- `failed` содержит `error.type` и безопасное `error.message`, без stack trace и текста звонка;
- таймкоды — секунды от начала аудио, числа `>= 0`, конец не меньше начала;
- `schema_version` изменяется по правилам semver контракта: несовместимое изменение — новая major;
- `model.name`, `model.version` и логический идентификатор bundle в `model.local_path` обязательны для воспроизводимости; абсолютный host path наружу не публикуется;
- Whisper-результат фиксирует VAD name/version/source revision/SHA-256/threshold; для T-one эти поля равны `null`;
- повторный запрос/чтение успешно завершённой версии идемпотентны;
- повторная загрузка занятого `call_id` не перезаписывает аудио и возвращает `409`;
- статус endpoint не раскрывает локальные пути, текст звонка или аудио;
- повторная обработка выполняется только явной административной операцией и создаёт новую ревизию либо атомарно заменяет результат по заранее согласованной политике;
- возможность обновления транскрипции требует полей `transcript_revision`/`supersedes` в будущей minor-версии; политика пока открыта.

## Полная схема результата MVP

```json
{
  "schema_version": "1.2",
  "call_id": "1234",
  "status": "completed",
  "source_audio": "1234.wav",
  "language": "ru",
  "duration_seconds": 4.2,
  "processing_seconds": 2.1,
  "real_time_factor": 0.5,
  "model": {
    "name": "Whisper large-v3",
    "version": "large-v3+ggml-f16",
    "source_revision": "362722b3fdcd2300b58a8286933ead1c48619667",
    "source_code_revision": "306c88f4d1286aec1bf96e544632897886af5501",
    "decoder": "beam_search",
    "local_path": "whisper-large-v3",
    "vad_name": "Silero VAD",
    "vad_version": "v5.1.2",
    "vad_source_revision": "e5614ed76a5dd4b03fad5068c89efcd2617a9d1e",
    "vad_sha256": "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf",
    "vad_threshold": 0.5
  },
  "text": "Очищенная фраза.",
  "raw_text": "очищенная фраза",
  "segments": [
    {
      "start": 0.0,
      "end": 1.0,
      "text": "Очищенная фраза.",
      "asr_text": "очищенная фраза",
      "speaker": "SPEAKER_00"
    }
  ],
  "postprocessing": {
    "method": "deterministic_glossary_v2",
    "glossary_version": "2",
    "term_replacements": 0,
    "phrase_replacements": 0
  },
  "created_at": "2026-08-13T12:00:00.000Z",
  "completed_at": "2026-08-13T12:00:02.100Z",
  "error": null
}
```
