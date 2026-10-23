# ApplyPilot Multi-Source

Источники вакансий подключаются независимо и используют общие схемы нормализации, скоринга, review и журналирования. Документ описывает **возможности приложения**, а не чьи-либо авторизованные аккаунты, заявки или персональные настройки.

## Матрица возможностей

| Источник | Поиск и детали | Механизм отклика | Ограничение |
|---|---|---|---|
| `hh.ru` | публичный поиск и детали | Chrome/Playwright runner | требуется пользовательская авторизация для отклика |
| `career.habr.com` | каталог и детали | native one-click с явным live opt-in | внешние ATS/анкеты отдельно |
| `getmatch.ru` | sitemap, публичные детали и Telegram-ссылки | guarded Telegram callback с явным live opt-in | только callback официального бота и принятый профиль |
| `geekjob.ru` | каталог и детали | read-only проверка native-формы и single-vacancy pilot | массовая отправка без подтверждения не активна |
| `yandex.ru/jobs` | каталог/детали | read-only form audit | submit без отдельного подтверждения не активен |
| `dreamjob.ru` | listings и зеркала | read-only HH mirror | второй отклик по зеркалу не создаётся |

Авторизация и состояние аккаунтов хранятся локально, вне Git. Записи `UNKNOWN` блокируют слепую повторную отправку.

## Общий пример

```bash
cd /path/to/ApplyPilot
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp examples/sources.toml private/config/sources.toml

# Обезличенные публичные fixtures
.venv/bin/python -m applypilot sources scan --track default \
  --fixture-dir tests/fixtures/public --json
.venv/bin/python -m applypilot sources list
.venv/bin/python -m applypilot sources dispatch-plan \
  --track default --limit 20 --json
```

Для работы с реальными вакансиями пользователь настраивает свой `private/config/`, не коммитя профили, cookies, идентификаторы аккаунтов, журналы и статусы откликов.

## Состояния провайдера

`needs_live_validation` означает, что соответствующий маршрут отправки ещё не прошёл необходимую проверку. Этот статус не препятствует поиску и предварительному чтению карточек. Callback и native-формы допускаются к отправке только через проверенные драйверы и явный live-флаг.

## Рейтинг и review lanes

Каждая запись получает `review_lane`:

- `FIT` — роль и условия подтверждены;
- `GOOD_UNKNOWN` — роль/стек прошли, но зарплата, дата или remote не подтверждены;
- `NEEDS_ENRICHMENT` — не хватает описания/деталей;
- `MANUAL_REVIEW` — требует ручного разбора;
- `SKIP` — жёсткое несоответствие;
- `APPLIED` — уже есть подтверждённый отклик.

`top` показывает `FIT` и `GOOD_UNKNOWN`, `review` — все reviewable lanes без
`SKIP`/`APPLIED`. Поэтому вакансия с неизвестной зарплатой не
исчезает из выдачи и не выдаётся за подтверждённый FIT.

## SQLite

Source-aware таблицы:

- `vacancies` — нормализованные размещения, уникальны по `(account, source, vacancy_id)`;
- `vacancy_links` — группа роли, канал, ATS/mirror и снимок duplicate-флагов;
- `source_application_attempts` — история попыток по источнику/аккаунту/каналу;
- `source_reservations` и `channel_reservations` — транзитные writer-locks.

Канал (`channel_key`) — единица защиты от дублей. Один ATS-URL блокируется для
всех площадок; нативные формы HH и Habr остаются разными каналами. DreamJob
использует `hh.ru:<id>`, поэтому повторный отклик через зеркало невозможен.

### State machine

```
None -> submitting -> success
                   -> unknown -> (reconcile with evidence) -> success
                   -> failed_before_submit -> submitting
                   -> needs_manual
success / already_applied — терминальны
```

`unknown` — карантин: автоматический retry запрещён. `reconcile` требует
непустое evidence. `recover` переводит зависшие `submitting` в `unknown` и
освобождает locks, не повторяя отправку вслепую.

```bash
.venv/bin/python -m applypilot sources recover --max-age-seconds 900
.venv/bin/python -m applypilot sources reconcile --track default \
  --source career.habr.com --id 123 --channel career.habr.com:123 \
  --evidence 'страница истории откликов показывает заявку'
.venv/bin/python -m applypilot sources attempts --source career.habr.com
```

## Безопасность live-операций

Поиск, рейтинг и dry-run не отправляют отклики. Изменяющие действия допускаются только по явно выбранному источнику и одобренной вакансии. Несоответствие анкеты или невозможность доказать результат отправки переводит попытку в `needs_manual` или `unknown`; автоматический повтор запрещён. Код не раскрывает локальные секреты и историю пользователя.
