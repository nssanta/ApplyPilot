# Архитектура ApplyPilot

Этот документ описывает текущую архитектуру ApplyPilot после интеграции web-admin/LLM-слоя. Источник истины — код в `src/applypilot/`; веб-панель не является отдельной реализацией бизнес-логики, а оркестрирует те же CLI-команды и приватные артефакты.

## Базовые принципы

1. **CLI независим от UI.** Обычные команды не импортируют `admin.py`; веб-панель загружается только для `applypilot admin`.
2. **Поиск не равен отклику.** `scan`, `plan`, `review`, `screen`, `inspect`, `sync` не отправляют отклик.
3. **Реальная отправка всегда явная.** Нужен `apply --run` или подтверждённое действие в локальном UI.
4. **Снимки поиска неизменяемы как вход выполнения.** Новый scan создаёт новый JSON-файл; неуспешный проход не затирает последний успешный.
5. **SQLite — источник истины для истории отправок.** JSON-снимки отвечают за входные данные; журнал SQLite — за dedupe, попытки и неоднозначные исходы.
6. **Приватные данные не входят в Git.** Профили, cookies/storage state, ключи, отчёты, SQLite и snapshots живут в `private/`.
7. **Неоднозначность не означает повтор.** После потенциальной отправки неизвестный результат становится `unknown` и блокирует автоматический повтор до подтверждения.
8. **Треки независимы от UI.** Их конфигурация вынесена в `tracks.py` и используется CLI/watch/admin.

## Модули

| Модуль | Ответственность |
| --- | --- |
| `cli.py` | argparse, оркестрация команд, scan/plan/apply/sync/screen, dry-run |
| `config.py` | пути, TOML-конфиги, валидация search/profile, allowlist профессионального контекста |
| `presets.py` | публичные поисковые пресеты |
| `parser.py` | публичный поиск HH, нормализация, enrichment, snapshots, `first_seen` |
| `scoring.py` | чистый детерминированный скоринг и фильтры |
| `screen.py` | opt-in LLM-скрининг FIT/MAYBE/SKIP/ERROR |
| `tracks.py` | независимый реестр поисковых треков |
| `admin.py` | localhost UI, API, запуск CLI subprocess, очередь/статистика/operator-state |
| `storage.py` | SQLite-журнал, dedupe, reservations, runs, negotiations, recovery |
| `session.py` | login/check приватной HH-сессии |
| `inspection.py` | read-only Playwright inspect вакансий и названий резюме |
| `autoapply.py` | браузерная отправка одного отклика и классификация результата |
| `negotiations.py` | read-only синхронизация статусов откликов HH |
| `cover_letters.py` | выбор режима off/template/llm и строгий локальный шаблон |
| `letters.py` | генератор индивидуального письма через OpenAI-compatible API |
| `llm.py` | совместимый OpenRouter-путь писем и bounded rerank |
| `pacing.py` | интервалы между реальными действиями |
| `balance.py` | чтение баланса aitunnel |
| `analytics.py` | агрегаты локального журнала |
| `review.py` | локальный HTML-review |
| `quality.py` | офлайн benchmark |
| `templates.py` | создание публичных search-шаблонов |

## Основной цикл поиска

```text
search.toml / preset
        │
        ▼
      scan
        │
        ├─ запросы × регионы × страницы
        ├─ request budget
        ├─ relevance / newest / balanced
        ├─ dedupe vacancy_id
        ├─ bounded enrichment
        └─ first_seen
        │
        ▼
immutable JSON snapshot
        │
        ├──────────────► plan / review
        │
        └──────────────► screen
```

### Request budget

HTTP-бюджет списывается непосредственно перед каждым поисковым запросом, включая retry и переходы по HH-редиректам. Один общий budget делится между search groups и выбранными режимами сортировки.

`details_limit` — отдельный лимит enrichment и не входит в search request budget.

### Сортировка

- `relevance`: весь budget на релевантность;
- `newest`: весь budget на `publication_time`;
- `balanced`: половина budget на свежесть, половина на релевантность.

CLI по умолчанию сохраняет `relevance`. UI явно выбирает `balanced`; watcher — `newest`.

### Snapshot

Snapshot сохраняет весь найденный набор, даже если отдельный detail request завершился ошибкой. Enrichment дополняет строку, но не решает, останется ли vacancy discoverable.

Реестр `seen.json` добавляет:

- `first_seen`;
- признак новой вакансии для UI.

## Цикл LLM-скрининга

```text
snapshot
   │
   ▼
mechanical score/filter
   │
   ▼
screen.py
   │
   ├─ allowlisted candidate context
   ├─ track rubric + private criteria
   ├─ prompt-injection boundary
   ├─ cache
   ├─ retry/backoff
   └─ spend ledger
   │
   ▼
screen report
   │
   ├─ FIT
   ├─ MAYBE
   ├─ SKIP
   └─ ERROR
   │
   ▼
accepted snapshot (опционально)
```

`ERROR` означает техническую ошибку, а не негативный вердикт. Такая строка не попадает в автоматический action-путь и может быть повторена через `--retry-errors`.

Cache key включает:

- vacancy ID/name/description/experience;
- allowlisted candidate context;
- model;
- track;
- итоговую rubric;
- prompt version.

Поэтому изменение приватных критериев инвалидирует старый verdict cache.

## Треки

`private/config/tracks.toml` связывает одно поисковое направление с:

- `key`;
- `label`;
- `type = ai | infra | general`;
- profile;
- search config;
- HH resume label;
- screen report;
- accepted snapshot.

`tracks.py` не зависит от web-admin.

Поведение загрузки fail-closed:

- если файла нет — создаётся нейтральный default track;
- если существующий TOML повреждён — файл не заменяется;
- пути разрешены только внутри `private/`;
- duplicate key и некорректный type считаются ошибкой.

## Web-admin

`admin.py` — локальный HTTP-сервер на stdlib `http.server`.

### Граница безопасности

Сервер принимает только loopback bind. Для запросов проверяются:

- `Host`;
- `Origin` для POST;
- per-process CSRF token;
- `Content-Type: application/json`;
- максимальный размер POST;
- security response headers.

Для LLM endpoint:

- внешний адрес должен быть HTTPS;
- HTTP разрешён только для loopback;
- URL с userinfo/query/fragment отклоняется.

API-ключ хранится локально и никогда не возвращается JSON-ответом настроек. Приоритет web-admin: runtime `AITUNNEL_API_KEY` → сохранённый `api_key` → `private/config/aitunnel.key`; прямой CLI использует env → key-file.

### Оркестрация задач

Admin не реализует scan/screen/apply второй раз. Он собирает аргументы и запускает:

```text
python -m applypilot ...
```

через `JobRunner`.

Ограничения:

- одновременно одна admin-задача;
- stdout/stderr стримятся в UI;
- завершённые jobs пишутся в `jobs.jsonl`;
- на POSIX subprocess стартует в новой process group;
- Stop посылает SIGTERM всей группе, чтобы shell pipeline не оставлял Python-потомков.

### Operator state

Небольшие JSON-файлы отражают ручные действия пользователя:

- `viewed.json`;
- `bad.json`;
- `manual-applied.json`;
- `letters-sent.json`.

Они влияют на UI и apply queue, но не удаляют строки из исходных scan/screen артефактов.

## Цикл отклика

```text
snapshot / accepted snapshot
          │
          ▼
       select
          │
          ▼
      preflight
          │
          ├─ HH URL
          ├─ resume selected
          └─ required cover letter
          │
          ▼
SQLite atomic reserve
          │
          ▼
       submitting
          │
          ▼
 Playwright apply_one
          │
          ├─ success
          ├─ already_applied
          ├─ needs_manual
          ├─ failed_before_submit
          └─ unknown
          │
          ▼
SQLite attempts/events/run_items
```

### Защита от дублей

`Store.blocked_ids(account)` объединяет:

1. локальные blocking statuses;
2. vacancy IDs, найденные в HH negotiations.

Дополнительно CLI/UI исключают manual operator state.

Dedupe изолирован по `account`.

### Reservations

Перед потенциальной отправкой SQLite выполняет `BEGIN IMMEDIATE` и создаёт reservation. Это защищает от повторной отправки одного vacancy ID при конкурирующих действиях.

После конечного результата временная reservation удаляется.

### Прерывание

Если процесс оборвался в состоянии `submitting`, следующий реальный apply или sync под run lock переводит такую попытку в `unknown`.

Она не становится автоматически retryable.

## Синхронизация HH negotiations

`sync` использует сохранённую HH-сессию, но не открывает чаты и не читает сообщения.

Полный sync заменяет negotiation-status rows только текущего account. Ограниченный `--pages N` считается truncated и только upsert-ит реально полученные страницы.

Свежая negotiation запись может автоматически подтвердить локальный `unknown` как `success`, если:

- vacancy ID совпадает;
- HH status известен;
- `fetched_at` не старше ambiguous attempt.

## Сопроводительные письма

### Контур apply

`cover_letters.py` определяет режим:

```text
off | template | llm
```

`template` полностью локален.

Для `llm`:

- `provider = aitunnel` → `letters.py`;
- `provider = openrouter` → совместимый путь `llm.py`.

При aitunnel web-preview и реальный apply используют один генератор `letters.generate_letter`.

Allowlist профессионального контекста валидируется в `config.professional_context`.

Поддерживаемый внешний resume text:

- UTF-8 `.txt`;
- UTF-8 `.md`.

PDF/DOCX автоматически не парсятся.

## Watcher

`packaging/applypilot-watch.sh` загружает тот же roster из `tracks.py`.

Для каждого трека:

```text
scan --days 1 --sort-mode newest
        │
        ▼
new snapshot
        │
        ▼
screen
        │
        ▼
report + accepted snapshot + watch.log
```

Watcher никогда не вызывает `apply`.

systemd user timer находится в:

- `packaging/applypilot-watch.service`;
- `packaging/applypilot-watch.timer`.

При установке из web-admin unit переписывается на фактический checkout.

## Хранилища

### JSON

| Файл | Назначение |
| --- | --- |
| `snapshots/hh_vacancies_*.json` | результаты scan |
| `snapshots/last_successful.json` | указатель на последний успешный scan |
| `seen.json` | первый момент обнаружения vacancy ID |
| `screen-*.json` | LLM verdict report |
| `accepted-*.json` | набор для review/apply |
| `viewed.json` | открытые пользователем вакансии |
| `bad.json` | вручную отклонённые |
| `manual-applied.json` | вручную отмеченные отклики |
| `letters-sent.json` | ручной учёт отправленных писем |
| `admin-settings.json` | локальные LLM-настройки web-admin |
| `jobs.jsonl` | журнал admin subprocess |
| `spend.jsonl` | расходы LLM |
| `watch.log` | watcher |

### SQLite

`applypilot.sqlite3` содержит:

- runs;
- run_items;
- attempts;
- events;
- reservations;
- negotiation_statuses;
- sync_snapshots;
- информацию об импортированной legacy-истории.

## Границы сети

| Операция | Сеть | Авторизация HH | Может отправить отклик |
| --- | --- | --- | --- |
| `plan` | нет | нет | нет |
| `review` | нет | нет | нет |
| `analytics` | нет | нет | нет |
| `scan` | публичный HH | нет | нет |
| `screen` | LLM provider | нет | нет |
| `letter preview` | зависит от режима | нет | нет |
| `inspect` | HH через Playwright | да | нет |
| `sync` | HH | да | нет |
| `apply --dry-run` | нет | нет | нет |
| `apply --run` | HH через Playwright | да | **да** |

## Проверки архитектурных границ

В тестах отдельно проверяются:

- budget/search diagnostics;
- сохранение вакансий при enrichment failure;
- account isolation;
- recovery interrupted runs;
- ambiguity/unknown handling;
- CSRF/Origin/Host web-admin;
- XSS-safe rendering;
- tracks fail-closed;
- process-group stop;
- provider URL validation;
- CLI independence from admin;
- cover-letter grounding/cache;
- LLM screening/cache/retry;
- watcher/track behavior.

Перед публикацией дополнительно запускаются Ruff, build, public leak-check и `git diff --check`.
