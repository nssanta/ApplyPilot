# ApplyPilot MCP (optional, local stdio)

`applypilot.mcp_server` — необязательный адаптер к уже существующим операциям
ApplyPilot. Он не содержит второго сканера, второго кликера, второй очереди или
второго журнала откликов. `scan`, `review`, `apply`, `status` и чтение очереди
делегируются тем же CLI-командам и тому же SQLite-ledger, что использует
оператор.

Адаптер выключен, пока не установлен extra `mcp`; обычный `python -m applypilot`
не импортирует MCP SDK.

## Установка и запуск

```bash
.venv/bin/python -m pip install -e '.[mcp]'

# Диагностика без открытия сервера:
.venv/bin/python -m applypilot.mcp_server --call capabilities
```

Клиент MCP запускает локальный stdio-процесс:

```json
{
  "mcpServers": {
    "applypilot": {
      "command": "/absolute/path/to/ApplyPilot/.venv/bin/python",
      "args": ["-m", "applypilot.mcp_server"],
      "cwd": "/absolute/path/to/ApplyPilot"
    }
  }
}
```

Поддерживается только `stdio`. Сервер намеренно не открывает HTTP-порт,
не принимает `--host`, не публикует endpoint и не предназначен для туннелей или
удалённого доступа. Приватные пути, профиль, cookies и SQLite остаются на машине
оператора.

Если пакет установлен не в editable-режиме, запускайте модуль из корня
репозитория или добавьте `src` в `PYTHONPATH`. Для проверки изменённой ветки без
переустановки:

```bash
PYTHONPATH=src .venv/bin/python -m applypilot.mcp_server --call capabilities
```

По умолчанию MCP не передаёт в CLI `--profile`/`--search`: выбранный трек сам
берёт свои `profile-*.toml` и `search-*.toml`. Эти флаги MCP передаёт только
если они были явно указаны при запуске сервера.

## Инструменты

| Инструмент | Тип | Что делает |
|---|---|---|
| `capabilities` | read-only | Возможности, ограничения и текущие opt-in флаги |
| `scans` | read-only | Последние scan/apply execution ID из локального ledger |
| `status` | read-only | Существующий статус scan, очереди и доступной проверки входящих |
| `queue` | read-only | Ограниченный `review --json` по треку, view и scan ID |
| `vacancy_detail` | read-only | Сохранённые публичные поля вакансии и состояние трека |
| `letter_preview` | read-only | Только локальный шаблон; LLM-preview через MCP не вызывается |
| `application_status` | read-only | Состояние попытки; `UNKNOWN` не разрешает повтор |
| `chat_list` | read-only | Реальные HH-чаты из авторизованного интерфейса, ограниченный список |
| `chat_read` | read-only | Сообщения выбранного чата и fingerprint последнего входящего |
| `chat_dry_run` | read-only проверка | Повторно проверяет binding и журнал, не заполняет поле и не кликает |
| `scan` | запись | Запускает существующий `scan`; требует `confirm=true` |
| `review_decide` | запись | Сохраняет `APPROVE/SKIP/MANUAL/DEFER/RESET`; требует `confirm=true` |
| `apply_dry_run` | аудит без отправки | Существующий план/аудит `apply --dry-run`; сети и live-клика нет |
| `apply` | live-запись | Точные ID через существующий `apply --run`; нужны `--enable-apply` и `confirm=true` |
| `chat_send` | live-запись | Один точный ответ; нужны `--enable-chat-send` и `confirm=true` |

`scan`, `review_decide`, `apply` и `chat_send` не обходят действующие проверки:
reviewed-профиль, `live_apply` в operating policy, свежесть HH sync, выбранное
резюме, APPROVED-пакет, карантин `UNKNOWN`, привязку чата и журнал повторов.

### Обычный поток

```bash
# 1. Поиск. Это локальная запись, поэтому нужен confirm.
.venv/bin/python -m applypilot.mcp_server --call scan \
  --arguments '{"track":"ai","sources":"hh.ru","days":3,"confirm":true}'

# 2. Очередь/статус только читаются.
.venv/bin/python -m applypilot.mcp_server --call status \
  --arguments '{"track":"ai","scan":"SCAN_ID"}'
.venv/bin/python -m applypilot.mcp_server --call queue \
  --arguments '{"track":"ai","scan":"SCAN_ID","view":"review","limit":50}'

# 3. Решение оператора по точным ID.
.venv/bin/python -m applypilot.mcp_server --call review_decide \
  --arguments '{"track":"ai","scan":"SCAN_ID","ids":["hh.ru:123"],"state":"APPROVE","confirm":true}'

# 4. Сначала dry-run, затем live только для точного списка ID.
.venv/bin/python -m applypilot.mcp_server --call apply_dry_run \
  --arguments '{"track":"ai","scan":"SCAN_ID","ids":["123"]}'
.venv/bin/python -m applypilot.mcp_server --enable-apply --call apply \
  --arguments '{"track":"ai","scan":"SCAN_ID","ids":["123"],"confirm":true}'
```

Live `apply` требует непустой список точных ID. Это защищает от случайной
отправки всей очереди. Результат `apply` содержит счётчики существующего ledger,
но `submission_confirmed` не заменяет проверку receipt/истории площадки.
Таймаут процесса означает неопределённый результат: повторять команду
автоматически нельзя, сначала нужно прочитать ledger и историю источника.

## HH chat: чтение и безопасный ответ

`chat_list` открывает авторизованную страницу откликов и читает реальные ссылки
из iframe. Список ограничен виртуальным списком HH, поэтому `incomplete: true`
означает «старые чаты могут отсутствовать», а не полный охват переписки.
`chat_read` возвращает последние сообщения, явное направление
`inbound/outbound` и `latest_inbound_key` только когда последнее сообщение
стабильно, входящее и содержит текст. Направление определяется по наблюдаемым
Magritte/BEM-классам сообщения; при отсутствии однозначного положительного
маркера оно остаётся `unknown`, а ключ и отправка блокируются. Вложения и
неопределённое направление требуют ручного разбора.

```bash
.venv/bin/python -m applypilot.mcp_server --call chat_list \
  --arguments '{"limit":20}'
.venv/bin/python -m applypilot.mcp_server --call chat_read \
  --arguments '{"thread_id":"THREAD_ID"}'

# Проверка без записи и без клика:
.venv/bin/python -m applypilot.mcp_server --call chat_dry_run \
  --arguments '{"thread_id":"THREAD_ID","vacancy_id":"VACANCY_ID",
                "company":"COMPANY","inbound_key":"FINGERPRINT","reply":"Точный текст"}'
```

Перед отправкой `chat_send` заново читает чат и требует совпадения всех полей:
`thread_id`, `vacancy_id`, работодатель, `inbound_key` и точный текст. После
заполнения binding проверяется ещё раз, поэтому новое входящее сообщение
отменяет отправку. Успех подтверждается только новым исходящим сообщением с
точным текстом после предыдущей границы ID. Любая неопределённость после клика
переводит попытку в `unknown`; резерв хранится в отдельном
`private/data/chat-replies.sqlite3`, не в основной базе откликов.

```bash
# Только после согласования точного текста владельцем:
.venv/bin/python -m applypilot.mcp_server --enable-chat-send --call chat_send \
  --arguments '{"thread_id":"THREAD_ID","vacancy_id":"VACANCY_ID",
                "company":"COMPANY","inbound_key":"FINGERPRINT",
                "reply":"Точный текст","confirm":true}'
```

Состояния журнала: `submitting` (процесс упал) читается как `unknown`, `sent`,
`unknown`, `stale`. Повторная отправка того же `inbound_key` запрещена при любом
из них. `stale` означает, что контекст изменился до клика; нужно перечитать чат и
подготовить новый ответ на новый `inbound_key`.

## Дедупликация и republication guard

PR #4 предлагал отдельную таблицу `confirmed_republications` и пост-фактум
коррекцию отчёта. В текущем `main` уже есть более ранний и более строгий guard:
`Store.cross_source_attempt_conflict` сравнивает нормализованный заголовок и
полные описания (shingles, overlap >= 0.88) и учитывает
`APPLIED_CONFIRMED/ALREADY_APPLIED/UNKNOWN` до планирования. Он вызывается из
`approved_campaign_items`, `queue_plan` и диспетчера, поэтому повторная
публикация той же роли с новым ID не попадает в отправку. Отдельный
republication-ledger из PR не переносился: он дублировал бы решение и мог бы
расходиться с текущим ledger. Регрессии покрыты
`tests/test_cross_source_relevance_dedup.py`, включая repost после `UNKNOWN`.

## Безопасная проверка ветки

```bash
# Только чтение и офлайн-тесты; реальных откликов и сообщений нет.
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mcp_server.py tests/test_chat_safety.py \
  tests/test_chat_journal.py tests/test_hh_chat.py \
  tests/test_cross_source_relevance_dedup.py
PYTHONPATH=src .venv/bin/python -m applypilot.mcp_server --call capabilities
PYTHONPATH=src .venv/bin/python -m applypilot.mcp_server --call scans
```

Для живой проверки сначала используйте `status`, `queue`, `vacancy_detail`,
`chat_list`, `chat_read` и `chat_dry_run` на уже авторизованной сессии. Не
запускайте `apply` или `chat_send` без отдельного решения владельца.
