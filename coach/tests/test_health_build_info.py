"""/health bär vilken commit som kör.

"Appen svarar 200" säger att något kör, inte att det är det som just
mergades. Utan commit i svaret gick en deploy att verifiera bara när
ändringen råkade synas i den publika API-ytan — en ren template- eller
UI-ändring lämnade inget spår alls.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from trixa_api.main import _build_info  # noqa: E402


def _clear_railway(monkeypatch=None):
    for key in ("RAILWAY_GIT_COMMIT_SHA", "RAILWAY_GIT_BRANCH",
                "RAILWAY_DEPLOYMENT_ID"):
        os.environ.pop(key, None)


def test_railway_variabler_vinner():
    """I containern finns ingen .git — Railways variabler är enda källan."""
    os.environ["RAILWAY_GIT_COMMIT_SHA"] = "abcdef0123456789abcdef"
    os.environ["RAILWAY_GIT_BRANCH"] = "main"
    os.environ["RAILWAY_DEPLOYMENT_ID"] = "dep-42"
    try:
        info = _build_info()
    finally:
        _clear_railway()
    assert info == {"commit": "abcdef012345", "branch": "main",
                    "deployment": "dep-42"}


def test_lokalt_lases_sha_ur_git_katalogen():
    """Utan Railway-variabler ska utvecklarens egen commit synas, annars går
    det inte att se om den lokala servern kör det man tror."""
    _clear_railway()
    info = _build_info()
    assert info["commit"] and len(info["commit"]) == 12
    assert info["branch"]


def test_health_svarar_aven_utan_bygginfo(monkeypatch):
    """Hälsokollen får aldrig falla för att bygginfon saknas — Railway
    stänger av tjänsten om /health slutar svara."""
    _clear_railway()
    import trixa_api.main as main

    monkeypatch.setattr(main.Path, "read_text", _raise, raising=False)
    info = main._build_info()
    assert info["commit"] is None
    assert main.health()["status"] == "ok"


def _raise(*_args, **_kwargs):
    raise OSError("ingen .git här")
