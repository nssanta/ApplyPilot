"""Помощники для пауз и дневных лимитов в auto-apply.

Это чистые детерминированные при заданном ``random.Random`` функции: они только
вычисляют время до следующего действия и факт исчерпания дневного бюджета.
Никакого fingerprinting, обхода антибот-защиты или обработки CAPTCHA здесь нет.
"""

from __future__ import annotations

import random


def next_delay(
    rng: random.Random,
    *,
    min_seconds: float,
    max_seconds: float,
    index: int,
    long_pause_every: int = 0,
    long_pause_min: float = 0.0,
    long_pause_max: float = 0.0,
) -> float:
    """Возвращает задержку в секундах до следующего действия.

    Базовая задержка — ``rng.uniform(min_seconds, max_seconds)``. Если
    ``long_pause_every > 0`` и ``index`` кратен этому значению, сверху
    добавляется длинная пауза из диапазона ``long_pause_min..long_pause_max``.

    При ``min_seconds > max_seconds`` границы меняются местами, отрицательные
    значения ограничиваются нулём. ``long_pause_every=0`` отключает длинные паузы.
    """
    lo, hi = _sanitize_bounds(min_seconds, max_seconds)
    delay = rng.uniform(lo, hi)

    if long_pause_every > 0 and index > 0 and index % long_pause_every == 0:
        plo, phi = _sanitize_bounds(long_pause_min, long_pause_max)
        delay += rng.uniform(plo, phi)

    return delay


def daily_cap_reached(count: int, per_day: int) -> bool:
    """Возвращает признак достижения дневного лимита действий.

    Значение ``per_day <= 0`` отключает лимит. Иначе True возвращается,
    когда ``count >= per_day``.
    """
    if per_day <= 0:
        return False
    return count >= per_day


def _sanitize_bounds(low: float, high: float) -> tuple[float, float]:
    """Меняет перепутанные границы местами и ограничивает отрицательные значения нулём."""
    if low > high:
        low, high = high, low
    return max(low, 0.0), max(high, 0.0)