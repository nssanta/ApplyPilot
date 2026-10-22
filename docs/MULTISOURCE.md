# ApplyPilot Multi-Source

Единый конвейер поиска и откликов для HH.ru, Habr Career, Getmatch, GeekJob,
Карьеры Яндекса и DreamJob. Каждая площадка остаётся отдельным модулем, но
пользователь работает с одним рейтингом, одной очередью и одной историей.

## Что уже работает

| Источник | Поиск | Детали | Аккаунт/сессия | Отклик |
|---|---|---|---|---|
| `hh.ru` | да, legacy `run campaign` | да | HH-профиль | проверенный legacy runner |
| `career.habr.com` | да, список + пагинация | да, приоритетно | отдельный профиль | `needs_live_validation` |
| `geekjob.ru` | да, `/vacancies` + `/vacancies/2` | да, приоритетно | отдельный профиль | `needs_live_validation` |
| `getmatch.ru` | да, публичный sitemap + delta | да, JSON-LD | отдельный профиль | `needs_live_validation` |
| `yandex.ru/jobs` | да, подтверждённые remote/GenAI URL | да | отдельный профиль | `needs_live_validation` |
| `dreamjob.ru` | да, company listing | да | без логина | `mirror_only`, канал HH |

`needs_live_validation` — не ошибка поиска. Это честный статус: карточка и
описание разобраны, но авторизованная форма отклика ещё не подтверждена живой
сессией. Такие вакансии видны в review-очереди, но не попадают в реальную
отправку.

## Быстрый старт

```bash
cd /path/to/ApplyPilot
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m applypilot doctor

# 1. Явные метки аккаунтов (без паролей и cookies).
cp examples/sources.toml private/config/sources.toml

# 2. Список источников и их возможностей.
.venv/bin/python -m applypilot sources list

# 3. Офлайн-проверка на сохранённых публичных HTML-образцах.
.venv/bin/python -m applypilot sources scan --track default \
  --fixture-dir tests/fixtures/public --json

# 4. Живой поиск всех включённых источников (без откликов).
.venv/bin/python -m applypilot sources scan --track default --limit 100

# При необходимости обогатить вакансии из сохранённых сообщений Getmatch
# (публичные страницы, без нажатия кнопок @g_jobbot):
.venv/bin/python -m applypilot sources scan --track default \
  --source getmatch.ru --telegram-hints --request-budget 40 --details-limit 40

# 5. Вход и проверка конкретного источника.
.venv/bin/python -m applypilot sources login --source career.habr.com
.venv/bin/python -m applypilot sources check --source career.habr.com

# 6. Единая очередь и dry-run выбранного источника.
.venv/bin/python -m applypilot sources status --track default
.venv/bin/python -m applypilot queue list --track default --source getmatch.ru --view review
.venv/bin/python -m applypilot sources plan --track default --source getmatch.ru
```

## Source accounts

`private/config/sources.toml` содержит только метки:

```toml
[accounts]
"hh.ru" = "hh-primary"
"career.habr.com" = "habr-primary"
"getmatch.ru" = "getmatch-primary"
"geekjob.ru" = "geekjob-primary"
"yandex.ru/jobs" = "yandex-primary"
"dreamjob.ru" = "hh-primary"
```

Cookies и browser storage лежат отдельно:
`private/data/providers/<source>/<account>/profile/` и `storage_state.json`.
DreamJob не имеет собственного канала отклика: он разделяет HH identity и
канал `hh.ru:<id>`.

## Рейтинг и review lanes

Каждая запись получает `review_lane`:

- `FIT` — роль и условия подтверждены;
- `GOOD_UNKNOWN` — роль/стек прошли, но зарплата, дата или remote не подтверждены;
- `NEEDS_ENRICHMENT` — не хватает описания/деталей;
- `MANUAL_REVIEW` — требует ручного разбора;
- `SKIP` — жёсткое несоответствие;
- `APPLIED` — уже есть подтверждённый отклик.

`top` показывает `FIT` и `GOOD_UNKNOWN`, `review` — все reviewable lanes без
`SKIP`/`APPLIED`. Поэтому перспективная AI-вакансия с неизвестной зарплатой не
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

## Ограничения live-этапа

Поиск, нормализация, рейтинг, SQLite и dry-run проверены офлайн. Реальные
формы откликов Habr/Getmatch/GeekJob/Яндекса требуют отдельного прохода с
авторизованными сессиями. До этого:

- `queue apply --run --source <non-hh>` отказывает;
- admin показывает `needs_live_validation`;
- DreamJob никогда не создаёт второй канал;
- никакие расписания и фоновые watcher'ы не включаются.
