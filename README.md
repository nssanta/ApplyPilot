# ApplyPilot

ApplyPilot — локальный инструмент для поиска вакансий, оценки соответствия и контролируемых откликов. Включает CLI, SQLite и локальную веб-панель.

**Рабочие аккаунты, критерии поиска, резюме, сессии, переписки и история откликов не являются публичной документацией.** Они хранятся в пользовательском каталоге `private/`, не входящем в Git.

## Возможности

- Поиск вакансий по нескольким платформам и раздельные поисковые треки.
- Нормализация карточек, объяснимый скоринг и optional LLM-скрининг.
- Отдельные очереди review/approval, черновики писем и ручные анкеты.
- SQLite-журнал, канонические каналы отправки и защита от дублей.
- Безопасные `scan`, `form-audit`, `dispatch-plan` и `dry-run`.
- Браузерные и Telegram-драйверы откликов, требующие явного разрешения и проверки результата.
- Опциональная веб-панель и экспериментальная Debian-сборка.

## Установка

Требуется Python 3.12+.

~~~bash
git clone https://github.com/nssanta/ApplyPilot.git
cd ApplyPilot
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m applypilot doctor
~~~

Для браузерных функций могут потребоваться Playwright и установленный Chrome, для Telegram — optional-зависимость `telethon`.

## Начало работы

Скопируйте нейтральные примеры в собственную локальную конфигурацию:

~~~bash
mkdir -p private/config
cp examples/profile.example.toml private/config/profile.toml
cp examples/search.example.toml private/config/search.toml
cp examples/sources.toml private/config/sources.toml

# Список провайдеров и офлайн-скан на синтетических HTML.
.venv/bin/python -m applypilot sources list
.venv/bin/python -m applypilot sources scan \
  --track default --fixture-dir tests/fixtures/public --json

# Без отправки заявок
.venv/bin/python -m applypilot sources dispatch-plan \
  --track default --limit 10 --json
~~~

Настраивайте собственные роли, зарплатные ожидания, географию, ограничения и резюме только в локальных профилях. Примеры в репозитории не описывают реальную кампанию какого-либо пользователя.

## Источники

| Источник | Поиск | Действия отклика |
|---|---|---|
| HH.ru | Поиск и карточки | Отдельный браузерный драйвер |
| Habr Career | Каталог и form-audit | Нативная форма при явном live opt-in |
| Getmatch | Sitemap, Telegram-ссылки, карточки | Подтверждаемый нативный Telegram callback |
| GeekJob | Каталог и проверка формы | Ограниченный пилот, не массовый драйвер |
| Яндекс Карьера | Вакансии и read-only анкеты | Нет проверенной массовой отправки |
| DreamJob | Каталог компаний | Зеркало HH; второй отклик запрещён |

Таблица описывает **возможности кода**, а не авторизацию чьих-либо аккаунтов или результаты реальных отправок. Детали в [Multi-Source](docs/MULTISOURCE.md).

## Границы live-операций

Поиск, скрининг, аудит форм и планирование не отправляют заявки. Для реального отклика необходимы готовый профиль, одобренная вакансия, подходящий драйвер и отдельное live-разрешение. Статус `UNKNOWN` блокирует повтор до независимого подтверждения источника.

Не подставляйте неизвестные личные данные в анкеты и не рассматривайте нажатие кнопки как доказательство успешной отправки.

## Веб-панель и документация

~~~bash
./run.sh
~~~

Локальная панель использует те же профили и SQLite, что и CLI.

- [Быстрый старт](docs/QUICKSTART.md)
- [Multi-Source](docs/MULTISOURCE.md)
- [Аккаунты и сессии](docs/ACCOUNTS.md)
- [Архитектура](docs/ARCHITECTURE.md)
- [Сборка Debian-пакета](packaging/README.md)

## Проверки

~~~bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python scripts/check_public.py
.venv/bin/python scripts/offline_multisource_smoke.py
git diff --check
~~~

Примечание: исправление текущей документации не удаляет ранее опубликованные данные из истории Git.

## Участники

Спасибо участникам, чьи изменения вошли в основной код проекта:

- [@Vova4o](https://github.com/Vova4o) — контур сопроводительных писем на подтверждённых фактах и улучшения надёжности откликов ([PR #1](https://github.com/nssanta/ApplyPilot/pull/1)).
- [@artemius125](https://github.com/artemius125) — LLM-скрининг вакансий, локальная веб-админка, треки, watcher/packaging и основа hardening-прохода ([PR #3](https://github.com/nssanta/ApplyPilot/pull/3); заменил PR #2).
