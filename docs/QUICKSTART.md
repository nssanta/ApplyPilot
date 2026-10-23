# ApplyPilot: публичный quickstart

Этот сценарий работает в чистом клоне **без папки `private/`**: все примеры,
тесты и offline-smoke используют синтетические fixtures из
`tests/fixtures/public/`.

## 1. Установка

```bash
git clone <repo-url> ApplyPilot
cd ApplyPilot
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
applypilot doctor
```

Для браузерных сессий добавьте `playwright`, для Telegram — `telethon`:

```bash
pip install -e '.[browser,telegram]'
python -m playwright install chromium
```

## 2. Проверка публичного кода

```bash
python -m pytest -q
ruff check src tests scripts
python scripts/check_public.py
python scripts/offline_multisource_smoke.py
```

Offline-smoke не ходит в сеть и не отправляет отклики: он сканирует пять
источников, сохраняет нормализованные записи в временную SQLite, считает
рейтинг и показывает, что live-непроверенные формы блокируются.

## 3. Локальная конфигурация

`private/` не входит в Git. Создайте её из публичных примеров:

```bash
mkdir -p private/config private/data
cp examples/profile.example.toml private/config/profile.toml
cp examples/search.example.toml private/config/search.toml
cp examples/sources.toml private/config/sources.toml
```

Отредактируйте профиль и поиск под себя. Секреты, cookies, session-файлы и
рабочая SQLite остаются только в `private/`.

Проверьте треки и источники:

```bash
applypilot config show
applypilot sources list
applypilot sources accounts --track default
```

## 4. Поиск и рейтинг

Публичный offline-пример:

```bash
applypilot sources scan --track default \
  --fixture-dir tests/fixtures/public --limit 100
applypilot queue list --track default --view top --limit 20
applypilot queue list --track default --view review --limit 20
```

Живой поиск (без отправки откликов):

```bash
applypilot sources scan --track default --limit 100
applypilot sources status --track default
```

Один заблокированный источник не останавливает остальные; ошибка видна в
`failed_sources`.

## 5. Общий диспетчер откликов

```bash
# Общий dry-run по всем источникам.
applypilot sources dispatch-plan --track default --limit 100 --json

# Пакетное одобрение до 100 ID (решение и причина обязательны).
applypilot sources approve-batch --track default --source getmatch.ru \
  --id 36887 --state APPROVED --reason 'подходит по стеку'

# По умолчанию это снова dry-run. Live требует --allow-live и проверенного
# драйвера источника; без драйвера источник пропускается с needs_live_validation.
applypilot sources dispatch-apply --track default --limit 100 --json
```

Диспетчер атомарно резервирует канал `(account, canonical channel)`: DreamJob
не создаёт второй отклик на то же HH-назначение, внешняя ATS блокируется.
Исключение драйвера переводит попытку в `UNKNOWN` и не повторяется вслепую.

## 6. Telegram Getmatch

Авторизация выполняется один раз вручную; импорт уже сохранённых сообщений
не требует повторного логина:

```bash
applypilot accounts telegram-setup
applypilot accounts telegram-login --phone +<номер>
applypilot accounts telegram-sync --limit 100 --json
applypilot accounts telegram-import --track default --json
```

`telegram-import` связывает `getmatch.ru/vacancies/<id>` из сообщений с
реальной очередью сканера. Для ещё не скачанной вакансии можно добавить
review-only placeholder: `--create-missing`. Он не попадает в live-очередь,
пока сканер не обогатит и не оценит его.

## 7. HH OAuth (read-only)

```bash
applypilot accounts hh-oauth-setup
applypilot accounts hh-oauth-authorize
applypilot accounts hh-oauth-exchange          # вставить полный redirect URL
applypilot accounts hh-oauth-status --online
applypilot accounts hh-oauth-negotiations --limit 20 --json
applypilot accounts hh-oauth-messages --negotiation-id <id> --json
```

Сообщения читаются только через `GET /negotiations/{id}/messages`. Отправка
`POST /negotiations` не включена: статус `needs_live_validation`.

## 8. Веб-панель

```bash
./run.sh
# или
applypilot admin
```

Панель показывает общий топ/review, очередь по источникам, общий dry-run,
Telegram-импорт и статус кликеров. Live-отправка из админки не включена:
она остаётся явной CLI-операцией после живой проверки формы.

## 9. Кликеры и живая проверка

`applypilot` не придумывает селекторы. Для Habr, GeekJob, Яндекса и Getmatch есть контракты `preflight`/`inspect`.
На Хабре подтверждён нативный one-click и отдельный HabrPageDriver; остальные
площадки пока имеют `apply_status=needs_live_validation`.

```bash
applypilot accounts --help
```

Для проверенного **нативного** сценария Habr Career доступны отдельный
браузерный профиль и явное разрешение запуска:

```bash
applypilot sources check --source career.habr.com
# Проверить в одной команде до 100 реальных форм по review/TOP, без POST:
applypilot sources form-audit --track ai_core --source career.habr.com \
  --view review --limit 100 --json
applypilot sources dispatch-plan --track ai_core --source career.habr.com --json

# РЕАЛЬНАЯ ОТПРАВКА: только ранее APPROVED и READY_TO_APPLY,
# с действующей сессией и независимым подтверждением в /responses.
# Запускать осознанно, не использовать для тестирования без разрешения.
applypilot sources dispatch-apply --track ai_core --source career.habr.com \
  --limit 100 --allow-live --enable-habr --visible --habr-min-interval 2
```

Habr driver поддерживает подтверждённую one-click форму; перед работой
проверяет удалёнку и наличие отклика в истории, пишет успех только по
подтверждению POST + уведомлению + точной записи в /responses. Между
отправками регулируемый интервал; при UNKNOWN оставшаяся Habr-пачка
останавливается без повторных кликов. Дополнительные обязательные вопросы,
другие формы и внешние ATS требуют ручной проверки. Сам аудит форм
не одобряет вакансии и не отправляет их.
Для GeekJob, Яндекса и Getmatch пока нет подтверждённого live-драйвера;
диспетчер пропускает их, не отправляя отклик. Для них сначала нужна
проверка реального авторизованного интерфейса.

## 10. Безопасность

- Не коммитьте `private/`, session-файлы, cookies и токены.
- `scripts/check_public.py` проверяет tracked-файлы, историю и артефакты.
- Не включайте расписания: все действия запускаются вручную.
- Реальные отклики и сообщения не отправляются без явного `--allow-live`
  и проверенного драйвера.
