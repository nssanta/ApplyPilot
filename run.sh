#!/usr/bin/env bash
# Быстрый запуск ApplyPilot: поднимает локальную веб-админку и открывает её в браузере.
#
#   ./run.sh            # запуск на порту 8765
#   ./run.sh 9000       # другой порт
#
# Ключ aitunnel читает само приложение: runtime-переменная AITUNNEL_API_KEY,
# затем сохранённые настройки админки, затем private/config/aitunnel.key.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT="${1:-8765}"

if [ ! -x .venv/bin/python ]; then
  echo "Нет .venv — сначала установка (см. README, раздел «Установка»)." >&2
  exit 1
fi

echo "ApplyPilot → http://127.0.0.1:${PORT}  (Ctrl-C для остановки)"
exec .venv/bin/python -m applypilot admin --port "${PORT}" --open
