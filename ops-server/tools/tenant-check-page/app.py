#!/usr/bin/env python3
"""Tenant readiness check for the Dynatrace Enablement app.

A thin web wrapper around tools/check-tenant-setup.sh: the SE pastes their tenant URL
and account OAuth client, the server runs the same live capability probes the CLI runs,
and renders the PASS/FAIL checklist. Nothing the user submits is ever persisted; the
client secret is never logged, echoed, or written anywhere. One JSON audit line per run
(requester, tenant, client id, verdict, failed checks — never the secret) goes to stdout
so Cloud Logging keeps a follow-up trail.
"""
import base64
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

from flask import Flask, Response, render_template_string, request

app = Flask(__name__)

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check-tenant-setup.sh")
AUDIT_TOKEN = os.environ.get("AUDIT_TOKEN", "")

# Only Dynatrace SaaS tenant hosts — everything else the script talks to (SSO, account
# API) is hardcoded inside it. This is the SSRF gate: no user input may steer a request
# anywhere else.
TENANT_RE = re.compile(
    r"^https://[a-z][a-z0-9]{4,15}\."
    r"(apps\.dynatrace\.com|sprint\.apps\.dynatracelabs\.com|dev\.apps\.dynatracelabs\.com)/?$")
CLIENT_ID_RE = re.compile(r"^dt0s02\.[A-Z0-9]{6,12}$")
SECRET_RE = re.compile(r"^dt0s02\.[A-Z0-9]{6,12}\.[A-Z0-9]{40,90}$")
URN_RE = re.compile(r"^urn:dtaccount:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

ORBITAL_URL = os.environ.get(
    "ORBITAL_URL", "https://autonomous-enablements.whydevslovedynatrace.com")
_SCOPES_CACHE: dict = {"at": 0.0, "html": ""}


def _scope_panel_html() -> str:
    """The required-scope list, fetched from the gate that enforces it.

    Never falls back to a baked-in list. A stale list that LOOKS authoritative is the
    failure this whole change exists to remove — if the list cannot be fetched, say so.
    """
    now = time.time()
    if _SCOPES_CACHE["html"] and now - _SCOPES_CACHE["at"] < 300:
        return _SCOPES_CACHE["html"]
    try:
        with urllib.request.urlopen(
                f"{ORBITAL_URL}/api/deploy/preflight-scopes", timeout=10) as r:
            data = json.loads(r.read().decode())
        entries = data.get("scopes") or []
        if not entries:
            raise ValueError("empty scope list")
        rows = "\n".join(html.escape(" or ".join(e)) for e in entries)
        out = (f'<div class="grp">{len(entries)} scopes:</div><pre>{rows}</pre>')
    except Exception:
        out = ('<div class="note">The scope list could not be loaded from the '
               'verification service just now. Re-open this page in a moment — it is not '
               'shown from a local copy on purpose, because a stale list is worse than '
               'no list.</div>')
    _SCOPES_CACHE.update({"at": now, "html": out})
    return out



# Naive in-memory limits: enough to stop a scripted abuser on a page only ~70 people
# will ever use. Per-IP window + a global concurrency cap (each run holds an outbound
# probe for ~30 s).
RATE: dict[str, deque] = {}
RATE_LOCK = threading.Lock()
RUNNING = threading.Semaphore(4)
AUDIT: deque = deque(maxlen=500)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enablement app — tenant readiness check</title>
<style>
 body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;max-width:880px;margin:40px auto;padding:0 16px}
 h1{font-size:1.4rem} a{color:#9d9dff}
 form{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px;margin:20px 0}
 label{display:block;margin:12px 0 4px;font-size:.9rem;color:#8b949e}
 input{width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-family:ui-monospace,monospace}
 button{margin-top:16px;padding:10px 22px;border:0;border-radius:6px;background:#238636;color:#fff;font-size:1rem;cursor:pointer}
 button:disabled{background:#30363d}
 pre{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;overflow-x:auto;font-size:.85rem;line-height:1.5}
 .pass{color:#3fb950}.fail{color:#f85149}.skip{color:#d29922}
 .ready{color:#3fb950;font-weight:600;font-size:1.1rem}
 .notready{color:#f85149;font-weight:600;font-size:1.1rem}
 .note{background:#161b22;border-left:3px solid #d29922;padding:10px 14px;font-size:.85rem;color:#8b949e}
 details{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin:16px 0}
 summary{cursor:pointer;color:#9d9dff;font-weight:600}
 details pre{border:0;margin:8px 0 4px;padding:8px 12px}
 details .grp{color:#8b949e;font-size:.85rem;margin:10px 0 2px}
</style></head><body>
<h1>Enablement app — tenant readiness check</h1>
<p>Verifies your tenant + account OAuth client can do everything the Enablement app
needs, <b>before</b> you register it: mint per-learner tokens, install OneAgent/ActiveGate,
write settings, store training content. Runs live probes — a granted scope is not proof.</p>
<div class="note">Your client secret is used once, in memory, for this check only — never
logged, never stored, never shown to anyone. Every test credential the check creates is
deleted before the page responds. Prefer not to paste a secret into a web page? Run the
<a href="/check-tenant-setup.sh">same script yourself</a> — identical checks, identical verdict.</div>
<details>
<summary>The scopes your OAuth client needs (create it with all of them)</summary>
<div class="grp">In <a href="https://myaccount.dynatrace.com" target="_blank">myaccount.dynatrace.com</a>
&rarr; Identity &amp; access management &rarr; OAuth clients &rarr; New. Scopes cannot be added to an
existing client afterwards — if one is missing, create a new client.</div>
{{ scope_panel|safe }}
</details>
<form method="post" action="/check" id="f">
 <label>Tenant URL (prod or sprint)</label>
 <input name="tenant" placeholder="https://abc12345.apps.dynatrace.com" required>
 <label>OAuth client ID</label>
 <input name="client_id" placeholder="dt0s02.XXXXXXXX" required>
 <label>OAuth client secret</label>
 <input name="client_secret" type="password" placeholder="dt0s02.XXXXXXXX.…" required autocomplete="off">
 <label>Account URN (shown in Account Management when you created the client)</label>
 <input name="account_urn" placeholder="urn:dtaccount:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" required>
 <button id="b">Run the check (~30 s)</button>
</form>
{{ result|safe }}
<script>document.getElementById('f').addEventListener('submit',()=>{const b=document.getElementById('b');b.disabled=true;b.textContent='Checking… (~30 s)';});</script>
</body></html>"""


def _derive_email(cid: str, secret: str, urn: str, tenant: str) -> str:
    """The SSO bearer JWT carries the client creator's email — grab it so the page never
    has to ask.

    Uses a SCOPE-LESS client_credentials grant: it succeeds for any real client and needs
    no guess about which scopes this one holds. The previous version named two scopes and
    tried each in turn, which meant the page carried its own copy of scope strings — the
    same hardcoded-list problem that let the page and the gate drift apart.

    Best-effort: '?' on any failure, never blocks the check.
    """
    host = ("https://sso-sprint.dynatracelabs.com" if ".sprint." in tenant
            else "https://sso-dev.dynatracelabs.com" if ".dev." in tenant
            else "https://sso.dynatrace.com")
    try:
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": cid,
            "client_secret": secret}).encode()
        with urllib.request.urlopen(
                urllib.request.Request(host + "/sso/oauth2/token", data=data), timeout=15) as r:
            tok = json.load(r).get("access_token", "")
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("email") or claims.get("preferred_username") or "?"
    except Exception:
        return "?"

def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with RATE_LOCK:
        q = RATE.setdefault(ip, deque(maxlen=10))
        while q and now - q[0] > 300:
            q.popleft()
        if len(q) >= 5:
            return True
        q.append(now)
        return False


def _render(raw: str, exit_code: int) -> str:
    lines = []
    for ln in ANSI_RE.sub("", raw).splitlines():
        esc = ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if re.match(r"\s*PASS\b", ln):
            lines.append(f'<span class="pass">{esc}</span>')
        elif re.match(r"\s*FAIL\b", ln):
            lines.append(f'<span class="fail">{esc}</span>')
        elif re.match(r"\s*SKIP\b", ln):
            lines.append(f'<span class="skip">{esc}</span>')
        elif ln.startswith("READY"):
            lines.append(f'<span class="ready">{esc}</span>')
        elif ln.startswith("NOT READY"):
            lines.append(f'<span class="notready">{esc}</span>')
        else:
            lines.append(esc)
    verdict = ("✅ This tenant is ready — wait for the go-ahead mail before registering."
               if exit_code == 0 else
               "❌ Not ready yet — add the scopes marked FAIL to the OAuth client and re-run.")
    return f"<p>{verdict}</p><pre>{chr(10).join(lines)}</pre>"


@app.get("/")
def index():
    return render_template_string(
        PAGE, scope_panel=_scope_panel_html(), result="")


@app.get("/check-tenant-setup.sh")
def script():
    with open(SCRIPT, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/x-shellscript",
                        headers={"Content-Disposition": "attachment; filename=check-tenant-setup.sh"})


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/audit")
def audit():
    if not AUDIT_TOKEN or request.args.get("token") != AUDIT_TOKEN:
        return Response("not found", status=404)
    return Response(json.dumps(list(AUDIT), indent=1), mimetype="application/json")


@app.post("/check")
def check():
    ip = _client_ip()
    if _rate_limited(ip):
        return render_template_string(
        PAGE, scope_panel=_scope_panel_html(), result='<p class="notready">Too many checks from your address — '
                         'wait a few minutes and try again.</p>'), 429

    tenant = request.form.get("tenant", "").strip().rstrip("/")
    cid = request.form.get("client_id", "").strip()
    secret = request.form.get("client_secret", "").strip()
    urn = request.form.get("account_urn", "").strip()

    problems = []
    if not TENANT_RE.match(tenant + "/") and not TENANT_RE.match(tenant):
        problems.append("tenant must look like https://&lt;env&gt;.apps.dynatrace.com "
                        "or https://&lt;env&gt;.sprint.apps.dynatracelabs.com")
    if not CLIENT_ID_RE.match(cid):
        problems.append("client id must look like dt0s02.XXXXXXXX")
    if not SECRET_RE.match(secret):
        problems.append("client secret has an unexpected shape (dt0s02.XXXXXXXX.…)")
    if not URN_RE.match(urn):
        problems.append("account URN must look like urn:dtaccount:&lt;uuid&gt;")
    if problems:
        return render_template_string(
        PAGE, scope_panel=_scope_panel_html(), result='<p class="notready">Fix these first:</p><ul><li>'
                         + "</li><li>".join(problems) + "</li></ul>"), 400

    if not RUNNING.acquire(timeout=5):
        return render_template_string(
        PAGE, scope_panel=_scope_panel_html(), result='<p class="notready">The checker is busy — try again in a '
                         'minute.</p>'), 503
    try:
        proc = subprocess.run(
            ["bash", SCRIPT, cid, secret, urn, tenant],
            capture_output=True, text=True, timeout=180)
        out = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        out, proc = "The check timed out after 180 s — tenant unreachable?", None
    finally:
        RUNNING.release()

    exit_code = proc.returncode if proc else 1
    email = _derive_email(cid, secret, urn, tenant)
    clean = ANSI_RE.sub("", out)
    failed = [re.sub(r"\s*<--.*$", "", ln.strip()[4:].strip())
              for ln in clean.splitlines() if ln.strip().startswith("FAIL")]
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "email": email, "tenant": tenant, "clientId": cid, "ip": ip,
        "ready": exit_code == 0, "failed": failed,
    }
    AUDIT.append(record)
    print("AUDIT " + json.dumps(record), flush=True)
    return render_template_string(
        PAGE, scope_panel=_scope_panel_html(), result=_render(out, exit_code))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
