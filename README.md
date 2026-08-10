# Локальный транскрибатор звонков

Минимальная цель проекта:

```text
data/input/1234.wav -> локальный ASR -> data/output/1234.json
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

## Структура

- `docs/` — требования, архитектура, аудит и журнал решений;
- `models/` — локальные веса (не должны попадать в Git);
- `data/input/` — тестовые входные записи;
- `data/output/` — JSON/TXT результаты;
- `data/failed/` — артефакты и маркеры неуспешной обработки;
- `src/` — код прототипа;
- `tests/` — автоматические тесты;
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
  --input data/input/1001.wav \
  --output data/output/1001.json \
  --decoder greedy
```

На проверенной Windows-среде эквивалентная команда:

```powershell
.venv\Scripts\python.exe transcribe.py `
  --input data\input\1001.wav `
  --output data\output\1001.json `
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

`--mode watch` сейчас использует переносимый polling, а `--mode batch` предназначен для запуска внешним ночным scheduler-ом. Неудачную задачу можно повторить только явно: `--requeue CALL_ID`.

Полная проверенная процедура описана в [docs/INSTALLATION.md](docs/INSTALLATION.md). Аудит и ограничения — в [docs/REPOSITORY_AUDIT.md](docs/REPOSITORY_AUDIT.md).
