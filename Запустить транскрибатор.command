#!/bin/zsh

PROJECT_DIR=${0:A:h}
cd "$PROJECT_DIR" || exit 1

if [[ ! -x .venv/bin/python ]]; then
  echo "Ошибка: локальное Python-окружение .venv не найдено."
  echo "Нажмите Enter, чтобы закрыть окно."
  read
  exit 1
fi

echo "Транскрибатор запущен."
echo "Кладите аудио в data/input."
echo "Для каждого звонка появится папка data/calls/имя с аудио, JSON и Markdown."
echo "Имя файла: только латиница, цифры, _ и -, например bot-001.aac."
echo "Для остановки нажмите Control+C."
echo

exec .venv/bin/python worker.py --mode poll --decoder greedy
