# Установка и запуск

Документ описывает проверенную разработческую процедуру. Production должен получать код, wheels и веса через внутренний проверенный источник, а не напрямую из интернета.

## Запуск архива получателем

Получателю не требуется заранее устанавливать Python или вводить команды установки. Архив нужно распаковать в папку с правом записи и запустить файл своей платформы:

| Платформа | Файл | Автоматически выбранный движок |
|---|---|---|
| Windows 10/11 x64 + NVIDIA | `Запустить транскрибатор.cmd` | Whisper large-v3/CUDA |
| Windows 10/11 x64 без NVIDIA | `Запустить транскрибатор.cmd` | T-one/CPU |
| Linux x86_64, glibc 2.28+ | `Запустить транскрибатор.sh` | T-one/CPU |
| macOS Intel/Apple Silicon, дополнительный путь | `Запустить транскрибатор.command` | T-one/CPU |

В Linux файловый менеджер может один раз попросить разрешить выполнение `.sh`; это делается через свойства файла без Terminal. Нужны стандартные `bash`, `curl`, `tar`, `sha256sum` и `mktemp`. Linux ARM пока не заявлен: у `miniaudio 1.61` нет готового CPython 3.12 wheel для этой платформы.

Первый запуск выполняет явную сетевую staging-фазу:

- скачивает закреплённый `uv 0.11.13` нужной платформы и проверяет SHA-256;
- создаёт локальное окружение с управляемым Python 3.12 внутри `.poetry-cache`, не меняя возможную разработческую `.venv`;
- для Windows/NVIDIA скачивает проверенные whisper.cpp/CUDA и Whisper large-v3 (около 3,8 ГБ);
- для CPU-пути скачивает проверенный архив T-one и `model.onnx`;
- ставит фиксированные прямые runtime-зависимости, включая AAC decoder;
- проверяет модель по размеру/SHA-256 и выполняет локальные тесты;
- на Windows запрашивает административное подтверждение и ставит outbound-block правила для управляемого Python и `whisper-cli.exe`;
- только после успешной проверки и firewall открывает `data/input` и запускает worker.

Повторный запуск не выполняет staging заново, если marker, окружение и импорты на месте, а модель проходит повторную проверку размера/SHA-256 по manifest. На Windows firewall-правила также проверяются до каждого запуска worker. Технический журнал сохраняется в `logs/setup.log`. One-click путь предназначен для локального пилота; production по-прежнему должен использовать внутренний полный hash-pinned wheelhouse/bundle и OS-level deny-all egress.

## 0. Проверенный quality-путь: Windows x64 + NVIDIA

Текущий основной путь использует официальный CUDA bundle `whisper.cpp v1.9.2` и GGML F16 `Whisper large-v3`. Нужны Windows x64, NVIDIA GPU/driver и около 4 ГБ свободного диска сверх окружения.

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

One-click Windows-запуск делает этот шаг автоматически после staging и не запускает worker, если пользователь отклонил административное подтверждение. При ручной установке PowerShell от администратора должен заблокировать исходящую сеть для обоих runtime-процессов:

```powershell
powershell -NoProfile -File security\windows-deny-network.ps1
```

Текущий pinned whisper.cpp staging script намеренно отказывается работать на другой ОС. One-click запускаторы Linux/macOS используют описанный ниже T-one/greedy CPU fallback.

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
