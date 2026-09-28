# Пакетирование ApplyPilot

Каталог `packaging/` содержит экспериментальную сборку Debian-пакета и watcher свежих вакансий.

## Debian `.deb`

`build-deb.sh` собирает самодостаточный пакет:

- приложение устанавливается в `/opt/applypilot`;
- внутри создаётся Python virtualenv;
- launcher устанавливается как `/usr/local/bin/applypilot`.

### Сборка

Из корня репозитория:

```bash
bash packaging/build-deb.sh
```

Результат:

```text
applypilot_<version>_amd64.deb
```

### Параметры

Переопределить версию:

```bash
VERSION=1.2.3 bash packaging/build-deb.sh
```

Добавить Python-зависимость Playwright:

```bash
WITH_BROWSER=1 bash packaging/build-deb.sh
```

Оба параметра можно использовать вместе:

```bash
VERSION=1.2.3 WITH_BROWSER=1 bash packaging/build-deb.sh
```

### Установка

```bash
sudo dpkg -i applypilot_*.deb
```

Если `dpkg` сообщает о недостающих системных зависимостях:

```bash
sudo apt-get -f install
```

Проверка:

```bash
applypilot --help
applypilot admin
```

Удаление:

```bash
sudo dpkg -r applypilot
```

### Ограничения пакета

- во время сборки нужен доступ к PyPI;
- virtualenv привязан к Python, использованному при сборке;
- целевой системе нужен совместимый Python 3.12+;
- `WITH_BROWSER=1` устанавливает пакет Playwright, но не скачивает браузерные бинарники;
- HH login настраивается пользователем после установки;
- `private/` и другие персональные данные в пакет не копируются;
- текущая сборка ориентирована на `amd64`.

После установки Playwright браузер при необходимости ставится отдельно:

```bash
/opt/applypilot/venv/bin/python -m playwright install chromium
```

## Watcher свежих вакансий

`applypilot-watch.sh` выполняет периодический read/search + LLM-screen цикл для всех активных треков.

Для каждого трека:

1. `scan --days 1 --sort-mode newest`;
2. извлечение созданного snapshot;
3. `screen`;
4. запись screen report и accepted snapshot;
5. запись результата в `private/data/watch.log`.

**Watcher никогда не запускает `apply`.**

Треки загружаются через `applypilot.tracks.load_tracks`, то есть watcher и web-admin используют один источник конфигурации.

### Корень репозитория

Задаётся через:

```bash
APPLYPILOT_HOME=/path/to/ApplyPilot
```

Если переменная отсутствует, shell-скрипт определяет корень относительно своего каталога.

### API-ключ

Приоритет:

1. `AITUNNEL_API_KEY`;
2. `api_key` из `private/data/admin-settings.json`;
3. `private/config/aitunnel.key`.

Если ключа нет, watcher пишет предупреждение и не сможет выполнить LLM-screen.

## systemd user timer

Установка вручную:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/applypilot-watch.service packaging/applypilot-watch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now applypilot-watch.timer
```

По умолчанию:

- первый запуск — через 5 минут;
- затем — раз в час;
- `Persistent=true`.

Проверить:

```bash
systemctl --user list-timers applypilot-watch.timer
systemctl --user status applypilot-watch.timer
```

Разовый прогон:

```bash
systemctl --user start applypilot-watch.service
```

Логи:

```bash
journalctl --user -u applypilot-watch.service
tail -f private/data/watch.log
```

### Путь к checkout

При ручной установке отредактируйте `APPLYPILOT_HOME` в service-файле.

При установке из web-admin текущий путь к checkout автоматически подставляется и в `Environment`, и в `ExecStart`.

### Изменение интервала

Отредактируйте `OnUnitActiveSec` в timer:

```ini
OnUnitActiveSec=30min
```

После изменения:

```bash
systemctl --user daemon-reload
systemctl --user restart applypilot-watch.timer
```

## cron

Если systemd user недоступен:

```cron
0 * * * * APPLYPILOT_HOME=/path/to/ApplyPilot /path/to/ApplyPilot/packaging/applypilot-watch.sh
```

Не храните API-ключ прямо в общей crontab, если можно передать его через защищённое окружение или локальные admin settings.
