# Установка и запуск

Документ описывает проверенную разработческую процедуру. Production должен получать код, wheels и веса через внутренний проверенный источник, а не напрямую из интернета.

## 0. Проверенный quality-путь: Windows x64 + NVIDIA

Текущий основной путь использует официальный CUDA bundle `whisper.cpp v1.9.2` и GGML F16 `Whisper large-v3`. Нужны Windows x64, NVIDIA GPU/driver и около 4 ГБ свободного диска сверх окружения.

### Автоматическая подготовка распакованного архива

Дважды нажать корневой `START_TRANSCRIBER.cmd`. Launcher требует не менее 6 ГиБ свободного места на время staging и последовательно выполняет проверяемые шаги установки. Если подходящего 64-битного Python 3.11 нет, он скачивает официальный installer Python 3.11.9 размером 26 216 840 байт и допускает его только при SHA-256 `5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde`. Python устанавливается в gitignored `.runtime/`, wheels кэшируются в `.runtime-cache/`, а рабочее окружение создаётся в `.venv/`.

Все сетевые загрузки выполняются до firewall. После успешной проверки модели и тестов launcher вызывает `security/windows-deny-network.ps1` через отдельное административное окно. Если elevation отменён или firewall не применён, worker не запускается. После успеха открывается `data/input` и выполняется `worker.py --mode poll --engine whisper --decoder beam_search`.

Launcher является путём локального пилота, а не production installer: он не задаёт корпоративные ACL, шифрование, срок хранения, service identity, централизованный audit или production-интеграцию CRM.

После создания `.venv` установить проект и обязательный `miniaudio==1.61`, затем в отдельной staging-фазе:

```powershell
.venv\Scripts\python.exe scripts\prepare_whisper_cpp.py --allow-network-download
.venv\Scripts\python.exe scripts\verify_model.py models\whisper-large-v3
```

Скрипт загружает официальный `whisper-cublas-12.4.0-bin-x64.zip` и `ggml-large-v3.bin`, проверяет зафиксированные размер/SHA-256 и публикует локальный manifest. Runtime не скачивает файлы и получает только локальные пути.

Для AAC ADTS дополнительно нужен зафиксированный PyAV wheel из `requirements/aac.txt`; процедура загрузки во время staging и офлайн-установки приведена в разделе 1. Whisper декодирует AAC во временный mono PCM WAV 16 кГц внутри каталога результата и удаляет его после обработки.

Одиночный запуск:

```powershell
.venv\Scripts\python.exe transcribe.py `
  --input data\input\1234.wav `
  --output data\output\1234.json `
  --engine whisper `
  --decoder beam_search `
  --verify-model-hashes
```

`--verify-model-hashes` читает все 3,1 ГБ перед каждым новым engine и подходит для допуска/проверки, но обычно не нужен на каждый файл после read-only публикации проверенного bundle.

После staging PowerShell от администратора должен заблокировать исходящую сеть для обоих runtime-процессов:

```powershell
powershell -NoProfile -File security\windows-deny-network.ps1
```

Текущий pinned whisper.cpp staging script намеренно отказывается работать на другой ОС. Для Linux/macOS нужен отдельный проверенный runtime bundle и новый ADR; T-one-процедура ниже остаётся fallback.

## 1. Staging T-one с сетью

Клонировать и pin-ить официальный код:

```bash
git clone https://github.com/voicekit-team/T-one.git third_party/T-one
git -C third_party/T-one checkout 3c5b6c015038173840e62cea99e10cdb1c759116
```

Создать Python 3.9+ environment. Проверено на Python 3.12.13:

```bash
python3.12 -m venv .venv
.venv/bin/pip install poetry==2.1.1
```

Установить точные main-зависимости из upstream `poetry.lock` без root package, затем обычный wheel T-one и отдельный аудио-decoder:

```bash
source .venv/bin/activate
POETRY_VIRTUALENVS_CREATE=false poetry \
  -C third_party/T-one install --only main --no-root
deactivate

.venv/bin/pip install --no-deps --no-build-isolation third_party/T-one
.venv/bin/pip install --only-binary=:all: miniaudio==1.61
```

Для AAC ADTS дополнительно скачать и установить зафиксированный PyAV wheel. Проверены CPython 3.12/macOS Intel и CPython 3.11/Windows x64:

```bash
.venv/bin/pip download \
  --only-binary=:all: --no-deps --require-hashes \
  --dest .poetry-cache/aac \
  -r requirements/aac.txt

.venv/bin/pip install \
  --no-index --no-deps --require-hashes \
  --find-links .poetry-cache/aac \
  -r requirements/aac.txt
```

Wheel содержит библиотеки FFmpeg; PyAV имеет BSD-3-Clause, а транзитивные лицензии сборки FFmpeg нужно проверить до внутренней поставки.

Editable install не рекомендуется: на проверенном пути с кириллицей `.pth` работал нестабильно. Перед внутренней поставкой Poetry, build backend, все wheels и их hashes также нужно pin/mirror; эта процедура ещё не является готовым offline wheelhouse.

### Проверенный Windows greedy runtime

На Windows 10 x64 фактически проверен CPython 3.11.9. Сетевой staging выполняется один раз и сохраняет только зафиксированные wheels; аудио и модель в нём не участвуют:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip download `
  --only-binary=:all: --no-deps --require-hashes `
  --dest .poetry-cache\windows-cp311 `
  -r requirements\windows-greedy.txt
```

Дальнейшая установка идёт строго без индекса:

```powershell
.venv\Scripts\python.exe -m pip install `
  --no-index --find-links .poetry-cache\windows-cp311 `
  --no-deps --require-hashes `
  -r requirements\windows-greedy.txt

.venv\Scripts\python.exe -m pip wheel `
  --no-cache-dir --no-index --no-deps --no-build-isolation `
  --wheel-dir .poetry-cache\windows-cp311 `
  third_party\T-one

.venv\Scripts\python.exe -m pip install `
  --no-index --no-deps `
  .poetry-cache\windows-cp311\tone-0.1.0-py3-none-any.whl
```

Для AAC на Windows затем повторить `pip download`/`pip install` из предыдущего раздела, заменив `.venv/bin/` на `.venv\Scripts\`.

Upstream `tone==0.1.0` объявляет Python binding KenLM обязательным даже для greedy, хотя локальный greedy-путь его не вызывает. На проверенной Windows-среде binding не устанавливается: нет официального wheel, а непроверенная локальная C++-сборка исключена. Поэтому `pip check` сообщает только известное отсутствие `tone -> kenlm`; beam search остаётся недоступен. Решение зафиксировано в ADR-008.

После staging запустить PowerShell от администратора и запретить egress именно для runtime Python:

```powershell
powershell -NoProfile -File security\windows-deny-network.ps1
```

Скрипт идемпотентен и откажется продолжать, если одноимённое firewall-правило имеет другие параметры. Без административного firewall использовать только изолированную среду с внешним deny-all egress; Python-level guard не ограничивает нативные DLL полностью.

## 2. Подготовка модели

Greedy bundle (144 193 371 байт):

```bash
.venv/bin/python scripts/prepare_model.py --allow-network-download
```

Beam-search bundle дополнительно скачивает 5 463 477 004 байта:

```bash
.venv/bin/python scripts/prepare_model.py \
  --allow-network-download \
  --include-kenlm
```

Скрипт pin-ит model revision, проверяет размер и SHA-256 и только после успеха публикует `manifest.json`. Он staging-only и не импортируется runtime.

Повторная локальная проверка без сети:

```bash
.venv/bin/python scripts/verify_model.py models/t-one --greedy-only
```

Для production перенести проверенный bundle через внутренний источник, сохранить model card/Apache-2.0 и монтировать каталог read-only.

## 3. Одиночный файл

```bash
.venv/bin/python transcribe.py \
  --input data/calls/1234/1234.wav \
  --output data/calls/1234/1234.json \
  --markdown-output data/calls/1234/1234.md \
  --engine t-one \
  --decoder greedy
```

Windows:

```powershell
.venv\Scripts\python.exe transcribe.py `
  --input data\calls\1234\1234.wav `
  --output data\calls\1234\1234.json `
  --markdown-output data\calls\1234\1234.md `
  --engine t-one `
  --decoder greedy `
  --verify-model-hashes
```

Имена должны иметь одинаковый безопасный `call_id`. Для beam search убрать `--decoder greedy` либо явно указать `beam_search` после подготовки KenLM.

## 4. Очередь и режимы

Основной Windows/NVIDIA-путь:

```powershell
.venv\Scripts\python.exe worker.py --mode once --engine whisper --decoder beam_search
.venv\Scripts\python.exe worker.py --mode poll --engine whisper --decoder beam_search
```

Переносимый T-one fallback:

```bash
# Один проход / ручная пакетная обработка
.venv/bin/python worker.py --mode once --engine t-one --decoder greedy

# Постоянная периодическая проверка
.venv/bin/python worker.py --mode poll --engine t-one --decoder greedy

# Семантика «после появления»; сейчас реализована polling-ом
.venv/bin/python worker.py --mode watch --engine t-one --decoder greedy

# Запуск внешним cron/systemd timer ночью
.venv/bin/python worker.py --mode batch --engine t-one --decoder greedy
```

По умолчанию файл должен быть неизменным 5 секунд. Затем он без перекодирования перемещается из `data/input` в `data/calls/{call_id}/`; туда же публикуются `{call_id}.json` и `{call_id}.md`. SQLite находится в `data/queue.sqlite3`, технический лог — `logs/worker.log`. Failed job повторяется ограниченно только для временных классов; ручной повтор:

```bash
.venv/bin/python worker.py --mode once --engine t-one --decoder greedy --requeue 1234
```

## 5. Тесты

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Windows: `.venv\Scripts\python.exe -m unittest discover -s tests -v`.

Фактические тестовые аудио и результаты исключены из Git. В Git хранится только текстовый эталон и код генерации/оценки.
