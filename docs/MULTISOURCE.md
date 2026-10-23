# ApplyPilot — Multi-Source

Каждый источник предоставляет независимый поиск и, когда поддерживается, отдельный драйвер отклика. Центральный слой объединяет карточки, рейтинг, review-очередь и SQLite-журнал, не смешивая вакансии разных платформ.

## Матрица возможностей

| Площадка | Discovery | Отклик |
|---|---|---|
| `hh.ru` | Поиск и загрузка деталей | Браузерный HH runner |
| `career.habr.com` | Каталог, карточки, form-audit | Нативный one-click при opt-in |
| `getmatch.ru` | Sitemap, Telegram-ссылки, инкрементальная загрузка | Callback через официальный бот при opt-in |
| `geekjob.ru` | Каталог, карточки, form-audit | Ограниченный пилот; не массовая отправка |
| `yandex.ru/jobs` | Вакансии и read-only анкеты | Отправка не подтверждена |
| `dreamjob.ru` | Каталог компаний | Только `mirror_only` без второго отклика |

Матрица описывает **реализованные компоненты**, а не какие-либо реальные профили, проверки кандидата или историю найма.

## Безопасный запуск

~~~bash
# Публичные синтетические fixtures, не требуют аккаунтов
.venv/bin/python -m applypilot sources scan \
  --track default --fixture-dir tests/fixtures/public --json

# Живой поиск без отправки
.venv/bin/python -m applypilot sources scan --track default --limit 50

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

`TOP` и `review` используют актуальную оценку трека из SQLite, а не старую метку HTML-снимка. Порог зарплаты, допустимая география, опыт и другие условия принадлежат локальному профилю пользователя, а не публичной документации.

Решение `APPROVED` не эквивалентно `FIT`: оно требует отдельного осознанного действия для конкретной вакансии.

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

`reconcile` требует независимого подтверждения провайдера, а `recover` не повторяет потенциально выполненные действия вслепую.

## Границы Live

`scan`, `inspect`, `form-audit` и `dispatch-plan` — read-only или dry-run. Драйвер может отправлять заявки только при наличии разрешённой авторизованной сессии, источникового `APPROVED`, готового состояния вакансии и явного live-режима.

Нативная форма, Telegram callback, WebView и внешняя ATS — разные способы подачи. Успех одного способа не подтверждает остальные. Результат отправки должен сверяться с историей или квитанцией самого источника.

См. [быстрый старт](QUICKSTART.md), [аккаунты](ACCOUNTS.md) и [архитектуру](ARCHITECTURE.md).
