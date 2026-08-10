# Локальный транскрибатор звонков

Минимальная цель проекта:

```text
data/input/1234.wav
  -> data/calls/1234/
       1234.wav
       1234.json
       1234.md
```

Основной кандидат — T-one, резервный — whisper.cpp. Статический аудит, одиночный greedy-прототип и SQLite-worker реализованы. Решение ещё не готово к production: beam search, расширенный quality-набор, SCA/CVE, целевая Linux-среда и нагрузочные тесты не завершены.

Greedy runtime также фактически проверен на Windows 10 / CPython 3.11: одиночный CLI и SQLite-worker работают с локальным `model.onnx`; установка использует Windows wheelhouse с SHA-256 из `requirements/windows-greedy.txt`.

## Принципы

- никакого внешнего API;
- никакой автоматической загрузки модели при обработке;
- только локальные веса из `models/`;
- реальные корпоративные звонки не используются для разработки и тестов;
- исходное аудио остаётся неизменным;
- сначала проверяется один искусственный звонок, затем очередь и фоновые режимы.

Входные форматы: WAV, MP3, FLAC, OGG и AAC ADTS. AAC декодируется локальным PyAV из зафиксированного wheel; в runtime ничего не скачивается.

После ASR выполняется локальная детерминированная постобработка: границы ASR-сегментов получают заглавную букву и финальный знак, а ошибочные написания терминов исправляются по редактируемому [`config/glossary.json`](config/glossary.json). Исходная выдача ASR всегда сохраняется в `raw_text` и `segments[].asr_text`; `text` содержит очищенную версию. LLM в этом пути не используется.

## Структура

- `docs/` — требования, архитектура, аудит и журнал решений;
- `models/` — локальные веса (не должны попадать в Git);
- `data/input/` — папка для нового аудио;
- `data/calls/{call_id}/` — готовая папка звонка с аудио, JSON и Markdown;
- `data/failed/` — артефакты и маркеры неуспешной обработки;
- `src/` — код прототипа;
- `config/glossary.json` — локальный словарь канонических терминов и точечных коррекций;
- `tests/` — автоматические тесты;
- `schemas/` и `examples/crm/` — версионированный контракт для CRM-команды;
- `logs/` — технические журналы без текста звонков.

Статус и порядок работ ведутся в [docs/TASKS.md](docs/TASKS.md).

## Перенос на другой компьютер

Приватный GitHub-репозиторий хранит только код, документацию, тесты и manifests:

```bash
git clone https://github.com/gurulow1/local-call-transcriber.git
cd local-call-transcriber
```

Веса, аудио, транскрипции, логи, `.venv`, wheelhouse и checkout T-one намеренно исключены. На Mac после клонирования заново выполнить staging из [docs/INSTALLATION.md](docs/INSTALLATION.md), проверить `models/t-one` и только затем запускать greedy. Приватный репозиторий не является хранилищем корпоративных данных.

## Проверенный одиночный запуск

После контролируемой установки и подготовки `models/t-one`:

```bash
.venv/bin/python transcribe.py \
  --input data/calls/1001/1001.wav \
  --output data/calls/1001/1001.json \
  --markdown-output data/calls/1001/1001.md \
  --decoder greedy
```

На проверенной Windows-среде эквивалентная команда:

```powershell
.venv\Scripts\python.exe transcribe.py `
  --input data\calls\1001\1001.wav `
  --output data\calls\1001\1001.json `
  --markdown-output data\calls\1001\1001.md `
  --decoder greedy
```

Перед обработкой вне изолированной тестовой среды администратор должен включить запрет исходящей сети через `security/windows-deny-network.ps1`.

Beam search является целевым decoder-ом, но требует локальный `kenlm.bin` размером 5 463 477 004 байта. На текущей маломощной машине фактически проверен только greedy.

## Фоновая обработка

Один проход по каталогу с устойчивой SQLite-очередью:

```bash
.venv/bin/python worker.py --mode once --decoder greedy
```

Постоянная проверка каталога:

```bash
.venv/bin/python worker.py --mode poll --decoder greedy
```

Основной сценарий:

1. Положить `call-001.aac` в `data/input`.
2. Подождать несколько секунд, пока файл перестанет изменяться.
3. Открыть `data/calls/call-001`: внутри будут исходное аудио, технический JSON и удобный для чтения Markdown.

Аудио переносится без перекодирования. Существующий `call_id` не перезаписывается.

`--mode watch` сейчас использует переносимый polling, а `--mode batch` предназначен для запуска внешним ночным scheduler-ом. Неудачную задачу можно повторить только явно: `--requeue CALL_ID`.

Полная проверенная процедура описана в [docs/INSTALLATION.md](docs/INSTALLATION.md). Аудит и ограничения — в [docs/REPOSITORY_AUDIT.md](docs/REPOSITORY_AUDIT.md).

## Reference API для CRM-команды

Отдельный локальный процесс принимает raw audio, показывает статус очереди и отдаёт готовый JSON:

```bash
.venv/bin/python crm_api.py --port 8765
```

Он всегда слушает только `127.0.0.1` и не имеет production-аутентификации/TLS. Полная передача для технарей: [docs/CRM_HANDOFF.md](docs/CRM_HANDOFF.md).

Для уже созданного JSON можно сделать отдельную очищенную копию, не затирая исходный результат:

```bash
.venv/bin/python scripts/postprocess_result.py \
  --input-json data/calls/bot1/bot1.json \
  --output-json data/calls/bot1/bot1.cleaned.json
```
