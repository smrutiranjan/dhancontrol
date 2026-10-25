"""Generate a fresh Dhan access token and store it in cred.yml.

Intended for cron, e.g. daily at 08:30 IST:
    30 8 * * * cd /path/to/DhanAlgo && /usr/bin/python3 generate_token.py >> token.log 2>&1
(If the server is not on IST, use CRON_TZ=Asia/Kolkata or convert the hour.)

Exit code 0 on success, 1 on failure.
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pyotp
import yaml
from dhanhq import DhanLogin

CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cred.yml")
IST = timezone(timedelta(hours=5, minutes=30))
RETRY_WAITS = [0, 3, 30, 60, 120]  # Dhan limits generation to once per 2 minutes


def load_cred():
    with open(CRED_FILE, "r", encoding="utf-8") as f:
        cred = yaml.safe_load(f)
    if not isinstance(cred, dict):
        raise ValueError("cred.yml must contain a YAML mapping")
    for key in ("clientid", "pin", "totp_secret"):
        if not cred.get(key):
            raise ValueError(f"cred.yml is missing '{key}'")
    return cred


def save_cred(cred):
    # Write to a temp file then replace, so a crash can't leave cred.yml truncated.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CRED_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(cred, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, CRED_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def generate(cred):
    login = DhanLogin(str(cred["clientid"]))
    last_err = None
    for wait in RETRY_WAITS:
        if wait:
            print(f"Waiting {wait}s before retry...")
            time.sleep(wait)
        try:
            totp = pyotp.TOTP(str(cred["totp_secret"])).now()
            data = login.generate_token(str(cred["pin"]), totp)
            if isinstance(data, dict) and data.get("accessToken"):
                return data
            last_err = data
        except Exception as e:
            last_err = str(e)
        print(f"Attempt failed: {last_err}")
    raise RuntimeError(f"Token generation failed: {last_err}")


def main():
    now = datetime.now(IST)
    print(f"[{now:%Y-%m-%d %H:%M:%S} IST] Generating Dhan token...")
    try:
        cred = load_cred()
        data = generate(cred)
        # Re-read so edits made to cred.yml during generation aren't overwritten.
        cred = load_cred()
        cred["token"] = data["accessToken"]
        cred.pop("token_renewed_at", None)
        cred["token_issued_at"] = now.strftime("%Y-%m-%d %H:%M:%S IST")
        save_cred(cred)
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    print("Token saved to cred.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
