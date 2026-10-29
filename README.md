# ApplyPilot

ApplyPilot — локальный инструмент для поиска вакансий, оценки соответствия и контролируемых откликов. Включает CLI, SQLite и локальную веб-панель.

**Рабочие аккаунты, критерии поиска, резюме, сессии, переписки и история откликов не являются публичной документацией.** Они хранятся в пользовательском каталоге `private/`, не входящем в Git.

## Возможности

- Поиск вакансий по нескольким платформам и раздельные поисковые треки.
- Нормализация карточек, объяснимый скоринг и optional LLM-скрининг.
- Track-scoped решения `APPLY/REVIEW/REJECT/APPLIED/BLOCKED`, повторный sweep старых неопределённостей и отдельные review/approval очереди.
- Чтение сложных анкет (text, textarea, number, select, radio, checkbox) с ответами только из подтверждённых приватных фактов.
- SQLite-журнал, канонические каналы отправки и защита от дублей.
- Безопасные `scan`, `form-audit`, `dispatch-plan`, `queue sweep` и `dry-run`.
- Одна команда `go` для цикла `scan → decide → apply → verify` по всем источникам, с `--visible` и `--headless`.
- Браузерные и Telegram-драйверы откликов, требующие явного разрешения и проверки результата; один `UNKNOWN` карантинится без остановки независимых вакансий.
- Опциональная веб-панель и экспериментальная Debian-сборка.

## Установка

Требуется Python 3.12+.

~~~bash
git clone https://github.com/nssanta/ApplyPilot.git
cd ApplyPilot
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m applypilot doctor
~~~

Для браузерных функций могут потребоваться Playwright и установленный Chrome, для Telegram — optional-зависимость `telethon`.

## Начало работы

Скопируйте нейтральные примеры в собственную локальную конфигурацию:

~~~bash
mkdir -p private/config
cp examples/profile.example.toml private/config/profile.toml
cp examples/search.example.toml private/config/search.toml
cp examples/sources.toml private/config/sources.toml

# Список провайдеров и офлайн-скан на синтетических HTML.
.venv/bin/python -m applypilot sources list
.venv/bin/python -m applypilot sources scan \
  --track default --fixture-dir tests/fixtures/public --json

# Только поиск и решения, без отправок
.venv/bin/python -m applypilot go --track default --scan-only

# Полный цикл; live-отправки разрешены operating-policy.toml
.venv/bin/python -m applypilot go --track default --visible
.venv/bin/python -m applypilot go --track default --headless

# Ad-hoc поиск и целевое число подтверждённых откликов
.venv/bin/python -m applypilot go --track default \
  --query "Example Role" --salary-from 100000 --sources all --target 10 --target-scope total --headless

# Одна логическая вакансия: HH, DreamJob-зеркало, отклик и ответ
.venv/bin/python -m applypilot queue links --track default --source dreamjob.ru

# Без отправки заявок
.venv/bin/python -m applypilot sources dispatch-plan \
  --track default --limit 10 --json
~~~

Настраивайте собственные роли, зарплатные ожидания, географию, ограничения и резюме только в локальных профилях. Примеры в репозитории не описывают реальную кампанию какого-либо пользователя.

## Ежедневный операторский поток

Для ежедневной работы не нужен большой `go`: четыре независимые операции дают
один `scan ID`, сохраняют выборку и передают точный утверждённый пакет кликеру.

~~~bash
# 1. Поиск. Возвращает scan ID, охват по источникам и команду продолжения.
.venv/bin/python -m applypilot scan --track default \
  --sources hh.ru,career.habr.com --days 3 --limit 100 \
  --request-budget 120 --details-limit 80 --json

# 2. Разбор топа конкретного скана: описание, зарплата, резюме,
#    причины допуска/отсева и явные hard blockers.
.venv/bin/python -m applypilot review --track default \
  --scan SCAN_ID --view review --limit 100 --json

# Массовое решение по конкретным ID (ID можно перечислять через запятую).
.venv/bin/python -m applypilot review --track default --scan SCAN_ID \
  --approve 12345,12346,12347 --skip 12348 --manual 12349 \
  --reason "Проверены обязанности, удалёнка и требования" --json

# 3. Dry-run и затем точная отправка только одобренного пакета этого скана.
.venv/bin/python -m applypilot apply --track default --scan SCAN_ID \
  --limit 20 --dry-run --json
.venv/bin/python -m applypilot apply --track default --scan SCAN_ID \
  --limit 20 --run --visible --json

# 4. Статус очереди, scan и входящих; чтение переписки остаётся отдельной командой.
.venv/bin/python -m applypilot status --track default --scan SCAN_ID --json
.venv/bin/python -m applypilot sources inbox-status --track default --json
~~~

`review` разделяет скор релевантности и обязательные требования. Точное
одобрение оператора снимает устаревший title/score/window gate, поэтому
проверенная Python/Go/Fullstack/AI-first вакансия не теряется из-за узкого
совпадения слов. Оно не обходит подтверждённое противоречие профилю
(например, обязательный язык ниже подтверждённого уровня), уже использованный
канал, `UNKNOWN` или внешний ATS. Каждое заблокированное ID получает причину.

`apply` не запускает новый поиск. Повторный запуск с тем же `--scan`/`--id`
продолжает неотработанные ID: подтверждённые и `UNKNOWN` исключаются, а
`UNKNOWN` не переотправляется без независимой сверки.

## Короткие проходы и продолжение

`go` сначала обрабатывает проверенную READY-очередь текущего источника, затем
выполняет ограниченный поиск, отбор и отправку и переходит к следующей площадке.
По умолчанию используются `--pass-request-budget 40`, `--pass-details-limit 20`
и `--pass-seconds 240`. Временной лимит мягкий: проверяется между операциями,
а не обрывает отправку посередине. Эти бюджеты не означают полного охвата поиска.

~~~bash
# Продолжить сохранённую кампанию с прежней целью и журналом подтверждений
.venv/bin/python -m applypilot go --campaign-id CAMPAIGN_ID --visible

# Только существующая проверенная очередь, без нового сканирования
.venv/bin/python -m applypilot go --track default --queue-only --visible

# Посмотреть причины задержек и доступный охват проверки входящих
.venv/bin/python -m applypilot queue actions --track default --json
.venv/bin/python -m applypilot sources inbox-status --track default --json
~~~

При продолжении неоднозначные прерванные отправки попадают в UNKNOWN и требуют
сверки. CAPTCHA и потеря сессии останавливают соответствующий источник;
ошибка общей БД останавливает кампанию. Личные настройки, реальные ID кампаний
и результаты работы сохраняйте только в `private/`.

`inbox-status` показывает сохранённые сведения и способ проверки, самостоятельно
переписки не читает и ответы не отправляет. `not_checked` означает неизвестный
результат проверки. Live-проверка новых сценариев выполняется отдельно от
офлайн-тестов на синтетических данных.

## Источники

| Источник | Поиск | Действия отклика |
|---|---|---|
| HH.ru | Запросы, регионы, страницы и детали | Существующий браузерный runner; батчи до 100, кампания продолжается до цели |
| Habr Career | Каталог и form-audit | Проверенная простая нативная форма; сложные анкеты уходят в ручной разбор |
| Getmatch | Sitemap, Telegram-ссылки, карточки | Callback при актуальной кнопке + подтверждённая web-форма: POST `/api/offers/<id>/apply` и точная запись в `/applications` |
| GeekJob | Каталог и проверка формы | Подтверждённый native submit: POST `/json/respond/vacancy` 200 + точная вакансия в `/responses` |
| Яндекс Карьера | Вакансии и анкеты | Проверенный native survey: submit + success page + applied state |
| DreamJob | Каталог компаний | Зеркало HH; второй отклик запрещён |

Таблица описывает возможности кода. Поддержка одиночного действия не означает, что пакетный процесс принят для любой учётной записи или вакансии. Для массового запуска требуются полный охват, действующая сессия, готовая очередь и подтверждение результата каждой отправки. Детали в [Multi-Source](docs/MULTISOURCE.md).

## Границы live-операций

Поиск, скрининг, аудит форм и планирование не отправляют заявки. Массовый live-запуск включается явно; он использует раздельные лимиты площадок, точный профиль отклика и общую защиту от повторов. Статус `UNKNOWN` блокирует повтор до независимого подтверждения источника.

Не подставляйте неизвестные личные данные в анкеты и не рассматривайте нажатие кнопки как доказательство успешной отправки.

## Веб-панель и документация

~~~bash
./run.sh
~~~

Локальная панель использует те же профили и SQLite, что и CLI.

- [Быстрый старт](docs/QUICKSTART.md)
- [Multi-Source](docs/MULTISOURCE.md)
- [Аккаунты и сессии](docs/ACCOUNTS.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Сборка Debian-пакета](packaging/README.md)

## Проверки

Проверки и сборки выполняются вручную локально. Автоматический CI/CD отключён; конфигурации pipeline не добавляются в репозиторий.

~~~bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/check_public.py
.venv/bin/python scripts/check_public.py --audit-history-privacy
.venv/bin/python scripts/offline_multisource_smoke.py
git diff --check
~~~

Примечание: исправление текущей документации не удаляет ранее опубликованные данные из истории Git.

## Участники

Спасибо участникам, чьи изменения вошли в основной код проекта:

- [@Vova4o](https://github.com/Vova4o) — контур сопроводительных писем на подтверждённых фактах и улучшения надёжности откликов ([PR #1](https://github.com/nssanta/ApplyPilot/pull/1)).
- [@artemius125](https://github.com/artemius125) — LLM-скрининг вакансий, локальная веб-админка, треки, watcher/packaging и основа hardening-прохода ([PR #3](https://github.com/nssanta/ApplyPilot/pull/3); заменил PR #2).
