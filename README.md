# Локальный транскрибатор звонков

[![Tests](https://github.com/gurulow1/local-call-transcriber/actions/workflows/tests.yml/badge.svg)](https://github.com/gurulow1/local-call-transcriber/actions/workflows/tests.yml)

Автономный CLI и SQLite-worker для русской речи:

```text
data/input/1234.wav
  -> data/calls/1234/
       1234.wav
       1234.json
       1234.md
```

Основной движок — `whisper.cpp v1.9.2` с локальной `Whisper large-v3`, beam search, CUDA и обязательным локальным Silero VAD. Лёгкий T-one/greedy сохранён как fallback. Облачные API и автоматическая загрузка весов во время обработки запрещены.

На проверенном Windows-хосте с RTX 3080 текущая конфигурация Whisper+VAD обработала шесть открытых FLEURS-записей общей длительностью 84,96 с за 21,203 с (взвешенный `RTF 0,2496`): 4 ошибки на 135 слов, micro-WER 2,96%. Это короткая читаемая речь, а не capacity-тест и не телефонные звонки. На отдельном синтетическом примере Whisper правильно распознал БИК, ИНН, НКЦ, сумму и дату, но фразу «четыре ноля и один» оформил как `4001`; критичные реквизиты нельзя принимать без бизнес-валидации.

## Что отличает пилот

- **Zero-egress runtime:** модель и VAD загружаются только с диска, а readiness-проверка fail-closed сверяет их hashes и точные firewall rules для Python и `whisper-cli`.
- **Проверяемая расшифровка:** JSON хранит raw ASR рядом с детерминированно очищенным текстом, таймкодами, идентификаторами модели/VAD и версией постобработки.
- **Два канала без новой модели:** Whisper автоматически включает штатный stereo-diarize и помечает левый/правый канал как `SPEAKER_00`/`SPEAKER_01`; mono-записи остаются без меток говорящих.
- **Crash-safe конвейер:** SQLite lease-очередь, атомарная публикация и идемпотентный `call_id` защищают от двойной обработки и частичных результатов.
- **Evidence, а не обещания:** локальный benchmark связывает код, эталоны, результаты и аудио SHA-256; неречевые regression-записи должны давать ноль слов.

## Запуск получателем из архива

Воспроизводимый source-архив пилота собирается командой `python scripts/build_pilot_archive.py`. Результат и отдельный SHA-256 появляются в `dist/`; в ZIP намеренно нет аудио, результатов, логов, локальных окружений и весов модели. Отдельный самодостаточный evidence-архив собирается `python scripts/build_pilot_evidence.py` только из локального acceptance-каталога и включает атрибуцию открытых записей и manifest всех файлов. При первом запуске получатель выполняет контролируемый staging, описанный ниже.

Предварительно устанавливать Python и зависимости не нужно. Распакуйте архив в обычную папку с правом записи и запустите файл своей платформы:

- **Windows 10/11 x64:** дважды нажмите `Запустить транскрибатор.cmd`. При доступной NVIDIA автоматически используется Whisper large-v3/CUDA; без NVIDIA — T-one/CPU.
- **Linux x86_64, glibc 2.28+:** разрешите выполнение `Запустить транскрибатор.sh` в свойствах файла, если файловый менеджер этого попросит, затем выберите «Запустить». Используется T-one/CPU.
- **macOS Intel или Apple Silicon (дополнительный путь):** дважды нажмите `Запустить транскрибатор.command`. Используется T-one/CPU.

Первый запуск требует интернет: запускатор скачивает в папку проекта проверенный по SHA-256 `uv 0.11.13`, управляемый Python 3.12, фиксированные прямые зависимости и выбранную модель. Для Windows/NVIDIA требуется около 3,8 ГБ загрузок; CPU-путь заметно меньше. Перед первым worker-запуском выполняются локальные тесты. На Windows затем появляется стандартный запрос администратора: он нужен, чтобы заблокировать исходящую сеть для Python и `whisper-cli.exe`; при отказе worker не запускается. После установки открывается `data/input`: положите туда аудио, а готовые JSON и Markdown появятся в `data/calls`.

Повторный запуск использует локальные файлы и заново сверяет модель с manifest по размеру и SHA-256. Журнал установки находится в `logs/setup.log`. One-click путь предназначен для локального пилота; production должен получать полный hash-pinned bundle из внутреннего источника и применять OS-level запрет исходящей сети.

## Быстрый запуск на проверенном Windows/NVIDIA-хосте

Входные форматы: WAV, MP3, FLAC, OGG и AAC ADTS. AAC декодируется локальным PyAV из зафиксированного wheel; для Whisper внутри временного scratch-каталога создаётся 16 кГц WAV с сохранением stereo, который удаляется после обработки. Двухканальный Whisper-вход получает метки каналов в JSON и Markdown. В runtime ничего не скачивается.

После ASR выполняется локальная детерминированная постобработка: регистр и финальный знак учитывают контекст предложения, а канонические написания терминов исправляются по редактируемому [`config/glossary.json`](config/glossary.json). Словарь не должен дописывать смысл. Исходная выдача ASR всегда сохраняется в `raw_text` и `segments[].asr_text`; `text` содержит очищенную версию. LLM в этом пути не используется.

## Структура

- `docs/` — требования, архитектура, аудит и журнал решений;
- `models/` — локальные веса (не должны попадать в Git);
- `data/input/` — папка для нового аудио;
- `data/calls/{call_id}/` — готовая папка звонка с аудио, JSON и Markdown;
- `data/failed/` — артефакты и маркеры неуспешной обработки;
- `src/` — код прототипа;
- `config/glossary.json` — локальный словарь канонических терминов и точечных коррекций;
- `tests/` — автоматические тесты;
- `THIRD_PARTY_NOTICES.md` — инвентарь лицензий пилотной поставки;
- `schemas/` и `examples/crm/` — версионированный контракт для CRM-команды;
- `logs/` — технические журналы без текста звонков.

Статус и порядок работ ведутся в [docs/TASKS.md](docs/TASKS.md).

## Перенос на другой компьютер

Публичный showcase-репозиторий хранит только код, документацию, тесты и безопасные manifests; модели, аудио, результаты и локальные окружения в Git не публикуются:

```bash
git clone https://github.com/gurulow1/local-call-transcriber.git
cd local-call-transcriber
```

Локальные артефакты Whisper подготавливаются отдельным staging-шагом:

```powershell
.venv\Scripts\python.exe scripts\prepare_whisper_cpp.py --allow-network-download
.venv\Scripts\python.exe scripts\verify_model.py models\whisper-large-v3
.venv\Scripts\python.exe scripts\verify_model.py models\whisper-vad
```

Staging скачивает около 0,67 ГБ официального CUDA-runtime и 3,1 ГБ весов, затем проверяет размер и SHA-256. Runtime после этого сеть не использует.

## Проверенный одиночный запуск

Основной quality-режим на проверенном Windows/NVIDIA-хосте:

```powershell
.venv\Scripts\python.exe transcribe.py `
  --input data\calls\1234\1234.wav `
  --output data\calls\1234\1234.json `
  --markdown-output data\calls\1234\1234.md `
  --engine whisper `
  --decoder beam_search
```

Для узкой терминологии можно передать только словарь терминов, без ожидаемых значений:

```powershell
--initial-prompt "БИК, ИНН, НКЦ, расчётный счёт, клиринг"
```

Фоновая обработка:

```powershell
.venv\Scripts\python.exe worker.py --mode once --engine whisper
.venv\Scripts\python.exe worker.py --mode poll --engine whisper
```

Неудачная задача повторяется явно: `worker.py --mode once --requeue CALL_ID`. Готовый корректный JSON повторно не обрабатывается.

## Лёгкий fallback

T-one работает без GPU и остаётся доступен явно:

```powershell
.venv\Scripts\python.exe transcribe.py `
  --input data\calls\1234\1234.wav `
  --output data\calls\1234\1234.json `
  --markdown-output data\calls\1234\1234.md `
  --engine t-one `
  --model-dir models\t-one `
  --decoder greedy
```

T-one beam search на Windows не готов: для него нужны локальный `kenlm.bin` на 5,46 ГБ и проверенная сборка Python binding KenLM.

## Безопасность и перенос

- модель загружается только с локального пути;
- исходное аудио не изменяется и не удаляется;
- временные JSON whisper.cpp и WAV после декодирования AAC создаются внутри локального scratch-каталога результата и удаляются после разбора;
- логи не содержат текст звонка;
- перед корпоративными данными нужен OS-level deny-all egress;
- модели, runtime, аудио, результаты, логи и `.venv` исключены из Git.

После клонирования репозитория runtime-артефакты подготавливаются заново через контролируемый источник. Код обвязки объявлен proprietary в `pyproject.toml`; публикация исходников не предоставляет лицензию на их повторное распространение. Условия сторонних компонентов перечислены в [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Подробности: [установка](docs/INSTALLATION.md), [качество](docs/QUALITY_EVALUATION.md), [безопасность](docs/SECURITY.md), [архитектура](docs/ARCHITECTURE.md).

## Проверка

На Windows перед демонстрацией запустите `Проверить готовность.cmd`. Проверка сверяет SHA-256 модели и VAD, версию whisper.cpp, GPU/VRAM, запись в рабочие каталоги и точные OS firewall rules. Любой `FAIL` означает `NO-GO`.

GitHub Actions выполняет модель-независимые unit/contract-тесты на Windows и Linux с Python 3.11/3.12. Весов и аудио в CI нет; реальная Whisper+VAD-приёмка остаётся отдельным локальным gate и описана в [пилотном отчёте](docs/PILOT_REPORT.md).

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Постоянная проверка каталога:

```bash
.venv/bin/python worker.py --mode poll --engine whisper --decoder beam_search
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

Проект не включает LLM, суммаризацию, диаризацию, CRM-специфичную интеграцию или UI. Для распознавания отдельная LLM не нужна; добавлять её для «исправления» реквизитов опасно без отдельного набора качества, потому что она может правдоподобно выдумывать значения. Reference API остаётся нейтральным loopback-only контрактом для технической команды.
