"""Tester för agent-API:t (/agent/*) — per-adept-token-scope.

Fejkad postgrest (insert/update/delete + filter). Verifierar att token låser
varje anrop till EN adept, att skriv-vägen taggar origin='nils' och normaliserar
sport, samt att revoke stänger åtkomsten.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

UID = "09db449d-b8fd-409a-b475-3401b0de9858"


class _R:
    def __init__(self, d):
        self.data = d


# Kolumner som FAKTISKT finns i Supabase, per tabell. Utan den här kontrollen
# svalde den fejkade klienten vilket fältnamn som helst — och lät ett anrop som
# skriver week_id/workout_id (kolumner som inte finns) passera som grönt test
# medan det kastade PGRST204 i drift. Tabeller som inte listas kontrolleras inte.
_REAL_COLUMNS = {
    "coach_overrides": {
        "id", "athlete_id", "coach_user_id", "scope", "week_start",
        "planned_session_id", "engine_recommendation", "override_decision",
        "motivation", "medical_context_disclosed", "athlete_explicit_request",
        "is_active", "created_at", "honored_by_planner", "honored_at",
    },
    "planned_sessions": {
        "id", "user_id", "date", "sport", "title", "workout_code", "intensity",
        "duration_min", "details", "purpose", "status", "origin", "created_at",
        "updated_at", "distance_km", "tp_workout_id", "steps", "exercises",
    },
    # exercise_code kom med migration 013 (progressionshistoriken behöver en
    # nyckel som överlever att katalogens namn skrivs om).
    "exercise_logs": {
        "id", "user_id", "session_date", "exercise_name", "exercise_code",
        "sets", "reps", "weight_from", "effort", "logged_at",
    },
}

# UNIQUE-constraints som databasen faktiskt upprätthåller.
_UNIQUE_KEYS = {
    "planned_sessions": ("user_id", "date", "sport"),
}


class UniqueViolation(Exception):
    """Speglar det postgrest kastar vid krock mot ett unikt index."""


def _check_columns(table, payload):
    known = _REAL_COLUMNS.get(table)
    if known is None:
        return
    unknown = set(payload) - known
    if unknown:
        raise KeyError(
            f"PGRST204: kolumnerna {sorted(unknown)} finns inte i {table}"
        )


class _Q:
    def __init__(self, t, st):
        self.t, self.st, self._f, self._u, self._ins, self._del, self._isnull = t, st, {}, None, None, False, None
        self._ranges: list = []

    def select(self, *a, **k):
        return self

    def eq(self, c, v):
        self._f[c] = v
        return self

    def is_(self, c, v):
        self._isnull = c
        return self

    def in_(self, c, v):
        self._f[c] = ("__in__", v)
        return self

    # Datumintervallen filtrerar på riktigt. Som no-ops (förut) returnerade
    # varje fönsterfråga hela tabellen, och sviten kunde inte se ett enda
    # gränsfel — t.ex. om planeraren cancel:ade grannveckans rader eller om
    # ett senare pass i veckan styrde ett tidigare (docs/12 I7).
    def gte(self, c, v):
        self._ranges.append((c, lambda x, v=v: x is not None and str(x) >= str(v)))
        return self

    def gt(self, c, v):
        self._ranges.append((c, lambda x, v=v: x is not None and str(x) > str(v)))
        return self

    def lte(self, c, v):
        self._ranges.append((c, lambda x, v=v: x is not None and str(x) <= str(v)))
        return self

    def lt(self, c, v):
        self._ranges.append((c, lambda x, v=v: x is not None and str(x) < str(v)))
        return self

    def neq(self, c, v):
        self._ranges.append((c, lambda x, v=v: x != v))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def update(self, d):
        self._u = d
        return self

    def insert(self, d):
        self._ins = d
        return self

    def delete(self):
        self._del = True
        return self

    def _match(self, r):
        for k, v in self._f.items():
            if isinstance(v, tuple) and v[0] == "__in__":
                if r.get(k) not in v[1]:
                    return False
            elif r.get(k) != v:
                return False
        if self._isnull and r.get(self._isnull) is not None:
            return False
        for col, pred in self._ranges:
            if not pred(r.get(col)):
                return False
        return True

    def execute(self):
        rows = self.st.setdefault(self.t, [])
        if self._ins is not None:
            _check_columns(self.t, self._ins)
            new = dict(self._ins)
            key = _UNIQUE_KEYS.get(self.t)
            if key and any(
                all(r.get(k) == new.get(k) for k in key) for r in rows
            ):
                raise UniqueViolation(
                    f"duplicate key value violates unique constraint på {self.t}{key}"
                )
            new.setdefault("id", f"id-{len(rows)+1}")
            rows.append(new)
            return _R([new])
        if self._u is not None:
            _check_columns(self.t, self._u)
            for r in rows:
                if self._match(r):
                    r.update(self._u)
            return _R([r for r in rows if self._match(r)])
        if self._del:
            self.st[self.t] = [r for r in rows if not self._match(r)]
            return _R([])
        return _R([r for r in rows if self._match(r)])


class _C:
    def __init__(self, st):
        self.st = st

    def table(self, n):
        return _Q(n, self.st)

    def schema(self, n):
        return self

    def from_(self, n):
        return _Q(n, self.st)


import datetime as _dt

_RECENT = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()


def _client_and_store():
    st = {
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Niklas"}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID, "garmin_athlete_id": "g1", "goal": "ironman"}],
        "planned_sessions": [],
        # Inom get_log:s 28-dagarsfönster. Ett fast juni-datum passerade bara
        # så länge faken ignorerade datumintervall (docs/12 I7).
        "training_log": [{"user_id": UID, "date": _RECENT, "sport": "Cykel", "duration_min": 60, "source": "tp"}],
        "coach_athletes": [{"athlete_id": UID, "coach_id": "coach1", "status": "accepted"}],
    }
    fake = _C(st)
    import coach.trixa.db as db
    import trixa_api.agent_auth as aa
    import trixa_api.agent_api as ag

    db.get_postgrest = lambda: fake
    aa.get_postgrest = lambda: fake
    ag.get_postgrest = lambda: fake
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ag.router)
    return TestClient(app, raise_server_exceptions=False), st, aa


def _with_token(st, aa):
    raw, h, prefix = aa.generate_token()
    st["api_tokens"].append({"id": "tok1", "user_id": UID, "name": "Nils",
                             "token_hash": h, "token_prefix": prefix, "revoked_at": None})
    return {"Authorization": f"Bearer {raw}"}


def _run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        return False
    print(f"✓ {name}")
    return True


def test_auth_required():
    c, st, aa = _client_and_store()
    assert c.get("/agent/whoami").status_code == 401
    assert c.get("/agent/whoami", headers={"Authorization": "Bearer trixa_nope"}).status_code == 401


def test_whoami_and_reads():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    assert c.get("/agent/whoami", headers=H).json()["user_id"] == UID
    assert c.get("/agent/athlete", headers=H).json()["goal"] == "ironman"
    assert len(c.get("/agent/log", headers=H).json()["sessions"]) == 1


def test_write_session_origin_and_sport():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/agent/plan/session", headers=H,
               json={"date": "2026-06-20", "sport": "bike", "title": "Z2", "duration_min": 90})
    assert r.json()["status"] == "ok", r.json()
    row = st["planned_sessions"][0]
    assert row["sport"] == "Cykel", row["sport"]      # engelska → svensk lagring
    assert row["origin"] == "nils", row["origin"]
    # upsert: samma dag+gren skrivs över, inte dubbleras
    c.post("/agent/plan/session", headers=H,
           json={"date": "2026-06-20", "sport": "bike", "title": "Ändrat"})
    assert len(st["planned_sessions"]) == 1
    assert st["planned_sessions"][0]["title"] == "Ändrat"


def test_delete_session_scoped():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    c.post("/agent/plan/session", headers=H,
           json={"date": "2026-06-20", "sport": "run", "title": "Löp"})
    sid = st["planned_sessions"][0]["id"]
    assert c.delete(f"/agent/plan/session/{sid}", headers=H).status_code == 200
    assert st["planned_sessions"] == []
    # okänt id → 404
    assert c.delete("/agent/plan/session/nope", headers=H).status_code == 404


def test_override():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/agent/override", headers=H, json={
        "scope": "volume", "engine_recommendation": {"h": 12},
        "override_decision": {"h": 10}, "motivation": "återhämtning krävs nu"})
    assert r.status_code == 200, r.text
    assert st["coach_overrides"][0]["athlete_id"] == "81b667bc"  # athlete_profiles.id, ej user_id
    assert st["coach_overrides"][0]["coach_user_id"] == "coach1"


def test_revoke_blocks():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    assert c.get("/agent/whoami", headers=H).status_code == 200
    st["api_tokens"][0]["revoked_at"] = "2026-06-15T00:00:00Z"
    assert c.get("/agent/whoami", headers=H).status_code == 401


if __name__ == "__main__":
    ok = True
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            ok &= _run(name, fn)
    print("\n✓ ALLT GRÖNT" if ok else "\n✗ NÅGOT FALLERADE")
    raise SystemExit(0 if ok else 1)
