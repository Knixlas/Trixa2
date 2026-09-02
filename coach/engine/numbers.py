"""Tolerant talkonvertering — en uppsättning, med uttalad semantik.

PostgREST levererar numerics som strängar, passbanken bär tal som mallar,
formulär skickar tomma strängar. Sju hjälpare på fem ställen gjorde samma
sak lite olika (docs/12 I10): två hette ``_as_float`` men den ena gav
``None`` för 0 och den andra ``0.0``; en ``_safe_int`` tog "3.0", en
``_as_int`` kastade på det. Flyttad kod fick fel import utan att något
sade till.

Här: ``to_float``/``to_int`` med ``default`` (None om inget anges),
``positive_float`` för lastvärden där 0 betyder "saknas".
"""

from __future__ import annotations

from typing import Any


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int | None = None) -> int | None:
    """Tar "3", 3.0 och "3.0". Rundar inte: 3.7 → 3, som int()."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def positive_float(value: Any) -> float | None:
    """Ett lastvärde: 0, negativt, tomt eller trasigt är alla "saknas"."""
    out = to_float(value)
    return out if out is not None and out > 0 else None
