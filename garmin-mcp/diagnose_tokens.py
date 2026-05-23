"""
diagnose_tokens.py
Djupare diagnostik: loggar in, gor ett API-anrop, och letar dar tokens
faktiskt hamnar (kan vara fordrojda till forsta requesten).
"""
import os
import json
from datetime import date

from dotenv import load_dotenv
import garth
from garminconnect import Garmin


def show(label, obj):
    print(f"\n--- {label} ---")
    print(f"type: {type(obj).__name__}")
    if obj is None:
        return
    # Visa attribut som kan innehalla tokens
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        if any(k in attr.lower() for k in ("oauth", "token", "sess", "cookie", "auth")):
            try:
                v = getattr(obj, attr)
                if callable(v):
                    continue
                t = type(v).__name__
                preview = ""
                if v is not None:
                    if hasattr(v, "__dict__"):
                        keys = [k for k in v.__dict__.keys() if not k.startswith("_")][:5]
                        preview = f" attrs={keys}"
                    elif isinstance(v, (str, bytes)):
                        preview = f" len={len(v)}"
                print(f"  .{attr}: {t}{preview}")
            except Exception as e:
                print(f"  .{attr}: ERROR {e}")


def main():
    load_dotenv()
    api = Garmin(
        email=os.getenv("GARMIN_EMAIL"),
        password=os.getenv("GARMIN_PASSWORD"),
        return_on_mfa=True,
    )
    result1, result2 = api.login()
    if result1 == "needs_mfa":
        mfa = input("MFA-kod: ").strip()
        api.resume_login(result2, mfa)
    print("\n=== Direkt efter login ===")
    show("api", api)
    show("api.client", api.client)
    show("api.client.sess", api.client.sess)
    show("garth (modul)", garth)
    show("garth.client", garth.client)

    # Forsok hamta data - kan vara att tokens fylls vid forsta requesten
    print("\n=== Anropar API for att triggera token-fyllning ===")
    try:
        profile = api.get_user_profile()
        print(f"  get_user_profile OK: displayName={profile.get('displayName')}")
    except Exception as e:
        print(f"  get_user_profile FAILED: {e}")

    print("\n=== Efter API-anrop ===")
    show("api.client", api.client)
    show("garth.client", garth.client)

    # Sok igenom HELA api.client.__dict__
    print("\n=== api.client.__dict__ raw ===")
    if hasattr(api.client, "__dict__"):
        for k, v in api.client.__dict__.items():
            t = type(v).__name__
            print(f"  {k}: {t}")
            if hasattr(v, "__dict__") and not callable(v):
                for k2, v2 in v.__dict__.items():
                    if not k2.startswith("_") and any(s in k2.lower() for s in ("oauth","token","cookie")):
                        print(f"    {k}.{k2}: {type(v2).__name__}")

    # Sok igenom api.client.sess.__dict__ - oauth-bibliotek brukar lagra dar
    if hasattr(api.client, "sess"):
        print("\n=== api.client.sess.__dict__ raw ===")
        sess = api.client.sess
        if hasattr(sess, "__dict__"):
            for k, v in sess.__dict__.items():
                t = type(v).__name__
                print(f"  {k}: {t}")


if __name__ == "__main__":
    main()
