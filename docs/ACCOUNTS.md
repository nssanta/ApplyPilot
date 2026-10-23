# Локальные аккаунты: Telegram MTProto и HH OAuth

Документ описывает подключение собственных аккаунтов к локальной установке. Клиенты Telegram и HH OAuth дополняют сканеры, SQLite-очередь и браузерные драйверы. Профили, коды авторизации и реальные результаты работы не публикуются.

## Границы и статусы

| Возможность | Статус | Что это значит |
|---|---|---|
| Telegram: вход, session, история @g_jobbot, кнопки | локальный клиент | Telethon; требует собственной авторизации |
| Telegram: native callback-отклик | guarded live driver | Требует принятого профиля, одобрения вакансии и отдельного opt-in; WebView — иной сценарий |
| HH OAuth: токен, refresh, `/me`, `/negotiations`, сообщения | `documented_read` | Официальные read-методы; проверяются на живом токене |
| HH OAuth: `POST /negotiations` (отклик) | `needs_live_validation` | Точная схема тела и допуск аккаунта не подтверждены; отправка заблокирована |
| HH Playwright-кликер | без изменений | Остаётся основным путём отклика до отдельной проверки API |

## Секреты

- Вводятся только локально в терминале: `getpass` для API_HASH, client_secret,
  кода Telegram, 2FA-пароля и authorization code.
- Файлы: `private/data/telegram/credentials.json`,
  `private/data/telegram/<account>/telegram.session`,
  `private/data/hh_oauth/credentials.json`,
  `private/data/hh_oauth/<account>/token.json`.
- Права `0600`, каталоги `0700`, атомарная запись.
- Токены и хэши не возвращаются в CLI/JSON/UI, не логируются и не попадают в Git.
- `private/` уже исключён из репозитория; не копируйте рабочие сессии в другие
  worktree и не публикуйте их.

Переменные окружения (альтернатива файлам, удобно для CI/локального запуска):

```bash
export APPLYPILOT_TELEGRAM_API_ID=...
export APPLYPILOT_TELEGRAM_API_HASH=...
export APPLYPILOT_HH_CLIENT_ID=...
export APPLYPILOT_HH_CLIENT_SECRET=...
export APPLYPILOT_HH_REDIRECT_URI=http://localhost:8765/callback
```

## Getmatch — синхронизация и проверка одиночного отклика

Сначала нужно убедиться, что профиль кандидата **принят Getmatch**
и открыт для необходимых работодателей. Наличие резюме и Telegram-сессии
само по себе **не доказывает** доставку откликов компаниям.
Штатная команда `/profile` в @g_jobbot показывает статус: в ответе
**«Ваш профиль принят» / «С ним можно откликаться на вакансии в один клик»**
ApplyPilot видит доказательство допуска. Live-пилот требует свежего
подтверждения от бота не старше 7 суток; после изменения профиля статус
нужно проверить ещё раз и повторно синхронизировать Telegram.

```bash
# Получить последние 500 сообщений, обогатить и пересчитать вакансии.
applypilot accounts telegram-status --online
# В официальном @g_jobbot: /profile (только чтение статуса)
applypilot accounts telegram-sync --limit 500
applypilot accounts telegram-import --track default --limit 500
applypilot sources scan --track default --source getmatch.ru \
  --telegram-hints --details-limit 80 --request-budget 85

# Сверить подтверждённые прошлые отклики по официальным входящим сообщениям.
# Это не создаёт новых откликов и не меняет неподтверждённые записи.
applypilot accounts telegram-reconcile --track default --limit 500
applypilot accounts telegram-plan --track default --limit 500 --json

# Одна конкретная вакансия: показывает статус и проверяет живую
# кнопку/отпечаток callback, НИЧЕГО не отправляет.
applypilot accounts telegram-pilot --track default --id VACANCY_ID \
  --probe-live --json
```

Только после проверки профиля, источника и пригодности конкретной вакансии
владелец может одобрить её. Одиночный live-пилот требует трёх независимых
действий: `APPROVED` в источниковой очереди, точного `--confirm-id` и
`APPLYPILOT_TELEGRAM_LIVE=1` вместе с `--allow-live`.

```bash
# Пример после самостоятельного решения владельца:
applypilot sources approve-batch --track default --source getmatch.ru \
  --id VACANCY_ID --state APPROVED --reason 'Подходящая AI/LLM-вакансия'

APPLYPILOT_TELEGRAM_LIVE=1 applypilot accounts telegram-pilot \
  --track default --id VACANCY_ID --allow-live --confirm-id VACANCY_ID
```

**Это реальная отправка и не dry-run.** Пакетный Getmatch-драйвер включается только явно:

```bash
# Только предварительно индивидуально APPROVED вакансии из ai_core:
applypilot sources dispatch-plan --track default --source getmatch.ru --json

# Реально нажимает сохранённые позитивные callback-кнопки @g_jobbot.
# Отдельное opt-in, по умолчанию всё выключено.
APPLYPILOT_TELEGRAM_LIVE=1 applypilot sources dispatch-apply \
  --track default --source getmatch.ru --limit 100 \
  --allow-live --enable-getmatch --getmatch-min-interval 2 --json
```

Пакетный драйвер проверен регрессионными тестами, включая дубли и остановку
после `UNKNOWN`; многочисленные **реальные** подряд отправки пока
не валидированы. Успех засчитывается только по новому входящему подтверждению
бота с точным ID вакансии; при его отсутствии статус `UNKNOWN`, без повтора.
Кнопки выбора страны и отклики на `SKIP` не допускаются. Сведения о
гражданстве или иных фактах соискателя не заполняются без подтверждённых данных.
`telegram-reconcile` использует только входящие подтверждения, не клики.

## Telegram MTProto для @g_jobbot

1. Создайте приложение на <https://my.telegram.org> → API development tools.
2. Установите optional-зависимость:

```bash
pip install -e '.[telegram]'
```

3. Сохраните API_ID/API_HASH локально (API_HASH вводится скрыто):

```bash
applypilot accounts telegram-setup
```

4. Выполните локальный вход. Код и 2FA-пароль вводятся скрыто и остаются
   в процессе; session сохраняется для следующих запусков:

```bash
applypilot accounts telegram-login --phone +<номер>
```

Первый запуск на чистом каталоге сам создаёт
`private/data/telegram/<account>/` с правами `0700` и файл
`telegram.session` с правами `0600` **до** старта Telethon. После входа,
`status`, `history` и `click` клиент гарантированно закрывается в `finally`,
включая ошибку ввода кода или сетевой сбой. Права `0600` повторно
применяются к session и её `-journal/-wal/-shm` файлам после каждого запуска.

5. Проверьте сессию и прочитайте историю:

```bash
applypilot accounts telegram-status --online
applypilot accounts telegram-history --limit 50
applypilot accounts telegram-sync --limit 50 --json
applypilot accounts telegram-plan --json
```

`telegram-sync` сохраняет сообщения и кнопки в SQLite и строит **dry-run** план.
`telegram-plan` рассматривает только ссылки `getmatch.ru/vacancies/<id>`
из официального чата `@g_jobbot` с **положительной callback-кнопкой
«Откликнуться»**. Кнопки выбора страны / отказа не считаются откликами.
В `planned` включаются лишь вакансии FIT/GOOD_UNKNOWN, `SKIP` показываются
отдельно в `blocked`, отсутствующие/неправильные кнопки — в `unmatched`.
План не разрешает отправку: для каждого кандидата `can_send=false`
и `action=needs_live_validation` до отдельной живой проверки.

Что читается: текст сообщений, дата, inline-кнопки (текст, тип, URL,
наличие callback), публичные ссылки на вакансии. Callback-данные не печатаются;
в SQLite сохраняется только их хэш.

Что не делается: сообщения не отправляются, кнопки не нажимаются, реальные
отклики не создаются. `TelegramAccount.click()` требует двух явных opt-in
(`allow_live=True` и `APPLYPILOT_TELEGRAM_LIVE=1`) и всё равно остаётся
`needs_live_validation` до проверки владельцем.

## HH OAuth

1. Создайте приложение на <https://dev.hh.ru> и укажите redirect URI
   (по умолчанию `http://localhost:8765/callback`).
2. Сохраните client_id/secret локально:

```bash
applypilot accounts hh-oauth-setup
```

3. Получите локальную ссылку авторизации, откройте её в браузере и подтвердите
   доступ:

```bash
applypilot accounts hh-oauth-authorize
```

После подтверждения браузер вернёт локальный redirect URL вида
`http://localhost:8765/callback?code=...&state=...`. Вставьте **полный URL**
в скрытый запрос `hh-oauth-exchange` — код и state извлекаются автоматически:

```bash
applypilot accounts hh-oauth-exchange
```

Если копируете только код, state обязателен явно:

```bash
applypilot accounts hh-oauth-exchange --state '<state из hh-oauth-authorize>'
```

State обязателен на **каждом** обмене: отсутствующий, просроченный (TTL
10 минут) или несовпадающий state отклоняется. После успешного обмена state
удаляется и не может быть использован повторно. Код и токены не печатаются.

4. Проверьте токен, прочитайте отклики и сообщения конкретного отклика:

```bash
applypilot accounts hh-oauth-status --online
applypilot accounts hh-oauth-negotiations --limit 20 --json
applypilot accounts hh-oauth-messages --negotiation-id <id> --limit 50 --json
```

`hh-oauth-messages` — только чтение `GET /negotiations/{id}/messages`; он
ничего не отправляет и не меняет статусы откликов. ID отклика берётся из
`hh-oauth-negotiations`.

5. Посмотрите spec отклика, ничего не отправляя:

```bash
applypilot accounts hh-oauth-apply-plan \
  --vacancy-id 123456 --resume-id <resume_id> --message "..." --json
```

`hh-oauth-apply-plan` возвращает метод, URL, тело и
`verification=needs_live_validation`. Реальная отправка через
`HhOAuthAccount.apply()` заблокирована до отдельного прохода с живым токеном
и подтверждения схемы `POST /negotiations`. Существующий HH-кликер не изменён.

Refresh токена выполняется автоматически при истечении срока; новый токен
сохраняется в тот же файл `0600`.

## Веб-панель

В admin появилась вкладка **«Аккаунты»**:

- показывает локальные статусы Telegram и HH OAuth без секретов;
- кнопки «Проверить Telegram», «Проверить HH OAuth», «Синхронизировать @g_jobbot»
  запускают те же CLI-команды через существующий job runner;
- ввод секретов в UI намеренно отсутствует — только локальный терминал.

## Что проверить при живой авторизации

1. `telegram-login` на реальном номере, затем `telegram-history`: видны ли
   сообщения @g_jobbot и кнопки. Проверить, что после первого запуска
   `private/data/telegram/<account>/` имеет `0700`, а `telegram.session` — `0600`.
2. `telegram-sync` + `telegram-plan`: сопоставляются ли реальные Getmatch-ID
   с очередью.
3. Одна кнопка «Откликнуться»: callback, WebView или переход в браузер.
   Только после этого решать, включать ли `click`.
4. HH OAuth: `status --online`, `negotiations`, затем
   `hh-oauth-messages --negotiation-id <id>`: приходят ли сообщения и автор.
5. `apply-plan` против реальной вакансии: сверить тело с OpenAPI и проверить
   на тестовой вакансии/резюме. Не отправлять массово.
6. Убедиться, что Playwright-кликер HH по-прежнему работает и не конфликтует
   с OAuth-токеном.

## Диагностика

| Симптом | Причина | Действие |
|---|---|---|
| `telethon_missing` | не установлен extra | `pip install -e '.[telegram]'` |
| `missing_credentials` | нет API_ID/HASH | `accounts telegram-setup` |
| `missing` / `expired` | нет session/токена | повторить login/authorize |
| `not_configured` | нет credentials при наличии session | сохранить credentials |
| `error` в HH status | сеть/токен/приложение | проверить `--online`, redirect URI, срок токена |
| `OAuth state is missing` | не запускали `hh-oauth-authorize` или state уже использован | повторить `hh-oauth-authorize` |
| `OAuth state is required` | вставлен только код без `--state` | вставить полный redirect URL или передать `--state` |
| `OAuth state has expired` | прошло больше 10 минут | повторить `hh-oauth-authorize` |
| `OAuth state does not match` | URL/state от другой авторизации | взять state из последнего `hh-oauth-authorize` |
