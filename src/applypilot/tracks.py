"""Конфигурация независимых поисковых треков.

Модуль намеренно не зависит от UI: CLI и watcher загружают тот же набор треков,
не импортируя реализацию web-admin.
"""

from __future__ import annotations

import json
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from .config import ConfigError

TRACKS_CONFIG = "private/config/tracks.toml"
RUBRIC_TYPES = ("ai", "infra", "general")

DEFAULT_TRACKS = [
    {
        "key": "default",
        "label": "Основной трек",
        "type": "general",
        "profile": "private/config/profile.toml",
        "search": "private/config/search.toml",
        "resume": "",
    },
]

TRACKS: dict[str, dict[str, Any]] = {}


def _private_path(value: Any, default: str, field: str) -> str:
    text = str(value or default).strip()
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "private":
        raise ConfigError(f"track.{field} must be a relative path below private/")
    return path.as_posix()


def _normalise_track(entry: dict[str, Any], *, strict_key: bool = False) -> dict[str, Any]:
    raw_key = str(entry.get("key", "")).strip().lower()
    key = "".join(ch for ch in raw_key if ch in "abcdefghijklmnopqrstuvwxyz0123456789_-")
    if not key:
        raise ConfigError("track.key is required and must use latin letters, digits, _ or -")
    if strict_key and key != raw_key:
        raise ConfigError(f"invalid track key: {entry.get('key')!r}")

    rubric = str(entry.get("type", "general")).strip().lower()
    if rubric not in RUBRIC_TYPES:
        raise ConfigError(f"track {key!r}: type must be one of {', '.join(RUBRIC_TYPES)}")

    return {
        "key": key,
        "label": str(entry.get("label") or key).strip() or key,
        "type": rubric,
        "profile": _private_path(
            entry.get("profile"), f"private/config/profile-{key}.toml", "profile"
        ),
        "search": _private_path(
            entry.get("search"), f"private/config/search-{key}.toml", "search"
        ),
        "resume": str(entry.get("resume") or "").strip(),
        "screen_report": _private_path(
            entry.get("screen_report"), f"private/reports/screen-{key}.json", "screen_report"
        ),
        "accepted": _private_path(
            entry.get("accepted"),
            f"private/data/snapshots/accepted-{key}.json",
            "accepted",
        ),
    }


def _normalise_entries(
    entries: list[dict[str, Any]], *, strict_keys: bool = False
) -> list[dict[str, Any]]:
    if not entries:
        raise ConfigError("tracks config must contain at least one [[track]]")
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"track[{index}] must be a TOML table")
        track = _normalise_track(entry, strict_key=strict_keys)
        if track["key"] in seen:
            raise ConfigError(f"duplicate track key: {track['key']}")
        seen.add(track["key"])
        normalised.append(track)
    return normalised


def load_tracks(root: Path) -> dict[str, dict[str, Any]]:
    """Загружает список треков, никогда не перезаписывая повреждённый существующий файл.

    При отсутствии конфига создаётся один нейтральный трек. После появления файла
    ошибки TOML, структуры или путей обрабатываются fail-closed: пользовательский
    конфиг сохраняется для исправления вместо тихой замены.
    """

    path = root / TRACKS_CONFIG
    # Не оставляем устаревший roster в памяти, если текущий файл не удалось загрузить.
    TRACKS.clear()
    if path.exists():
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except OSError as exc:
            raise ConfigError(f"cannot read tracks config: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid tracks TOML; file was left unchanged: {exc}") from exc

        raw = data.get("track")
        if not isinstance(raw, list):
            raise ConfigError("tracks config must contain [[track]] tables")
        entries = _normalise_entries(raw, strict_keys=True)
    else:
        entries = _normalise_entries([dict(entry) for entry in DEFAULT_TRACKS])
        _write_tracks_config(path, entries)

    TRACKS.clear()
    TRACKS.update({entry["key"]: entry for entry in entries})
    return TRACKS


def _write_tracks_config(path: Path, entries: list[dict[str, Any]]) -> None:
    """Атомарно сохраняет проверенные определения треков."""

    normalised = _normalise_entries(entries)
    lines = [
        "# Треки поиска для ApplyPilot. Конфиг общий для CLI/watch/UI.",
        "# key/type(ai|infra|general)/profile/search/resume; пути только внутри private/.",
        "",
    ]
    for track in normalised:
        lines.append("[[track]]")
        for field_name in (
            "key",
            "label",
            "type",
            "profile",
            "search",
            "resume",
            "screen_report",
            "accepted",
        ):
            lines.append(
                f'{field_name} = {json.dumps(track[field_name], ensure_ascii=False)}'
            )
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write("\n".join(lines))
            fh.flush()
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)