# ApplyPilot

ApplyPilot — локальный инструмент для поиска вакансий на HH.ru, отбора кандидатов, LLM-скрининга и контролируемых откликов. Проект рассчитан на работу из терминала и через локальную веб-панель. Личные профили, сессии HH, ключи, снимки вакансий и журналы хранятся только в `private/` и не входят в Git.

## Что умеет

- публичный поиск вакансий без авторизации HH;
- несколько поисковых направлений через треки;
- объяснимый офлайн-скоринг и HTML-review;
- LLM-скрининг `FIT / MAYBE / SKIP / ERROR`;
- ручной запуск campaign с постоянной SQLite-очередью и историей снимков;
- раздельные для каждого трека решения FIT/MAYBE/SKIP/APPROVED/DEFERRED;
- локальная веб-панель для поиска, просмотра, скрининга, треков, статистики и очереди откликов;
- сохраняемые черновики писем и ручных анкет;
- ограниченное read-only чтение HH-переписок через официальный OAuth API;
- безопасный `dry-run` и явный `apply --run`;
- защита от повторных откликов через SQLite-журнал и синхронизацию HH negotiations;
- сборка wheel/sdist и экспериментального Debian-пакета.

CLI остаётся самостоятельным интерфейсом. Веб-панель — опциональная оболочка над теми же командами и приватными артефактами.

Публичный сценарий установки и проверки без папки `private/`: [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Multi-Source (этап 2)

Единый конвейер для HH, Habr Career, Getmatch, GeekJob, Карьеры Яндекса и DreamJob.
Подробности и матрица готовности: [docs/MULTISOURCE.md](docs/MULTISOURCE.md).

```bash
# Явные метки аккаунтов; cookies сюда не пишутся.
cp examples/sources.toml private/config/sources.toml

# Публичная офлайн-проверка на синтетических fixtures (работает без private/).
.venv/bin/python -m applypilot sources scan --track default \
  --fixture-dir tests/fixtures/public --json

# Живой поиск, затем review и dry-run по конкретному источнику.
.venv/bin/python -m applypilot sources scan --track default --limit 100
.venv/bin/python -m applypilot sources status --track default
.venv/bin/python -m applypilot queue list --track default --source getmatch.ru --view review
.venv/bin/python -m applypilot sources plan --track default --source getmatch.ru
.venv/bin/python -m applypilot sources dispatch-plan --track default --limit 100 --json
.venv/bin/python -m applypilot sources approve-batch --track default --source getmatch.ru \
  --id VACANCY_ID --state APPROVED --reason 'подходит по стеку'
.venv/bin/python -m applypilot sources dispatch-apply --track default --limit 100 --json

# Вход/проверка сессии источника (выполняется вручную, не фоновой задачей).
.venv/bin/python -m applypilot sources login --source career.habr.com
.venv/bin/python -m applypilot sources check --source career.habr.com
.venv/bin/python -m applypilot sources form-audit --track default \
  --source career.habr.com --view review --limit 100 --json
```

У Habr Career есть проверенный native-one-click с отдельным CLI-флагом:
`dispatch-apply --allow-live --enable-habr`, контролируемым интервалом и
проверкой ответа в истории. `form-audit` только читает страницы и помогает
агенту оценить пакет форм без откликов. GeekJob, Getmatch, Яндекс пока имеют
`needs_live_validation`. DreamJob — зеркало HH без второго отклика.
По умолчанию `dispatch-apply` всегда dry-run.

Сохранённые сообщения `@g_jobbot` связываются с очередью без повторного логина.
Getmatch умеет отдельно читать подтверждённые прошлые отклики и проводить
строго одиночную проверку Telegram-кнопки без отправки:

```bash
.venv/bin/python -m applypilot accounts telegram-sync --limit 500
.venv/bin/python -m applypilot accounts telegram-import --track default --limit 500
.venv/bin/python -m applypilot accounts telegram-reconcile --track default --limit 500
.venv/bin/python -m applypilot accounts telegram-pilot --track default \
  --id VACANCY_ID --probe-live --json
```

Нативный callback Getmatch успешно проверен одной реальной отправкой
с подтверждением официального бота и сохранением в SQLite. Для
последовательных отправок есть защищённый `GetmatchCallbackDriver`,
подключённый к `dispatch-apply --enable-getmatch`; требуется отдельный
`--allow-live`, `APPLYPILOT_TELEGRAM_LIVE=1`, подтверждённый профиль
и предварительно APPROVED вакансии. Пакетная устойчивость проверена
тестами, но большой живой объём ещё не проверялся. Чаты HR, WebView
и дополнительные анкеты не считаются автоматически завершёнными.

Нативный Habr-кликер проверен одним реальным откликом с HTTP 200, сообщением
«Отклик отправлен» и точным совпадением ID в /responses. Для него доступен
отдельный CLI live opt-in; GeekJob, Яндекс и Getmatch пока остаются
`needs_live_validation`, без реальной отправки.

## Аккаунты Telegram и HH OAuth (этап 3)

Локальные клиенты для реальных аккаунтов добавлены отдельно и не заменяют
существующий HH-кликер:

```bash
pip install -e '.[telegram]'
applypilot accounts telegram-setup          # API_ID/API_HASH, скрытый ввод
applypilot accounts telegram-login --phone +<номер>
applypilot accounts telegram-sync --limit 50 --json
applypilot accounts hh-oauth-setup          # client_id/secret, скрытый ввод
applypilot accounts hh-oauth-authorize
applypilot accounts hh-oauth-exchange       # вставить полный redirect URL
applypilot accounts hh-oauth-status --online
applypilot accounts hh-oauth-negotiations --limit 20 --json
applypilot accounts hh-oauth-messages --negotiation-id <id> --json
```

Секреты хранятся только в `private/data/**` с правами `0600`, каталоги
аккаунтов — `0700`; значения не выводятся в CLI/JSON/UI и не попадают в Git.
OAuth state обязателен на каждом обмене, живёт 10 минут и удаляется после
успешного обмена. Отправка через Telegram callback и `POST /negotiations` HH
имеет статус `needs_live_validation` и по умолчанию заблокирована.
Подробности, статусы и чек-лист живой проверки:
[`docs/ACCOUNTS.md`](docs/ACCOUNTS.md).

## Быстрый старт

### Веб-панель

Из корня репозитория:

```bash
./run.sh
```

По умолчанию откроется <http://127.0.0.1:8765>. Другой порт:

```bash
./run.sh 9000
```

Эквивалентная команда:

```bash
.venv/bin/python -m applypilot admin --open
```

Панель привязана только к loopback-интерфейсу. Для изменяющих запросов используются CSRF-токен, проверки `Host`/`Origin` и JSON-only POST. API-ключ не возвращается браузеру обратно: UI получает только признак, задан ли ключ.

### Терминал

```bash
.venv/bin/python -m applypilot --help
.venv/bin/python -m applypilot doctor
.venv/bin/python -m applypilot scan --preset ai-agents-llmops --area 113 --remote --days 7
```

Для постоянной очереди используйте **`default`** из `private/config/tracks.toml` (или другой явно выбранный трек). Пример выше — основная команда полного AI-only цикла; здесь команды просмотра и статусов:

```bash
.venv/bin/python -m applypilot queue list --track default --view top --limit 100
.venv/bin/python -m applypilot queue list --track default --view needs_letter --limit 100
.venv/bin/python -m applypilot queue list --track default --view needs_form --limit 100
.venv/bin/python -m applypilot queue list --track default --view unknown --limit 100
.venv/bin/python -m applypilot queue history --track default --limit 100
```

`--limit` — верхняя граница партии, не обязанность добирать её нерелевантными вакансиями. Активный профиль bulk одобрен владельцем; массовая отправка разрешена по команде пользователя, без согласования каждой вакансии. Привязка к правильному резюме, учёт успешных HH-откликов и блокировка повторной отправки `UNKNOWN` сохраняются как технические гарантии.

Глобальные параметры `--data-dir`, `--profile`, `--search` указываются **до** подкоманды:

```bash
.venv/bin/python -m applypilot \
  --profile private/config/profile.toml \
  --search private/config/search.toml \
  scan --preset ai-agents-llmops
```

## Установка из исходников

Требуется Python 3.12+.

```bash
export PIP_CACHE_DIR="$PWD/private/pip-cache"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/private/browsers"

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install -r requirements-browser.lock
.venv/bin/pip install -e ".[dev,browser]"
.venv/bin/python -m playwright install chromium
```

Для воспроизводимой установки зависимости зафиксированы в:

- `requirements.lock`;
- `requirements-dev.lock`;
- `requirements-browser.lock`.

Создайте приватные конфиги:

```bash
mkdir -p private/config
cp examples/profile.example.toml private/config/profile.toml
cp examples/search.example.toml private/config/search.toml
```

Перед реальными откликами профиль должен быть проверен вручную и содержать `reviewed = true`.

## Приватная структура данных

Типовая локальная структура:

```text
private/
├── config/
│   ├── profile.toml
│   ├── search.toml
│   ├── tracks.toml
│   └── aitunnel.key              # необязательно
├── data/
│   ├── applypilot.sqlite3
│   ├── hh_session.json
│   ├── admin-settings.json
│   ├── viewed.json
│   ├── bad.json
│   ├── manual-applied.json
│   ├── spend.jsonl
│   └── snapshots/
├── reports/
├── browsers/
└── archive/
```

`private/config/tracks.toml` создаётся автоматически при первом запуске веб-панели, если файла ещё нет. Если файл существует, но повреждён, ApplyPilot **не перезаписывает его дефолтом**: загрузка завершается ошибкой, а исходный файл остаётся на месте.

Пути треков разрешены только внутри `private/`.

## Поиск вакансий

`scan` читает публичную выдачу HH.ru и `HH-Lux-InitialState`; OAuth для поиска не требуется.

Пример:

```bash
.venv/bin/python -m applypilot scan \
  --preset ai-agents-llmops \
  --area 113 \
  --remote \
  --days 7 \
  --pages 1 \
  --request-budget 100 \
  --details-limit 100 \
  --sort-mode balanced
```

Встроенные пресеты:

- `ai-llm`;
- `ai-agents-llmops`;
- `ml-engineering`;
- `python-backend`;
- `go-backend`;
- `software-general`.

`ai-agents-llmops` ориентирован на engineering-роли вокруг AI Agents, LLM, RAG, LLMOps и AI Platform и отсеивает очевидные QA/security/sales/training роли.

### Режим сортировки

`--sort-mode`:

- `relevance` — историческое поведение CLI и значение по умолчанию;
- `newest` — поиск по дате публикации;
- `balanced` — делит общий HTTP-бюджет между свежестью и релевантностью.

Обычный UI-скан использует `balanced`; ручная кампания по умолчанию использует `newest`.

### Бюджет и снимки

`--request-budget` учитывает реальные поисковые HTTP-вызовы, включая повторы и редиректы HH. `--details-limit` отдельно ограничивает загрузку полных описаний.

Снимок сохраняет найденные вакансии даже при частичном enrichment. Для каждой вакансии хранится `first_seen`, поэтому UI отличает новую вакансию от уже виденной в прошлых сканах.

Статусы снимка:

- `ok` — проход завершён;
- `empty` — корректная пустая выдача;
- `truncated` — закончился лимит страниц/HTTP-бюджет;
- `partial` — часть сегментов завершилась ошибкой.

Неуспешный пустой проход не заменяет `last_successful.json`.

## Офлайн-отбор

`plan` не открывает браузер и не отправляет отклики:

```bash
.venv/bin/python -m applypilot plan \
  --preset ai-agents-llmops \
  --input private/data/snapshots/FILE.json \
  --limit 100 \
  --min-score 30 \
  --rescore \
  --output private/reports/plan.json
```

Скоринг остаётся детерминированным и объяснимым: результат содержит итоговый score и причины начисления баллов.

Для локального HTML-обзора:

```bash
.venv/bin/python -m applypilot review \
  --input private/data/snapshots/FILE.json \
  --preset ai-agents-llmops \
  --top 100
```

## LLM-скрининг

`screen` — отдельный opt-in этап после механического поиска/скоринга. Модель получает только allowlist профессионального контекста и данные вакансии. Текст профиля и вакансии явно трактуется как данные, а не инструкции.

```bash
export AITUNNEL_API_KEY="..."
.venv/bin/python -m applypilot screen \
  --input private/data/snapshots/FILE.json \
  --track default \
  --model gpt-5-mini \
  --accept fit+maybe \
  --output private/reports/screen-ai-core.json \
  --emit-snapshot private/data/snapshots/accepted-ai-core.json
```

Вердикты:

- `FIT` — убедительное совпадение;
- `MAYBE` — возможно подходит или данных недостаточно;
- `SKIP` — явное существенное несоответствие;
- `ERROR` — транспортная/парсерная ошибка; такой результат никогда не считается принятым.

Кэш учитывает вакансию, кандидата, модель, трек, рубрику и версию промпта. Изменение критериев инвалидирует старый кэш. Ошибки можно повторять через `--retry-errors`.

По умолчанию внешний endpoint — aitunnel. Веб-настройка `base_url` реально передаётся subprocess-команде `screen`; удалённый endpoint должен использовать HTTPS. HTTP разрешён только для loopback.

## Веб-панель

Веб-панель находится в `src/applypilot/admin.py` и не импортируется обычными CLI-командами.

Разделы:

### Обзор

Показывает:

- наличие HH-сессии;
- последний sync;
- число обработанных/заблокированных вакансий;
- статистику FIT/MAYBE/SKIP/ERROR по трекам;
- новые и свежие вакансии;
- текущую фоновую задачу;
- баланс провайдера при доступном ключе.

### Вакансии

Для каждого трека отображаются:

- FIT/MAYBE/SKIP/ERROR;
- score и причина;
- зарплата и опыт;
- дата первого обнаружения;
- статус HH/локального журнала;
- выбранное резюме;
- дубликаты/repost;
- состояния «просмотрено», «плохая», «отклик отправлен», «письмо отправлено».

Открытие вакансии из UI помечает её просмотренной; просмотренные/плохие/уже обработанные вакансии не попадают в очередь отклика.

### Отклики

Панель строит точную очередь перед запуском. Доступны режимы:

- все принятые;
- только `FIT`;
- только вручную отмеченные.

Реальная отправка требует одновременно:

1. `reviewed = true` в профиле трека;
2. явного подтверждения в UI;
3. отдельного запуска apply-процесса.

Вкладка **Очередь** показывает ту же SQLite-очередь кампаний, что и CLI, включая историю запусков, причины оценки, проблемы и состояние отклика. Решение владельца привязано к треку; фактический статус попытки и решение владельца хранятся отдельно.

### Письма и анкеты

Черновик письма создаётся шаблонным или подключённым LLM-режимом, сохраняется в SQLite, может быть отредактирован и одобрен. Одобренное письмо попадёт в apply только вместе с одобренной вакансией и отдельным явным запуском. Отправка письма работодателю отдельно от HH-отклика не реализована.

Если HH показывает вопросы, ApplyPilot сохраняет задачу `form` со статусом `NEEDS_MANUAL`. Захват текста/вариантов вопросов и их безопасная автозапись в этой версии не поддерживаются; задача не превращается в обычный отклик.

```bash
.venv/bin/python -m applypilot workflow list --track default
.venv/bin/python -m applypilot workflow letter-preview --track default --id VACANCY_ID
.venv/bin/python -m applypilot workflow edit --track default --id TASK_ID --text 'Проверенный текст письма'
.venv/bin/python -m applypilot workflow approve --track default --id TASK_ID
.venv/bin/python -m applypilot queue list --track default --view needs_form
```

### Переписки HH

`inbox sync` вручную читает ограниченную страницу чатов через официальный OAuth API. Нужен `HH_ACCESS_TOKEN`; cookie Playwright не подменяет OAuth. Сохраняются тексты и метаданные отправителя, без URL вложений. Не вызываются методы отправки сообщения или отметки прочитанным. Роль «рекрутер/бот» берётся только из метаданных HH; черновики ответов и отправка сообщений пока не реализованы.

```bash
export HH_ACCESS_TOKEN='…'
.venv/bin/python -m applypilot inbox sync --max-chats 20 --messages-per-chat 20
.venv/bin/python -m applypilot inbox list --limit 100
```

### Резюме и треки

Трек объединяет:

- ключ и название;
- тип рубрики `ai / infra / general`;
- приватный профиль;
- поисковый конфиг;
- HH-резюме;
- путь к screen-report;
- путь к accepted snapshot.

Треки можно создавать, редактировать и удалять. UI умеет читать доступные названия резюме из HH в read-only режиме.

### Статистика

Показывает вердикты по трекам, расходы LLM, последний известный баланс и дневную историю из `spend.jsonl`.

### Лог

Фоновые команды запускаются как subprocess и стримят вывод в UI. Одновременно выполняется не более одной admin-задачи. На POSIX каждая задача запускается в отдельной process group, поэтому «Стоп» завершает весь pipeline, а не только shell-родителя.

### Настройки

Можно задать:

- модель;
- OpenAI-compatible `base_url`;
- API-ключ;
- общие ограничения кандидата;
- зарплатное ожидание;
- отдельные критерии скрининга по каждому треку.

Ключ из веб-панели хранится локально в `private/data/admin-settings.json` с попыткой выставить права `0600`. Приоритет для web-admin: runtime `AITUNNEL_API_KEY` → сохранённый ключ панели → `private/config/aitunnel.key`. Для прямого CLI: `AITUNNEL_API_KEY` → `private/config/aitunnel.key`.

## Сессия HH и read-only inspect

Публичному `scan` cookies не нужны.

Для `inspect`, `sync` и реального `apply` используется установленный Google Chrome
с постоянным приватным профилем `private/data/hh_chrome_profile/`. Куки, localStorage
и браузерная идентичность живут в одном профиле и обновляются сами. Экспорт
`private/data/hh_session.json` остаётся для read-only `sync`/`inspect`.

```bash
.venv/bin/python -m applypilot login
.venv/bin/python -m applypilot session check
```

### Режимы браузера

Реальный `apply` по умолчанию идёт в **visible**-режиме: открывается обычное окно
Chrome, и при CAPTCHA его можно пройти руками. Процесс ждёт до
`[browser] manual_captcha_timeout_seconds` (по умолчанию 300 с), не повторяя
финальный клик.

```bash
# видимое окно, ручная CAPTCHA (по умолчанию)
.venv/bin/python -m applypilot apply --input FILE.json --run --visible

# без окна: быстрее, но CAPTCHA пройти нельзя
.venv/bin/python -m applypilot apply --input FILE.json --run --headless

# то же для постоянной AI-only очереди (100/run, 300/day в bulk-профиле)
APPLYPILOT_INTERACTIVE=1 .venv/bin/python -m applypilot --profile private/config/profile.toml queue apply --track default --limit 100 --run --visible
.venv/bin/python -m applypilot --profile private/config/profile.toml queue apply --track default --limit 100 --run --headless
```

`headless` использует настоящий Chrome 156 с тем же UA/Client Hints, locale
`ru-RU`, зоной `Europe/Moscow` и экраном 1366×768, без `HeadlessChrome` в UA.
Если HH показывает CAPTCHA до финальной отправки, запуск останавливается с сохранением очереди; после submit исход может быть `UNKNOWN`. Перед следующей попыткой сверять HH и не повторять тот же vacancy ID с неопределённым исходом. В visible-режиме при наличии интерактивного входа или `APPLYPILOT_INTERACTIVE=1` Chrome может ждать ручного решения CAPTCHA. Переключение headless/visible выполняется отдельным запуском и не превращает `UNKNOWN` в повторяемую заявку.

Сессия сохраняется в:

```text
private/data/hh_session.json
```

Проверить доступные резюме без действий:

```bash
.venv/bin/python -m applypilot inspect --resumes
```

Проверить ограниченный список вакансий:

```bash
.venv/bin/python -m applypilot inspect \
  --input private/data/snapshots/FILE.json \
  --selected \
  --limit 100 \
  --preset ai-agents-llmops
```

`inspect` использует отдельный browser context и не вызывает `click`, `fill`, `submit` или page-evaluated JavaScript.

## Синхронизация откликов HH

`sync` читает статусы negotiations без открытия чатов и без чтения сообщений:

```bash
.venv/bin/python -m applypilot sync
```

Без `--pages` проход идёт до пустой страницы. При ограниченном числе страниц snapshot помечается `truncated` и не удаляет неизвестные записи из неполученных страниц.

Если локальный результат был `unknown`, а свежий HH negotiations подтверждает тот же vacancy ID, `sync` автоматически переводит его в `success`.

`history reconcile` остаётся ручным совместимым механизмом для явно подтверждённых статусов из JSON/CSV; это не обязательный шаг после каждого `unknown`.

## Контролируемый отклик

### Dry-run

```bash
.venv/bin/python -m applypilot apply \
  --input private/data/snapshots/accepted-ai-core.json \
  --preset ai-agents-llmops \
  --dry-run \
  --limit 100
```

Dry-run не открывает браузер и не отправляет отклики, но создаёт auditable run и фиксирует выбранный список кандидатов.

### Реальный запуск

```bash
.venv/bin/python -m applypilot apply \
  --input private/data/snapshots/accepted-ai-core.json \
  --preset ai-agents-llmops \
  --run \
  --limit 20 \
  --target-success 10
```

Перед отправкой проверяются HH URL, выбранное резюме и обязательность письма. SQLite резервирует вакансию атомарно до потенциальной отправки.

Основные состояния:

- `prepared` — подготовлено;
- `submitting` — начата потенциальная отправка;
- `success` — отправка подтверждена;
- `already_applied` — HH сообщает, что отклик уже существует;
- `needs_manual` — требуется ручное действие;
- `failed_before_submit` — ошибка произошла до потенциальной отправки;
- `unknown` — после потенциальной отправки результат нельзя доказать.

`unknown` и найденные HH negotiations блокируют автоматический повтор. После прерванного запуска оставшиеся `submitting` восстанавливаются как `unknown`.

## Сопроводительные письма

Контур реального `apply` управляется секцией `[cover_letter]`:

```toml
[cover_letter]
mode = "off"                  # off | template | llm
provider = "aitunnel"         # aitunnel | openrouter
fallback_to_template = false
```

Режимы:

- `off` — письмо не готовится;
- `template` — строгий локальный шаблон без внешнего API;
- `llm` — письмо генерирует выбранный провайдер.

Для `provider = "aitunnel"` реальный apply использует тот же `letters.py`, что и web-preview, поэтому предпросмотр и отправка идут через один генератор. `provider = "openrouter"` сохранён как совместимый путь через старый `llm.py`.

Профессиональный контекст берётся только из allowlist-полей `[professional]`. Для полного текста резюме поддерживаются UTF-8 `.txt`/`.md`; PDF/DOCX автоматически не разбираются.

Проверка письма без отправки:

```bash
.venv/bin/python -m applypilot letter preview \
  --input private/data/snapshots/FILE.json \
  --id VACANCY_ID
```

## История и дедупликация

SQLite `private/data/applypilot.sqlite3` — источник истины для:

- runs;
- run items;
- attempts;
- events;
- reservations;
- HH negotiation statuses;
- campaign runs и сохранённых observations;
- vacancy cards и scores/review decisions по каждому треку;
- application/conversation state;
- задач писем/форм и прочитанных inbox сообщений;
- sync snapshots.
- импорта старой истории.

Дедупликация ведётся по `account + vacancy_id`. Кроме локальных завершённых/неопределённых попыток, блокируются vacancy ID, уже найденные в HH negotiations.

Дополнительные operator-state файлы веб-панели:

- `viewed.json`;
- `bad.json`;
- `manual-applied.json`;
- `letters-sent.json`.

Они также исключаются из action-очереди.

## Фоновый поиск

Текущий workflow работает только по явной команде. ApplyPilot не включает timer, cron или watcher. В `packaging/` остались экспериментальные legacy-файлы планировщика; они не нужны для ручной кампании и не устанавливаются запуском `./run.sh`.

## Debian-пакет

Экспериментальная сборка:

```bash
bash packaging/build-deb.sh
```

Подробности и ограничения описаны в [packaging/README.md](packaging/README.md).

## Архитектура

Техническая схема модулей, потоков данных, циклов campaign/apply/inbox, SQLite-состояний и границ безопасности находится в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Проверки перед публикацией

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/check_public.py
.venv/bin/python scripts/offline_multisource_smoke.py
.venv/bin/python -m build
git diff --check
```

`scripts/check_public.py` проверяет публичные tracked-файлы и доступную Git-историю на утечки приватных данных/секретов.

## Участники

Спасибо участникам, чьи изменения вошли в основной код проекта:

- [@Vova4o](https://github.com/Vova4o) — контур сопроводительных писем на подтверждённых фактах и улучшения надёжности откликов ([PR #1](https://github.com/nssanta/ApplyPilot/pull/1)).
- [@artemius125](https://github.com/artemius125) — LLM-скрининг вакансий, локальная веб-админка, треки, watcher/packaging и основа hardening-прохода ([PR #3](https://github.com/nssanta/ApplyPilot/pull/3); заменил PR #2).
