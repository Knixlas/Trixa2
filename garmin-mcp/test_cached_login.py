"""
test_cached_login.py
Forsoker logga in med cachade tokens. Bra for att felsoka utan att
behova trigga workflow-runs.
"""
import json
import os
import traceback
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin


def main():
    load_dotenv()
    token_path = Path(os.path.expanduser("~/.garminconnect/garmin_tokens.json"))

    if not token_path.exists():
        print(f"FAIL: {token_path} saknas - kor test_connection.py forst")
        return

    data = json.loads(token_path.read_text())
    print(f"di_token: {len(data['di_token'])} chars")
    print(f"di_refresh_token: {len(data['di_refresh_token'])} chars")

    try:
        api = Garmin(email=os.getenv("GARMIN_EMAIL"), password=os.getenv("GARMIN_PASSWORD"))
        print("Garmin() OK")
        print(f"  api.client type: {type(api.client).__name__}")
        print(f"  api.client.di_token initially: {api.client.di_token!r}")
    except Exception as e:
        print(f"FAIL i Garmin(): {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    try:
        api.client.di_token = data["di_token"]
        api.client.di_refresh_token = data["di_refresh_token"]
        print("Tilldelade tokens OK")
    except Exception as e:
        print(f"FAIL vid tilldelning: {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    try:
        profile = api.get_user_profile()
        print(f"get_user_profile OK: displayName={profile.get('displayName')!r}")
    except Exception as e:
        print(f"FAIL i get_user_profile: {type(e).__name__}: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
