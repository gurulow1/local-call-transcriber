# Пакет передачи команде CRM

## Что передаётся

Транскрибатор — автономный локальный модуль. CRM передаёт ему аудио, опрашивает статус и забирает версионированный JSON. Визуальный интерфейс остаётся в CRM.

```text
CRM -> PUT audio -> staging -> SQLite queue -> local ASR -> JSON -> CRM
```

На Windows с NVIDIA one-click запуск выбирает Whisper large-v3/CUDA; без NVIDIA, на Linux и macOS — T-one/CPU. HTTP-контракт и формат результата от выбранного движка не зависят.

Статический контракт: [`openapi-v1.json`](openapi-v1.json). JSON Schema: [`../schemas/`](../schemas/). Искусственные примеры: [`../examples/crm/`](../examples/crm/).

## Reference-запуск

One-click файл платформы уже запускает ASR-worker. Для ручного reference-запуска после автоматической установки:

```powershell
# Windows + NVIDIA
.poetry-cache\venv\Scripts\python.exe worker.py --mode poll --engine whisper --decoder beam_search

# Windows без NVIDIA
.poetry-cache\venv\Scripts\python.exe worker.py --mode poll --engine t-one --decoder greedy
```

```bash
# Linux/macOS
.poetry-cache/venv/bin/python worker.py --mode poll --engine t-one --decoder greedy
```

Второй процесс принимает локальные HTTP-запросы:

```powershell
.poetry-cache\venv\Scripts\python.exe crm_api.py --port 8765
```

```bash
.poetry-cache/venv/bin/python crm_api.py --port 8765
```

Сервер намеренно слушает только `127.0.0.1` и принимает `Host` лишь для `127.0.0.1`/`localhost` с фактическим портом. У него нет аутентификации и TLS, поэтому его нельзя публиковать в LAN/интернет. Для production команда CRM выбирает нативный адаптер либо размещает эталон за корпоративным mTLS/service-auth proxy.

## Поток обмена

### 1. Загрузить аудио

```bash
curl --fail-with-body \
  --request PUT \
  --header 'Content-Type: audio/aac' \
  --data-binary '@demo-001.aac' \
  http://127.0.0.1:8765/v1/jobs/demo-001/audio.aac
```

Тело — сырые байты аудио, не JSON/base64. `call_id` в URL и расширение определяют каноническое имя `demo-001.aac`. Ответ `202 Accepted` содержит SHA-256 принятых байтов. Совпадающий `call_id` не перезаписывается и даёт `409 Conflict`.

### 2. Проверить статус

```bash
curl --fail-with-body http://127.0.0.1:8765/v1/jobs/demo-001
```

Статусы: `new`, `queued`, `processing`, `completed`, `failed`. Ответ не содержит аудио, текст звонка и локальные пути.

### 3. Забрать результат

```bash
curl --fail-with-body http://127.0.0.1:8765/v1/jobs/demo-001/result
```

До готовности ответ — `409 result_not_ready`. После `completed` возвращается полный JSON `1.2`.

## Имитатор CRM

Для проверки без `curl` есть stdlib-клиент. Он намеренно подключается только к loopback:

```bash
.poetry-cache/venv/bin/python scripts/crm_client.py upload --audio demo-001.aac
.poetry-cache/venv/bin/python scripts/crm_client.py status --call-id demo-001
.poetry-cache/venv/bin/python scripts/crm_client.py result --call-id demo-001
```

На Windows в этих трёх командах используется `.poetry-cache\venv\Scripts\python.exe`.

В dev/test для этих команд используются только искусственные или разрешённые тестовые данные.

## HTTP-ошибки

| Код | Значение |
|---|---|
| `400` | неверный `call_id`, длина или неполное тело |
| `404` | endpoint или задача не найдены |
| `409` | `call_id` уже есть или результат ещё не готов |
| `411` | нет `Content-Length` |
| `413` | превышен настраиваемый локальный лимит загрузки; по умолчанию 1 ГиБ |
| `415` | формат или `Content-Type` не поддержан |

## Что должна решить CRM-команда

1. Файлый обмен, HTTP или корпоративная очередь.
2. Polling статуса или callback/webhook о готовности.
3. Service identity, аутентификация, TLS, ACL, firewall и audit.
4. Где живут аудио и результаты, срок хранения и удаления.
5. Формат аудио, mono/stereo и есть ли отдельные каналы менеджера/клиента.
6. Политика повторной транскрибации и версий модели.

## Известные границы

- `speaker` формируется только для двухканального Whisper-аудио и обозначает канал, а не личность или роль; mono-диаризация пока не реализована;
- callback/webhook не реализован;
- `/healthz` подтверждает только работу HTTP-адаптера, а не готовность модели/воркера;
- технический guard ограничивает декодированное аудио четырьмя часами; бизнес-лимит звонка ещё должен утвердить владелец продукта;
- reference API принимает не более 1 ГиБ на upload по умолчанию и не заменяет perimeter/auth/rate-limit слой;
- reference API не заменяет production ingress;
- quality на естественных корпоративных звонках ещё не измерено;
- в dev/test нельзя использовать реальные корпоративные звонки.
