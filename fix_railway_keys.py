#!/usr/bin/env python3
"""
fix_railway_keys.py — Reads the Coinbase JSON key file and sets Railway variables correctly.

Run from the apex-momentum-agent directory:
    cd ~/Documents/Claude/Projects/XRPL\ a/apex-momentum-agent
    python3 fix_railway_keys.py
"""
import json
import subprocess
import sys
from pathlib import Path


def find_latest_cdp_json():
    downloads = Path.home() / "Downloads"
    files = sorted(
        downloads.glob("cdp_api_key*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def validate_pem(private_key: str) -> bool:
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        load_pem_private_key(private_key.encode("utf-8"), password=None)
        return True
    except Exception as e:
        print(f"   ⚠️  Key validation: {e}")
        return False


def railway_set(key: str, value: str) -> bool:
    result = subprocess.run(
        ["railway", "variables", "--set", f"{key}={value}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"   ✅ {key} set")
        return True
    else:
        print(f"   ❌ {key} failed: {result.stderr.strip() or result.stdout.strip()}")
        return False


def main():
    # ── 1. Find JSON file ──────────────────────────────────────────────────────
    json_file = find_latest_cdp_json()
    if not json_file:
        print("❌ No cdp_api_key*.json found in ~/Downloads")
        print("   Download your API key JSON from cloud.coinbase.com/access/api")
        sys.exit(1)

    print(f"📂 Using: {json_file.name}  ({json_file})")

    with open(json_file) as f:
        data = json.load(f)

    api_key = data.get("name", "").strip()
    private_key = data.get("privateKey", "").strip()

    if not api_key or not private_key:
        print("❌ JSON is missing 'name' or 'privateKey' field")
        sys.exit(1)

    # ── 2. Diagnose ────────────────────────────────────────────────────────────
    print(f"\n🔍 Key info:")
    print(f"   name        : {api_key}")
    first_line = private_key.split("\n")[0]
    print(f"   privateKey  : {first_line} ...")
    line_count = private_key.count("\n") + 1
    print(f"   line count  : {line_count}")

    print(f"\n🔍 Validating PEM with cryptography library...")
    valid = validate_pem(private_key)
    if valid:
        print("   ✅ PEM is valid")
    else:
        print("   ⚠️  PEM failed local validation — will still attempt Railway set")

    # ── 3. Format for Railway ──────────────────────────────────────────────────
    # Railway stores env vars as single-line strings.
    # Our bot.py converts literal \n → real newlines at startup.
    # So we store the key with LITERAL \n characters.
    api_secret_for_railway = private_key.replace("\n", "\\n")

    print(f"\n🚀 Setting Railway variables...")
    ok1 = railway_set("COINBASE_API_KEY", api_key)
    ok2 = railway_set("COINBASE_API_SECRET", api_secret_for_railway)

    # ── 4. Redeploy ────────────────────────────────────────────────────────────
    if ok1 and ok2:
        print("\n🚀 Redeploying to Railway...")
        subprocess.run(["railway", "up"], check=False)
        print("\n✅ Done! Check Railway logs in ~60 seconds.")
    else:
        print("\n❌ Some variables failed to set.")
        print("   Make sure you ran: railway login && railway link")
        print("\n📋 Fallback — paste these manually into Railway Variables:")
        print(f"\n   COINBASE_API_KEY:\n   {api_key}")
        print(f"\n   COINBASE_API_SECRET (literal \\n — do NOT press enter):")
        print(f"   {api_secret_for_railway[:80]}...")


if __name__ == "__main__":
    main()
