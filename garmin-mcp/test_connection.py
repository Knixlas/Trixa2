"""
Snabbtest av Garmin Connect-åtkomst.

Kör efter installation för att verifiera att inloggning, token-caching och 
de viktigaste API-anropen fungerar – innan du kopplar in MCP-servern i 
Claude Desktop.

    python test_connection.py
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from garmin_client import GarminClient


def _check(label: str, fn) -> bool:
    print(f"  → {label} …", end=" ", flush=True)
    try:
        result = fn()
        print("OK")
        return True, result
    except Exception as e:  # noqa: BLE001
        print(f"FEL ({type(e).__name__}: {e})")
        return False, None


def main() -> int:
    load_dotenv()
    print("=" * 60)
    print("Garmin Connect – anslutningstest")
    print("=" * 60)

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        print("\n❌ GARMIN_EMAIL och GARMIN_PASSWORD saknas i .env")
        print("   Kopiera .env.example till .env och fyll i.")
        return 1

    print(f"\nE-post: {email}")
    print(f"Token-katalog: {os.getenv('GARMIN_TOKEN_DIR', '~/.garminconnect')}")
    print("\nKör tester …\n")

    client = GarminClient(
        email=email,
        password=password,
        token_dir=Path(os.getenv("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser(),
    )

    today = date.today().isoformat()
    passed = 0
    total = 0

    # 1. Login
    total += 1
    ok, _ = _check("Inloggning", lambda: client.api)
    if not ok:
        print("\n❌ Avbryter – kommer inte vidare utan inloggning.")
        return 1
    passed += 1

    # 2. User profile
    total += 1
    ok, profile = _check("Hämta användarprofil", client.api.get_user_profile)
    if ok and profile:
        passed += 1
        name = profile.get("displayName") or profile.get("fullName") or "?"
        print(f"     Namn: {name}")

    # 3. Latest activity
    total += 1
    ok, activities = _check(
        "Hämta senaste aktivitet", lambda: client.api.get_activities(0, 1)
    )
    if ok and activities:
        passed += 1
        a = activities[0]
        print(f"     Senaste: {a.get('activityName')} "
              f"({a.get('activityType', {}).get('typeKey')}) "
              f"– {a.get('startTimeLocal')}")

    # 4. Training readiness
    total += 1
    ok, _ = _check("Hämta training readiness", lambda: client.api.get_training_readiness(today))
    if ok:
        passed += 1

    # 5. HRV
    total += 1
    ok, _ = _check("Hämta HRV", lambda: client.api.get_hrv_data(today))
    if ok:
        passed += 1

    # 6. Sleep
    total += 1
    ok, _ = _check("Hämta sömndata", lambda: client.api.get_sleep_data(today))
    if ok:
        passed += 1

    # 7. VO2max
    total += 1
    ok, _ = _check("Hämta VO2max", lambda: client.api.get_max_metrics(today))
    if ok:
        passed += 1

    print()
    print("=" * 60)
    print(f"Resultat: {passed}/{total} tester gick igenom")
    print("=" * 60)

    if passed == total:
        print("\n✅ Allt fungerar. Du kan nu koppla in servern i Claude Desktop.")
        return 0
    elif passed >= 2:
        print("\n⚠️  Inloggning funkar men vissa endpoints fallerade.")
        print("   Det kan bero på att du inte har data för dagens datum")
        print("   (HRV/sömn/readiness kräver att klockan synkat under natten).")
        return 0
    else:
        print("\n❌ Större problem – kontrollera lösenord och MFA.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
