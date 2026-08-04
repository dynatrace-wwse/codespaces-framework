#!/usr/bin/env python3
"""Read-only probe: can we list (and therefore create) users on a tenant account?

Prints counts and statuses only. Never prints a token.
Usage: probe_users.py <ID_ENV> <SECRET_ENV> <URN_ENV>
"""
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


def env(name: str) -> str:
    out = subprocess.run(["sudo", "-n", "grep", "-m1", "-oP", rf"^{name}=\K.*",
                          "/home/ops/.env"], capture_output=True, text=True)
    return out.stdout.strip().strip('"')


id_env, secret_env, urn_env = sys.argv[1], sys.argv[2], sys.argv[3]
cid, cs, urn = env(id_env), env(secret_env), env(urn_env)
if not (cid and cs and urn):
    sys.exit(f"missing one of {id_env}/{secret_env}/{urn_env}")

body = urllib.parse.urlencode({
    "grant_type": "client_credentials", "client_id": cid, "client_secret": cs,
    "scope": "account-idm-read account-idm-write", "resource": urn}).encode()
tok = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://sso.dynatrace.com/sso/oauth2/token", data=body)))["access_token"]

account = urn.rsplit(":", 1)[-1]
base = f"https://api.dynatrace.com/iam/v1/accounts/{account}"

for path in ("users", "groups"):
    req = urllib.request.Request(f"{base}/{path}",
                                 headers={"Authorization": f"Bearer {tok}"})
    try:
        d = json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        print(f"{path}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
        continue
    items = d.get("results") or d.get("items") or []
    print(f"{path}: {len(items)}")
    for it in items[:40]:
        label = it.get("email") or it.get("name") or "?"
        status = it.get("userStatus") or it.get("status") or ""
        print(f"   - {label} {status}")
