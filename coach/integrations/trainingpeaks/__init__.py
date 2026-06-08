"""TrainingPeaks-integration — Trixa2:s enda externa datakälla/skrivkanal.

Se `docs/06_TP_INTEGRATION_REBUILD.md`.

- `client`         — synkron HTTP-klient (auth, läs, skriv)
- `mapping`        — passbank main_set → TP strukturformat
- `sync`           — TP → Supabase (matar tabellerna engine läser)
- `workout_writer` — WeekPlan → TP planerade pass
"""

from .client import (
    TPAuthError,
    TPClient,
    TPError,
    TPNotFoundError,
    TPRateLimitError,
    default_cookie_provider,
)

__all__ = [
    "TPClient",
    "TPError",
    "TPAuthError",
    "TPNotFoundError",
    "TPRateLimitError",
    "default_cookie_provider",
]
