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

Пересчитать решения по уже сохранённой очереди без сети можно так:

```bash
.venv/bin/python -m applypilot queue sweep --track default --dry-run
```

`queue sweep` пересматривает все незавершённые строки, включая старые `NOT_STARTED/READY`, и сохраняет `decision`. Без `--dry-run` чистые FIT могут получить `APPROVED` по policy; человеческие `SKIP/DEFERRED` не перезаписываются.

Сводка funnel по источникам:

```bash
.venv/bin/python -m applypilot queue stats --track default
```

## Одна команда: `go`

`go` — основной интерфейс. Он берёт активные треки из operating policy и по умолчанию делает полный цикл по всем источникам:

```bash
# только scan → decide, без внешних действий
.venv/bin/python -m applypilot go --track default --scan-only

# полный цикл с видимым окном (удобно для первого live-прогона и CAPTCHA)
.venv/bin/python -m applypilot go --track default --visible

# пакетный режим без окна; лимиты и паузы площадок сохраняются
.venv/bin/python -m applypilot go --track default --headless

# ad-hoc запрос, зарплата и желаемое число подтверждённых откликов
.venv/bin/python -m applypilot go --track default \
  --query "AI агент" --salary-from 120000 --sources all --target 50 --target-scope total --headless

# один цикл вместо until-exhausted
.venv/bin/python -m applypilot go --track default --headless --once
```

`--target N` требует явного `--target-scope total` (всего) или `per-source` (на каждой нативной площадке). Считаются только новые подтверждённые отправки этой кампании. Размер батча `--limit` не является потолком кампании. ID, параметры и связанные отправки сохраняются в SQLite; продолжение: `go --campaign-id ID`.

Пустой батч не доказывает исчерпание: продолжаются незавершённый поиск, дозагрузка описаний и готовая очередь. Результат различает `target_reached`, `exhausted`, `technical_budget`, `configured_budget`, `platform_blocked`, `user_required` и отсутствие корректного отчёта. При недоборе возвращается ненулевой код. `per_day = 0` или отсутствие дневного бюджета отключает искусственный локальный потолок; явно заданный бюджет сохраняется.

Фильтры запуска и происхождение значений включаются в приватный JSON-отчёт. `queue sweep --rescore` пересчитывает сохранённые карточки без отправки. Инструкции нового чата находятся в корневом `AGENTS.md`, актуальная локальная задача — в `private/current/00_START_HERE.md`; архивные отчёты не являются дополнительными инструкциями.

Одна вакансия в нескольких источниках, включая DreamJob-зеркало, видна так:

```bash
.venv/bin/python -m applypilot queue links --track default --source dreamjob.ru
```

`--sources all` — значение по умолчанию; можно передать `--sources hh.ru,career.habr.com`. `--days` задаёт окно поиска, `--limit` — батч, `--max-cycles` — технический бюджет продолжения. Живой режим требует `live_apply = true` в `private/config/operating-policy.toml`; без него live-запуск завершается понятной ошибкой; для поиска используйте `--scan-only`.

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

Для последовательных циклов без ручного перезапуска добавьте `--until-exhausted` (или `--cycles N`):

```bash
.venv/bin/python -m applypilot run campaign \
  --track default --source hh.ru --days 30 --resume \
  --apply --allow-live --until-exhausted --max-cycles 20
```

Пустой раунд не завершает цикл: сначала проверяются полнота поиска, дозагрузка описаний и оставшаяся очередь. `--max-cycles` — явный страховочный предел, а не дневной лимит площадки.

Драйверы изолируют сбой вакансии, сохраняют каждую попытку в SQLite и подтверждают успех только по ответу источника. Один `UNKNOWN` без CAPTCHA карантинится по вакансии, остальные независимые вакансии продолжают батч; CAPTCHA, потеря сессии и platform block останавливают источник. Для screening-вопросов используются только явно сохранённые факты профиля. Неизвестные ответы и неподдерживаемые поля остаются в ручном разборе.

Живой driver одной площадки не означает пакетную готовность всех площадок. Проверьте сессию, форму и receipt конкретного источника перед расширением размера очереди. GeekJob и Яндекс подтверждены live-receipt; Getmatch подтверждён и Telegram-callback, и native web-формой (POST + `/applications`). DreamJob остаётся зеркалом HH: второй отклик не отправляется, но `queue links` показывает общий отклик и ответ.

## Локальная проверка проекта

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/check_public.py
.venv/bin/python scripts/offline_multisource_smoke.py
git diff --check
```

CI/CD не используется; проверки и сборку запускают локально. Подробнее: [источники](MULTISOURCE.md), [аккаунты](ACCOUNTS.md), [архитектура](ARCHITECTURE.md).
