"""Besvärens påverkan per gren — en väg för planeraren, rampen och MCP.

Förut lästes bara ``impact_per_discipline``. Onboardingen skrev ``{}`` och
hälsosidan förvalde "none", så ett ont knä med grad 2 ändrade ingenting
(docs/13). Nu bedöms saknad påverkan ur kroppsdel och grad
(data/injury_impact.yaml). Det som valts uttryckligen gäller alltid.
"""

from __future__ import annotations

from coach.engine._loader import load_yaml

SPORTS = ("swim", "bike", "run", "strength")
_RANK = {"none": 0, "partial": 1, "full": 2}
AUTO = "auto"


def _rules() -> dict:
    return load_yaml("injury_impact.yaml")


def _locations(concern: dict) -> list[str]:
    locs = concern.get("locations") or ([concern["location"]] if concern.get("location") else [])
    out = []
    for loc in locs:
        base = str(loc)
        for suffix in ("_left", "_right"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        out.append(base)
    return out


def _severity(concern: dict) -> int:
    try:
        return max(1, min(5, int(concern.get("severity") or 0)))
    except (TypeError, ValueError):
        return 0


def derived_impacts(concern: dict) -> dict[str, str]:
    """Påverkan ur kroppsdel och grad, utan hänsyn till uttryckliga val."""
    rules = _rules()
    loads = rules.get("load_by_location") or {}
    table = rules.get("impact_by_severity") or {}
    sev = _severity(concern)
    out = {s: "none" for s in SPORTS}
    if not sev:
        return out
    for loc in _locations(concern):
        for sport, load in (loads.get(loc) or {}).items():
            level = (table.get(load) or {}).get(sev) or (table.get(load) or {}).get(str(sev)) or "none"
            if sport in out and _RANK.get(level, 0) > _RANK[out[sport]]:
                out[sport] = level
    return out


def concern_impacts(concern: dict) -> dict[str, dict]:
    """Påverkan per gren för ett besvär, med källa: "explicit" eller "auto"."""
    explicit = concern.get("impact_per_discipline") or {}
    derived = derived_impacts(concern)
    out = {}
    for sport in SPORTS:
        value = explicit.get(sport)
        if value in _RANK:
            out[sport] = {"impact": value, "source": "explicit"}
        else:
            out[sport] = {"impact": derived[sport], "source": AUTO}
    return out


def effective_impacts(concerns: list | None) -> dict[str, str]:
    """Strängaste påverkan per gren över alla besvär."""
    worst = {s: "none" for s in SPORTS}
    for concern in concerns or []:
        if not isinstance(concern, dict):
            continue
        for sport, info in concern_impacts(concern).items():
            if _RANK[info["impact"]] > _RANK[worst[sport]]:
                worst[sport] = info["impact"]
    return worst


def affecting(concerns: list | None, sports_: list[str]) -> list[dict]:
    """Besvär som påverkar någon av adeptens aktiva grenar, för texten till adepten."""
    out = []
    for concern in concerns or []:
        if not isinstance(concern, dict):
            continue
        hit = {
            s: info["impact"] for s, info in concern_impacts(concern).items()
            if s in sports_ and info["impact"] != "none"
        }
        if hit:
            out.append({"name": concern.get("name") or "besvär",
                        "severity": _severity(concern), "impacts": hit})
    return out
