# ApplyPilot — Quickstart

Инструкция для **чистого публичного клона**, без реальных аккаунтов и локальных операционных данных.

## Установка

Нужен Python 3.12+.

~~~bash
git clone https://github.com/nssanta/ApplyPilot.git
cd ApplyPilot
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m applypilot doctor
~~~

Для браузерных функций дополнительно требуется установленный браузер и Playwright; для работы через Telegram используется optional-зависимость `telethon`.

## Тестирование без сети и откликов

~~~bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/check_public.py
.venv/bin/python scripts/offline_multisource_smoke.py

.venv/bin/python -m applypilot sources scan \
  --track default --fixture-dir tests/fixtures/public --json
~~~

Все тестовые примеры содержат синтетические вакансии; их результат не описывает личную кампанию пользователя.

## Локальные настройки

~~~bash
mkdir -p private/config
cp examples/profile.example.toml private/config/profile.toml
cp examples/search.example.toml private/config/search.toml
cp examples/sources.toml private/config/sources.toml
~~~

Откройте скопированные файлы и задайте собственные критерии, резюме и метки аккаунтов. Не коммитьте `private/`, cookies, session-файлы, ключи или реальные отчёты.

## Поиск и очередь

~~~bash
.venv/bin/python -m applypilot sources list

# Живой поиск — без отправки работодателям
.venv/bin/python -m applypilot sources scan \
  --track default --limit 50

# Состояние и dry-run
.venv/bin/python -m applypilot sources status --track default
.venv/bin/python -m applypilot sources dispatch-plan \
  --track default --limit 50 --json
~~~

Подходящую вакансию следует оценивать по реальному описанию и ограничениям, а не только по score. Операционные решения хранятся в SQLite выбранного пользователя.

## Одобрение и диспетчер

~~~bash
# VACANCY_ID — пример заполнителя, не настоящая вакансия
.venv/bin/python -m applypilot sources approve-batch \
  --track default --source career.habr.com \
  --id VACANCY_ID --state APPROVED --reason 'роль и условия проверены'

# Dry-run: отправка выключена
.venv/bin/python -m applypilot sources dispatch-apply \
  --track default --limit 10 --json
~~~

Live-действия включаются отдельными флагами. Они требуют авторизованной сессии, точного канала, одобрения и проверенного драйвера. `UNKNOWN` после возможной отправки не допускает автоматического повтора.

## Примеры read-only проверок

~~~bash
# Аудит формы Habr или GeekJob без submit
.venv/bin/python -m applypilot sources form-audit \
  --track default --source career.habr.com --view review --limit 10 --json

# Telegram: проверка статуса, синхронизация и preview без callback
.venv/bin/python -m applypilot accounts telegram-status --online
.venv/bin/python -m applypilot accounts telegram-sync --limit 50
.venv/bin/python -m applypilot accounts telegram-plan \
  --track default --json
~~~

Детали подключения Telegram, HH OAuth и живых драйверов находятся в [аккаунтах](ACCOUNTS.md) и [Multi-Source](MULTISOURCE.md). Не переносите в публичные инструкции данные своего профиля или фактические отчёты по откликам.

## Веб-панель

~~~bash
./run.sh
~~~

Панель использует ту же локальную очередь, что и CLI, и предназначена для loopback-доступа.

## Безопасность публикации

Проверяйте отсутствие приватных файлов и переменных окружения в Git, а также кодовые тесты и `scripts/check_public.py`. Исправление `main` не удаляет данные из ранее опубликованной истории коммитов.
