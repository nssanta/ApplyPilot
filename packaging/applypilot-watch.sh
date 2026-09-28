#!/usr/bin/env bash
# applypilot-watch.sh — периодический скан свежих вакансий HH + LLM-скрининг.
#
# Что делает: для каждого трека из private/config/tracks.toml запускает `scan`
# с коротким окном свежести (--days 1), затем `screen` результата в отчёт и
# снапшот принятых вакансий. НИЧЕГО НЕ ОТКЛИКАЕТСЯ — отклик остаётся ручным
# и запускается отдельно (`apply`). Watcher только ищет, скринит и логирует,
# чтобы можно было ответить среди первых.
#
# Каталог репозитория настраивается через переменную окружения APPLYPILOT_HOME
# (по умолчанию — родитель родителя каталога самого скрипта).
# Ключ aitunnel берётся из AITUNNEL_API_KEY, иначе из private/data/admin-settings.json.
#
# Лог: private/data/watch.log

set -euo pipefail

# --- Определяем корень репозитория ------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
: "${APPLYPILOT_HOME:="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd -P)"}"

cd -- "$APPLYPILOT_HOME"

VENV="$APPLYPILOT_HOME/.venv"
PY="$VENV/bin/python"
LOG="$APPLYPILOT_HOME/private/data/watch.log"

mkdir -p "$APPLYPILOT_HOME/private/data" \
         "$APPLYPILOT_HOME/private/reports" \
         "$APPLYPILOT_HOME/private/data/snapshots"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG"
}

# --- Активируем виртуальное окружение ---------------------------------------
if [[ ! -x "$PY" ]]; then
    log "ОШИБКА: не найден интерпретатор venv: $PY"
    echo "applypilot-watch: не найден $PY" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# --- Ключ aitunnel ----------------------------------------------------------
# Приоритет у переменной окружения; иначе читаем api_key из настроек админки.
if [[ -z "${AITUNNEL_API_KEY:-}" ]]; then
    AITUNNEL_API_KEY="$("$PY" - "$APPLYPILOT_HOME/private/data/admin-settings.json" <<'PYEOF' || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(str(json.load(fh).get("api_key") or "").strip())
except Exception:
    print("")
PYEOF
)"
    export AITUNNEL_API_KEY
fi
if [[ -z "${AITUNNEL_API_KEY:-}" ]]; then
    log "ПРЕДУПРЕЖДЕНИЕ: AITUNNEL_API_KEY не задан — скрининг будет пропущен"
fi

# --- Один проход по направлению ---------------------------------------------
# Аргументы: <key> <rubric> <profile> <search> <report> <accepted-snapshot>
run_track() {
    local track="$1" rubric="$2" profile="$3" search="$4" report="$5" accepted="$6"
    local scan_out rc snap items screen_out kept

    log "track=$track: scan --days 1 старт"

    # set +e вокруг сетевых вызовов: надёжно ловим код возврата вне зависимости
    # от контекста вызова функции (иначе set -e внутри вызванной в '||' функции
    # ведёт себя неочевидно).
    set +e
    scan_out="$("$PY" -m applypilot --profile "$profile" --search "$search" \
        scan --days 1 2>&1)"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        log "track=$track: scan ПРОВАЛ (rc=$rc): $(printf '%s' "$scan_out" | tail -n1)"
        return 1
    fi

    snap="$(printf '%s\n' "$scan_out" | sed -n 's/.*snapshot: \(.*\)$/\1/p' | tail -n1)"
    items="$(printf '%s\n' "$scan_out" | sed -n 's/.*; items: \([0-9]*\);.*/\1/p' | head -n1)"
    if [[ -z "$snap" || ! -f "$snap" ]]; then
        log "track=$track: scan без снапшота — пропуск скрининга"
        return 1
    fi
    log "track=$track: scan ok, items=${items:-?}, snapshot=$snap"

    set +e
    screen_out="$("$PY" -m applypilot --profile "$profile" --search "$search" \
        screen --input "$snap" --track "$rubric" \
        --output "$report" --emit-snapshot "$accepted" 2>&1)"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        log "track=$track: screen ПРОВАЛ (rc=$rc): $(printf '%s' "$screen_out" | tail -n1)"
        return 1
    fi

    kept="$(printf '%s\n' "$screen_out" | sed -n 's/.*accepted ([^)]*): \([0-9]*\) ->.*/\1/p' | head -n1)"
    log "track=$track: screen ok, accepted=${kept:-?}, report=$report, accepted-snapshot=$accepted"
    return 0
}

# --- Запуск ------------------------------------------------------------------
log "watch старт (APPLYPILOT_HOME=$APPLYPILOT_HOME)"

overall=0

# Read the same track roster as the admin. NUL delimiters preserve spaces and
# newlines in paths; no configuration text is executed as shell code.
tracks_file="$(mktemp)"
trap 'rm -f -- "$tracks_file"' EXIT
if ! "$PY" - "$APPLYPILOT_HOME" >"$tracks_file" <<'PYEOF'
import sys
from pathlib import Path

from applypilot.admin import load_tracks

for key, cfg in load_tracks(Path(sys.argv[1])).items():
    fields = (key, cfg["type"], cfg["profile"], cfg["search"],
              cfg["screen_report"], cfg["accepted"])
    sys.stdout.buffer.write(("\0".join(fields) + "\0").encode("utf-8"))
PYEOF
then
    log "ОШИБКА: не удалось прочитать конфигурацию треков"
    exit 1
fi
mapfile -d '' -t track_fields <"$tracks_file"
if [[ ${#track_fields[@]} -eq 0 || $(( ${#track_fields[@]} % 6 )) -ne 0 ]]; then
    log "ОШИБКА: конфигурация треков пуста или повреждена"
    exit 1
fi

# Провал одного направления не должен прерывать другие.
for ((i=0; i<${#track_fields[@]}; i+=6)); do
    run_track "${track_fields[i]}" "${track_fields[i+1]}" "${track_fields[i+2]}" \
        "${track_fields[i+3]}" "${track_fields[i+4]}" "${track_fields[i+5]}" || overall=1
done

log "watch финиш (overall_rc=$overall)"
exit "$overall"
