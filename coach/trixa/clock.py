"""Adeptens dag, inte serverns.

Railway kör UTC. Varje ``date.today()`` i appen gav därför serverns datum:
mellan 00:00 och 02:00 svensk tid låg dashboarden en dag efter — måndagens
pass var "Planerad" i stället för "Idag", söndagens ologgade var "Idag" i
stället för "Missad", och vid ISO-veckoskiftet visades förra veckan som
"den här". Kodöversynen 2026-09-02 (docs/12 F2) räknade tjugo sådana
anrop utan en enda tidszonskonvertering i repot.

Alla datum-nu-anrop går härifrån. Tidszonen är svensk tills adepten kan
bära sin egen; ``TRIXA_TZ`` styr den tills dess.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "Europe/Stockholm"


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TRIXA_TZ", _DEFAULT_TZ))


def now() -> datetime:
    """Nu, i adeptens tidszon."""
    return datetime.now(tz())


def today() -> date:
    """Dagens datum i adeptens tidszon."""
    return now().date()
