# ApplyPilot — Multi-Source

Каждый источник предоставляет независимый поиск и, когда поддерживается, отдельный драйвер отклика. Центральный слой объединяет карточки, рейтинг, review-очередь и SQLite-журнал, не смешивая вакансии разных платформ.

## Матрица возможностей

| Площадка | Discovery | Отклик |
|---|---|---|
| `hh.ru` | Поиск, запросы/регионы/страницы, загрузка деталей | Существующий браузерный runner; пределы из профиля |
| `career.habr.com` | Каталог, карточки, form-audit | Проверенная простая нативная форма; сложные поля требуют разбора |
| `getmatch.ru` | Sitemap, Telegram-ссылки, инкрементальная загрузка | Callback при актуальной кнопке + проверенная web-форма (`/api/offers/<id>/apply` + `/applications`) |
| `geekjob.ru` | Каталог, карточки, form-audit | Проверенный native submit: `/json/respond/vacancy` 200 + `/responses` |
| `yandex.ru/jobs` | Вакансии и анкеты | Проверенный native survey: submit, success page, applied state и POST-ответ |
| `dreamjob.ru` | Каталог компаний | Только `mirror_only`: отклик и ответ видны через HH, второй submit запрещён |

Матрица описывает **реализованные компоненты**, а не какие-либо реальные профили, проверки кандидата или историю найма.

## Безопасный запуск

~~~bash
# Публичные синтетические fixtures, не требуют аккаунтов
.venv/bin/python -m applypilot sources scan \
  --track default --fixture-dir tests/fixtures/public --json

# Живой поиск без отправки
.venv/bin/python -m applypilot run campaign \
  --track default --source hh.ru --source career.habr.com \
  --source getmatch.ru --source geekjob.ru \
  --days 30 --until-complete --resume --max-passes 100 --limit 100

# Просмотр и dry-run выбранных записей
.venv/bin/python -m applypilot sources status --track default
.venv/bin/python -m applypilot sources dispatch-plan \
  --track default --limit 50 --json

# Аудит форм без отправки
.venv/bin/python -m applypilot sources form-audit \
  --track default --source career.habr.com --view review --limit 10 --json
~~~

В индивидуальном `private/config/sources.toml` задаются лишь метки аккаунтов. Сами cookies, токены, профили браузера, отклики и снимки не входят в публичный репозиторий. Реальные параметры поиска задаются отдельно.

## Скоринг и review

- `FIT` — совпадение роли и подтверждённых условий.
- `GOOD_UNKNOWN` — роль подходит, но условия требуют уточнения.
- `NEEDS_ENRICHMENT` — данных пока недостаточно.
- `MANUAL_REVIEW` — требуется участие оператора.
- `SKIP` — обнаружено несоответствие.
- `APPLIED` — отклик уже учтён по истории источника.

`TOP` и `review` используют актуальную оценку трека из SQLite, а не старую метку HTML-снимка. Словарь роли и резюме переключаются через preset/track; личные зарплатные ожидания, география, опыт и дополнительные semantic guards задаются в локальной конфигурации. Общий scanner применяет эти guards независимо от имени трека.

`APPROVED` отделяет вакансию, допущенную к отправке, от простой оценки `FIT`. Его может выставить настроенное автоодобрение по приватным критериям или оператор при разборе; повторное подтверждение владельца для каждой вакансии не требуется. Live-запуск всё равно требует явных флагов, готового состояния, проверенного драйвера, дедупликации и лимитов площадки.

`decision.APPLY/REVIEW/REJECT/APPLIED/BLOCKED` — отдельная ось поверх review. `READY_TO_APPLY` показывается только треку с `APPROVED` и прошедшим score/filter; общий `vacancies.application_state` не делает вакансию готовой во всех треках. `queue sweep` пересматривает старые строки и не оставляет `NOT_STARTED/READY` без окончательного решения. Повторяющиеся employer+title посты схлопываются до отправки.

## Source-aware хранилище

- `vacancies` — нормализованные записи по `(account, source, vacancy_id)`;
- `vacancy_track_scores` — независимые оценки по трекам;
- `vacancy_links` — канонический канал, mirror и внешний ATS;
- `source_application_attempts` — состояния откликов;
- `source_reservations` и `channel_reservations` — блокировка конкурентной отправки.

Ключ канала предотвращает дубли между источниками, ведущими в одну внешнюю форму. DreamJob может зеркалировать HH и не создаёт нового канала.

Пример жизненного цикла:

~~~text
prepared -> submitting -> success
                      -> unknown (карантин, без автоматического повтора)
                      -> failed_before_submit
                      -> needs_manual
~~~

`reconcile` требует независимого подтверждения провайдера, а `recover` не повторяет потенциально выполненные действия вслепую. Один `UNKNOWN` без CAPTCHA карантинится на уровне вакансии; независимые вакансии батча продолжают работу. CAPTCHA, потеря сессии и platform block останавливают источник.

`dreamjob.ru` — зеркало HH. Второй отклик туда не отправляется. Команда `queue links` собирает одну логическую вакансию по `channel_key`/`job_group_key` и показывает все источники, `applied_via`, receipt и состояние ответа. DreamJob-строка получает `MIRRORED_APPLIED_VIA_HH`, а ответ работодателя из HH-переговоров виден в обеих строках.

## Границы Live

`scan`, `inspect`, `form-audit` и `dispatch-plan` — read-only или dry-run. Драйвер может отправлять заявки только при подтверждённой сессии, готовом состоянии вакансии и явном live-режиме. Пределы применяются отдельно по источнику и аккаунту. Успешная одиночная отправка подтверждает только конкретный путь; пакетную готовность следует считать по нескольким последовательным receipts и полному пулу подходящих вакансий.

Нативная форма, Telegram callback, WebView и внешняя ATS — разные способы подачи. Успех одного способа не подтверждает остальные. Результат отправки должен сверяться с историей или квитанцией самого источника.

См. [быстрый старт](QUICKSTART.md), [аккаунты](ACCOUNTS.md) и [архитектуру](ARCHITECTURE.md).
