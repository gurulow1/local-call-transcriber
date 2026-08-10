# Проверка автономности

## Выполнено 2026-08-09

1. Код T-one клонирован и зависимости установлены в отдельной staging-фазе.
2. `model.onnx` получен по pin `106f3b0b32a9e107eb613312e4ebc61ff3d53926`, размер и SHA-256 проверены.
3. После завершения staging каждый CLI-запуск был новым процессом.
4. CLI вызывает только `StreamingCTCPipeline.from_local()`.
5. До импорта T-one выставляются `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1`.
6. На период model load, decode и inference Python-level `socket.connect`, `connect_ex`, `create_connection` и `getaddrinfo` заменены fail-closed guard-ом; unit test подтверждает отказ.
7. Фактические `test_call.flac` и `1001.wav` успешно обработаны из локального каталога в среде выполнения, где обычные команды ранее не могли разрешить DNS/достучаться до GitHub без отдельного сетевого разрешения.

Это подтверждает практическую автономность проверенного Python-пути, но **не является полным аудитом сети**: нативная библиотека теоретически может открыть socket в обход Python, а трафик процесса ещё не снимался `tcpdump`/NDR.

## Воспроизводимая OS-level проверка на macOS

В проекте есть тестовый profile `security/macos-deny-network.sb`. Ниже сохранена историческая команда проверки, выполненной до перехода на папки `data/calls`:

```bash
sandbox-exec -f security/macos-deny-network.sb \
  .venv/bin/python transcribe.py \
  --input data/input/1001_offline.wav \
  --output data/output/1001_offline.json \
  --decoder greedy
```

Для новой проверки нужно взять отдельную копию синтетического аудио с новым `call_id`: рабочий обработчик сам перенесёт её из `data/input` в `data/calls/<call_id>/` и создаст JSON и Markdown. Уже созданные результаты не перезаписываются.

Получен `completed`: 16,115 с WAV обработаны за 9,430 с, RTF `0,5852`; текст совпал с обычным sandbox-запуском, исходный SHA-256 не изменился. Этот механизм предназначен только для локального доказательства; `sandbox-exec` deprecated и не заменяет промышленный firewall.

Первый диагностический запуск выявил не сетевую, а packaging-проблему editable `.pth` с кириллическим путём. После установки audited checkout обычным wheel тест прошёл без `PYTHONPATH`. Production не должен зависеть от editable checkout.

## Как повторить специалистам ИБ на Linux

Рекомендуется выполнить все три уровня:

1. Запустить сервис под отдельным UID в network namespace без интерфейса либо в контейнере с `--network none`.
2. На хостовом firewall задать deny-all egress для UID/container c логированием блокировок; не полагаться только на DNS-блокировку.
3. Снимать попытки через `tcpdump -i any`, Wireshark/eBPF или корпоративный NDR/EDR и сопоставлять PID/UID.

Проверять cold start, успешный файл, битый файл, отсутствующий manifest, отсутствующий вес и повторный запуск после очистки внешних HF-кэшей. В production каталог весов монтировать read-only, а исходящий HTTP(S), произвольный DNS, QUIC и прокси запрещать на уровне узла и сегмента.

## Ещё не выполнено

- захват DNS/egress конкретного PID;
- проверка на выбранной промышленной Linux-системе;
- анализ системных вызовов нативных ONNX Runtime/miniaudio/KenLM;
- offline wheelhouse/install с пустого хоста.

## Windows-проверка 2026-08-09

- CPython 3.11.9 / Windows 10 x64, T-one greedy, ONNX Runtime 1.22.0.
- Зависимости сначала получены в staging, затем установлены из локального wheelhouse с `--no-index`; hashes проверены по `requirements/windows-greedy.txt`.
- `1001.wav` и upstream `test_call.flac` успешно обработаны локальным `model.onnx` в обычных новых процессах.
- Во время отдельного FLAC-инференса PID опрашивался через `netstat -ano` каждые 50 мс; сетевых строк для процесса не наблюдалось, результат `completed` получен за 2,467 с.
- Runtime выполнялся в среде без сетевого разрешения инструмента и с `deny_python_network()`/HF offline flags.
- Добавлен `security/windows-deny-network.ps1`, создающий постоянное outbound-block правило для `.venv\Scripts\python.exe`. Применить его в текущей среде не удалось из-за отсутствия административных прав; синтаксис скрипта проверен.

Опрос `netstat` может пропустить очень короткое соединение и не заменяет ETW/WFP/NDR. До реальных данных Windows-запуск вне изолированной среды требует применения firewall-правила администратором и отдельной проверки заблокированных попыток.
