# ApplyPilot — Quickstart

Инструкция для чистого публичного клона. Реальные критерии, резюме, ответы, аккаунты и история заявок хранятся только в локальном `private/`.

## Установка и офлайн-проверка

Нужны Python 3.12+, Playwright/Chrome для браузерных действий и optional `telethon` для Telegram.

```bash
git clone https://github.com/nssanta/ApplyPilot.git
cd ApplyPilot
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m applypilot doctor
.venv/bin/python -m applypilot sources scan \
  --track default --fixture-dir tests/fixtures/public --json
```

Fixtures синтетические. Они проверяют обработку данных и не подтверждают полноту рынка или отправку.

## Локальная конфигурация

```bash
mkdir -p private/config
cp examples/profile.example.toml private/config/profile.toml
cp examples/search.example.toml private/config/search.toml
cp examples/sources.toml private/config/sources.toml
```

Настройте треки ролей, поисковые условия, названия резюме и source-аккаунты. Ролевой `--preset` заменяет словарь поиска и выбирает соответствующий ключ резюме из `profile.toml`; зарплата, регионы и прочие фильтры остаются настройками пользователя. Параметры источников можно задавать отдельно в `[source_limits]`.

## Поиск и очередь

Обычный поиск ничего не отправляет. Для bounded-прохода с возобновляемыми курсорами:

```bash
.venv/bin/python -m applypilot run campaign \
  --track default \
  --source hh.ru --source career.habr.com \
  --source getmatch.ru --source geekjob.ru \
  --days 30 --until-complete --resume --max-passes 100 --limit 100
```

Итог отдельно показывает полноту и число уникальных карточек по каждому источнику. `partial`, `truncated`, `blocked` и `failed` сохраняют незавершённость; `--resume` продолжает с сохранённого запроса/страницы или курсора. Полнота означает завершение доступного обхода с текущими запросами и фильтрами, а не всех вакансий в интернете.

## Явный live-запуск

Сначала проверьте `sources status` и `sources dispatch-plan`. Когда профиль и очередь готовы, тот же запуск включает отправку явными флагами:

```bash
.venv/bin/python -m applypilot run campaign \
  --track default \
  --source hh.ru --source career.habr.com \
  --source getmatch.ru --source geekjob.ru \
  --days 30 --until-complete --resume --max-passes 100 --limit 100 \
  --apply --allow-live --headless
```

HH использует пределы `[limits]`; остальные источники имеют отдельные бюджеты и могут быть переопределены в `[source_limits."career.habr.com"]`, `[source_limits."getmatch.ru"]` и других таблицах профиля. Скорость также ограничивается паузой драйвера. `DreamJob` остаётся зеркалом HH и не получает отдельную отправку.

Драйверы изолируют сбой вакансии, сохраняют каждую попытку в SQLite и подтверждают успех только по ответу источника. `UNKNOWN` помещается в карантин до сверки. Для простых screening-вопросов используются только явно сохранённые факты профиля. Неизвестные ответы и неподдерживаемые поля остаются в ручном разборе.

Живой driver одной площадки не означает пакетную готовность всех площадок. Проверьте сессию, форму и receipt конкретного источника перед расширением размера очереди. GeekJob и Яндекс пока требуют подтверждённого живого пилота.

## Локальная проверка проекта

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/check_public.py
.venv/bin/python scripts/offline_multisource_smoke.py
git diff --check
```

CI/CD не используется; проверки и сборку запускают локально. Подробнее: [источники](MULTISOURCE.md), [аккаунты](ACCOUNTS.md), [архитектура](ARCHITECTURE.md).
