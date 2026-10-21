# Пакетирование ApplyPilot

Каталог `packaging/` содержит экспериментальную сборку Debian-пакета и legacy-файлы watcher.

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

## Legacy watcher

`applypilot-watch.sh` и systemd unit-файлы сохранены для истории, но не входят в актуальный рабочий процесс. ApplyPilot запускает поиск только по явной команде пользователя; не устанавливайте и не включайте watcher, timer или cron. Ручной поиск и постоянная очередь доступны через `applypilot run campaign` и локальную вкладку «Очередь».
