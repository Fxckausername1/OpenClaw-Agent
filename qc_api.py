#!/usr/bin/env python3
"""QuantConnect API v2 client helper.

Auth per QC docs: SHA-256 hash of "API_TOKEN:unixtime", sent as
Basic base64("USER_ID:hash") + Timestamp header. The raw token never
travels over the wire.

Credentials: env QC_USER_ID / QC_API_TOKEN, or files
credentials/qc_userid.txt and credentials/qc_token.txt.

Usage:
  ./venv/bin/python qc_api.py authenticate          # verify credentials
  ./venv/bin/python qc_api.py projects              # list projects
"""
import sys
import json
from os import environ
from base64 import b64encode
from hashlib import sha256
from time import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
BASE_URL = "https://www.quantconnect.com/api/v2"


def _cred(env_key, file_name):
    v = environ.get(env_key)
    if v:
        return v.strip()
    p = ROOT / "credentials" / file_name
    if p.exists():
        return p.read_text().strip()
    return None


def get_headers():
    user_id = _cred("QC_USER_ID", "qc_userid.txt")
    api_token = _cred("QC_API_TOKEN", "qc_token.txt")
    if not user_id or not api_token:
        raise SystemExit("QC credentials missing: set QC_USER_ID/QC_API_TOKEN "
                         "or create credentials/qc_userid.txt + qc_token.txt")
    timestamp = f"{int(time())}"
    hashed = sha256(f"{api_token}:{timestamp}".encode()).hexdigest()
    auth = b64encode(f"{user_id}:{hashed}".encode()).decode("ascii")
    return {"Authorization": f"Basic {auth}", "Timestamp": timestamp}


def post(endpoint, payload=None):
    r = requests.post(f"{BASE_URL}/{endpoint.lstrip('/')}",
                      headers=get_headers(), json=payload or {}, timeout=60)
    try:
        return r.json()
    except Exception:
        return {"success": False, "status": r.status_code, "text": r.text[:300]}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "authenticate"
    if cmd == "authenticate":
        out = post("authenticate")
    elif cmd == "projects":
        out = post("projects/read")
    else:
        out = post(cmd)
    print(json.dumps(out, indent=2)[:2000])


if __name__ == "__main__":
    main()
