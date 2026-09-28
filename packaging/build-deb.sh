#!/usr/bin/env bash
#
# build-deb.sh — сборка самодостаточного Debian-пакета ApplyPilot.
#
# Полученный .deb содержит Python virtualenv с установленным приложением
# в /opt/applypilot и небольшой launcher /usr/local/bin/applypilot.
#
# Использование:
#   bash packaging/build-deb.sh
#   VERSION=1.2.3 bash packaging/build-deb.sh
#   WITH_BROWSER=1 bash packaging/build-deb.sh
#
# Переменные окружения:
#   VERSION       Переопределить версию пакета. По умолчанию читается
#                 [project].version из pyproject.toml.
#   WITH_BROWSER  При значении 1 установить необязательную зависимость
#                 "browser" (Playwright) во встроенный virtualenv.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Определяем пути.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYPROJECT="${REPO_ROOT}/pyproject.toml"

PACKAGE="applypilot"
ARCH="amd64"
MAINTAINER="${MAINTAINER:-ApplyPilot Maintainers <maintainers@example.invalid>}"

# ---------------------------------------------------------------------------
# Предварительные проверки.
# ---------------------------------------------------------------------------
echo "==> Проверка зависимостей сборки"

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "ОШИБКА: dpkg-deb не найден. Установите: sudo apt-get install dpkg-dev" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ОШИБКА: python3 не найден. Нужен Python 3.12 или новее." >&2
  exit 1
fi

if [ ! -f "${PYPROJECT}" ]; then
  echo "ОШИБКА: pyproject.toml не найден: ${PYPROJECT}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Определяем версию.
# ---------------------------------------------------------------------------
if [ -n "${VERSION:-}" ]; then
  version="${VERSION}"
  echo "==> Версия из VERSION: ${version}"
else
  version="$(python3 - "${PYPROJECT}" <<'PYEOF'
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Совместимый fallback для Python < 3.11.
    tomllib = None

path = sys.argv[1]
if tomllib is not None:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    print(data["project"]["version"])
else:
    # Минимальный fallback-парсер: ищем version внутри [project].
    in_project = False
    for line in open(path, encoding="utf-8"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project and stripped.startswith("version"):
            _, _, rhs = stripped.partition("=")
            print(rhs.strip().strip('"').strip("'"))
            break
PYEOF
)"
  if [ -z "${version}" ]; then
    echo "ОШИБКА: не удалось определить версию из pyproject.toml" >&2
    exit 1
  fi
  echo "==> Версия из pyproject.toml: ${version}"
fi

# ---------------------------------------------------------------------------
# Готовим временную структуру пакета.
# ---------------------------------------------------------------------------
BUILD_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf "${BUILD_ROOT}"
}
trap cleanup EXIT

STAGE="${BUILD_ROOT}/${PACKAGE}_${version}_${ARCH}"
echo "==> Подготовка структуры пакета: ${STAGE}"

mkdir -p "${STAGE}/opt/applypilot"
mkdir -p "${STAGE}/usr/local/bin"
mkdir -p "${STAGE}/DEBIAN"

# ---------------------------------------------------------------------------
# Создаём встроенный virtualenv и устанавливаем приложение.
# ---------------------------------------------------------------------------
VENV_DIR="${STAGE}/opt/applypilot/venv"
echo "==> Создание virtualenv: ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

echo "==> Обновление pip внутри virtualenv"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null

if [ "${WITH_BROWSER:-0}" = "1" ]; then
  echo "==> Установка ApplyPilot с browser-extra (нужен доступ к сети)"
  "${VENV_DIR}/bin/pip" install "${REPO_ROOT}[browser]"
else
  echo "==> Установка ApplyPilot (нужен доступ к сети)"
  "${VENV_DIR}/bin/pip" install "${REPO_ROOT}"
fi

# ---------------------------------------------------------------------------
# Устанавливаем launcher.
# ---------------------------------------------------------------------------
LAUNCHER="${STAGE}/usr/local/bin/applypilot"
echo "==> Создание launcher: /usr/local/bin/applypilot"
cat > "${LAUNCHER}" <<'LAUNCHEOF'
#!/bin/sh
exec /opt/applypilot/venv/bin/python -m applypilot "$@"
LAUNCHEOF
chmod 755 "${LAUNCHER}"

# ---------------------------------------------------------------------------
# Создаём DEBIAN/control.
# ---------------------------------------------------------------------------
echo "==> Создание DEBIAN/control"
cat > "${STAGE}/DEBIAN/control" <<CONTROLEOF
Package: ${PACKAGE}
Version: ${version}
Section: utils
Priority: optional
Architecture: ${ARCH}
Maintainer: ${MAINTAINER}
Depends: python3 (>= 3.12)
Description: Локальный инструмент поиска вакансий и откликов на HH.ru
 ApplyPilot помогает искать, оценивать и просматривать вакансии, готовить
 сопроводительные письма и контролируемо вести процесс откликов.
 .
 Пакет содержит отдельный Python virtualenv в /opt/applypilot и команду
 applypilot в /usr/local/bin. Браузеры Playwright и вход в HH.ru
 пользователь настраивает после установки.
CONTROLEOF

# ---------------------------------------------------------------------------
# Собираем .deb.
# ---------------------------------------------------------------------------
OUTPUT="${REPO_ROOT}/${PACKAGE}_${version}_${ARCH}.deb"
echo "==> Сборка пакета через dpkg-deb"
dpkg-deb --build --root-owner-group "${STAGE}" "${OUTPUT}"

echo
echo "==> Готово. Пакет:"
echo "    ${OUTPUT}"
echo
echo "Установка:"
echo "    sudo dpkg -i ${OUTPUT}"
