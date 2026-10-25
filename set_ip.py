"""Ensure the PRIMARY static IP on the Dhan account is IP_ADDRESS, using the token in cred.yml.

Reads the configured IPs first: skips if already correct, uses modify_ip if a
primary IP exists, otherwise set_ip.

Intended for cron, e.g. every 6 days:
    30 8 */6 * * cd /path/to/DhanAlgo && /usr/bin/python3 set_ip.py >> set_ip.log 2>&1
Run it after generate_token.py so the token is fresh.

Exit code 0 on success, 1 on failure.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import yaml
from dhanhq import DhanLogin

CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cred.yml")
IST = timezone(timedelta(hours=5, minutes=30))
IP_ADDRESS = "65.20.81.99"
IP_FLAG = "PRIMARY"


def configured_primary(login, token):
    """Return the currently configured primary IP ('' if none/unknown)."""
    resp = login.get_ip(token)
    print(f"Configured IPs: {resp}")
    if isinstance(resp, dict):
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return str(data.get("primaryIP") or "").strip()
    return ""


def is_failure(response):
    return isinstance(response, dict) and (
        str(response.get("status", "")).lower() in ("failure", "failed", "error")
        or bool(response.get("errorType"))
        or bool(response.get("error"))
    )


def main():
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] Ensuring {IP_FLAG} IP is {IP_ADDRESS}...")
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            cred = yaml.safe_load(f) or {}
        client_id = cred.get("clientid")
        token = cred.get("token")
        if not client_id or not token:
            raise ValueError("cred.yml needs 'clientid' and 'token' (run generate_token.py first)")

        login = DhanLogin(str(client_id))
        current = configured_primary(login, token)
        if current == IP_ADDRESS:
            print("Primary IP already set; nothing to do.")
            return 0
        if current:
            print(f"Modifying primary IP {current} -> {IP_ADDRESS}")
            response = login.modify_ip(token, IP_ADDRESS, IP_FLAG)
        else:
            print(f"Setting primary IP {IP_ADDRESS}")
            response = login.set_ip(token, IP_ADDRESS, IP_FLAG)
        print(response)
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    return 1 if is_failure(response) else 0


if __name__ == "__main__":
    sys.exit(main())
