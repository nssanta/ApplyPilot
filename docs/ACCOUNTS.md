# ApplyPilot — локальные аккаунты

Интеграции HH OAuth, браузерных сессий и Telegram MTProto изолированы от публичного репозитория. Этот документ описывает **устройство подключения**, а не состояние чьего-либо настоящего аккаунта.

## Секреты и профили

- Все ключи, cookies, session storage и личные параметры держите в `private/`.
- Не копируйте файлы сессий в issue, pull request, чат или диагностический лог.
- Права на локальные файлы секретов должны быть ограничены владельцем (обычно `0600`, каталоги `0700`).
- Отправку нельзя считать разрешённой только потому, что Telegram или сайт авторизованы.
- Внешние запросы чтения, формы подачи и переписки — разные категории действий.

## Telegram MTProto

Необязательная зависимость:

~~~bash
.venv/bin/python -m pip install -e '.[telegram]'
.venv/bin/python -m applypilot accounts telegram-setup
.venv/bin/python -m applypilot accounts telegram-login
.venv/bin/python -m applypilot accounts telegram-status --online
~~~

API_ID и API_HASH, коды входа и 2FA вводите только в локальном окружении. Не добавляйте значения в публичные примеры.

Для источника, использующего официального Telegram-бота, можно синхронизировать сообщения и связывать публичные ссылки с source-aware очередью:

~~~bash
# Чтение без откликов
.venv/bin/python -m applypilot accounts telegram-sync --limit 50
.venv/bin/python -m applypilot accounts telegram-import \
  --track default --limit 50
.venv/bin/python -m applypilot accounts telegram-plan \
  --track default --json

# Сверка уже подтверждённых откликов без новой отправки
.venv/bin/python -m applypilot accounts telegram-reconcile \
  --track default --limit 50
~~~

Native callback-драйвер проверяет входящее сообщение официального бота, точный ID вакансии, текст кнопки, её контрольную сумму и свежесть подтверждения принятого кандидатского профиля. Для live-отправки необходимо отдельное разрешение, `APPROVED` и готовое состояние вакансии.

Одно нажатие кнопки **не является подтверждением**. Успех учитывается только по ответу официального источника с точным ID; `UNKNOWN` карантинируется. Дополнительные формы, WebView и сообщения HR требуют отдельной реализации и проверки.

## HH OAuth

OAuth-методы могут предоставлять чтение статусов, откликов и переписок в рамках разрешений аккаунта.

~~~bash
.venv/bin/python -m applypilot accounts hh-oauth-setup
.venv/bin/python -m applypilot accounts hh-oauth-authorize
.venv/bin/python -m applypilot accounts hh-oauth-status --online
~~~

Обмен authorization code выполняется приватно, по подтверждённому redirect URI и с проверкой `state`. Код авторизации и refresh/access tokens не следует помещать в публичные логи. Наличие read-only OAuth доступа не доказывает возможность API-отклика.

Для браузерной HH-сессии используйте штатный локальный `login` и `session check`. Браузерные cookies хранятся отдельно от Telegram и OAuth.

## Проверки перед отправкой

Проверьте нужную вакансию, актуальность объявления, совместимость условий, правильное резюме или профиль, канал и отсутствие предыдущего отклика. При неопределённости не повторяйте автоматическую отправку до сверки.

Не подставляйте неизвестные факты о гражданстве, опыте, стаже или иных персональных обстоятельствах. Все реальные контакты кандидата, ответы работодателей и статусы откликов должны оставаться в приватных журналах.

[Безопасный Quickstart](QUICKSTART.md) · [Multi-Source](MULTISOURCE.md)
