#!/usr/bin/env python3
"""Bootcamp load test — N parallel training sessions + realistic telemetry.

Simulates a cohort: provisions N Orbital sessions (one per bot user) for a
training, waits for readiness, then emits the same training.* bizevents the
app sends (started / step.completed / question.answered / completed) straight
to the central tenant — so the Live Board, Analytics and per-user Grail
isolation can be verified under load without 100 humans.

Usage (from ops-server/, any python3 — stdlib only):
  python3 tools/bootcamp_loadtest.py --sessions 10                 # full run
  python3 tools/bootcamp_loadtest.py --emit-only --sessions 10     # telemetry only
  python3 tools/bootcamp_loadtest.py --terminate                   # tear down bots
  python3 tools/bootcamp_loadtest.py --status                      # bot session states

The bizevent token is the remote-grail COE platform token, decrypted from
/home/ops/.env (needs sudo) — the same credential the app's ingest path uses.
"""

import argparse
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request

ORBITAL = "https://autonomous-enablements.whydevslovedynatrace.com"
COE_APPS = "https://geu80787.apps.dynatrace.com"
INGEST = f"{COE_APPS}/platform/classic/environment-api/v2/bizevents/ingest"
TRAINING_ID = "kubernetes-101"
TRAINING_TITLE = "Kubernetes 101"
BOT_DOMAIN = "bootcamp.dev"
STEP_COUNT = 4
QUESTIONS = 5
READY_TIMEOUT_S = 20 * 60
POLL_INTERVAL_S = 20
STATE_FILE = "/tmp/bootcamp_loadtest_state.json"


def bot_email(i: int) -> str:
    return f"bot{i:02d}@{BOT_DOMAIN}"


def http_json(url: str, payload=None, bearer: str | None = None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
        return resp.status, (json.loads(body) if body.strip() else {})


def coe_token() -> str:
    """Decrypt the remote-grail COE platform token from /home/ops/.env."""
    code = (
        "from cryptography.fernet import Fernet\n"
        "import re\n"
        "env = open('/home/ops/.env').read()\n"
        "key = re.search(r'^GH_OAUTH_ENC_KEY=(.*)$', env, re.M).group(1).strip()\n"
        "enc = re.search(r'^REMOTE_GRAIL_COE_TOKEN_ENC=(.*)$', env, re.M).group(1).strip()\n"
        "print(Fernet(key.encode()).decrypt(enc.encode()).decode())\n"
    )
    out = subprocess.run(
        ["sudo", "/home/ops/ops-venv/bin/python", "-c", code],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(state: dict):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


# ── Provisioning ─────────────────────────────────────────────────────────────

def provision(n: int) -> dict:
    state = load_state()
    sessions = state.setdefault("sessions", {})
    for i in range(1, n + 1):
        email = bot_email(i)
        if email in sessions:
            print(f"  {email}: already tracked ({sessions[email]['jobId']})")
            continue
        try:
            status, body = http_json(f"{ORBITAL}/api/arena/provision", {
                "trainingId": TRAINING_ID,
                "userId": email,
                "tenantId": "geu80787",
                "tenantUrl": COE_APPS,
            })
            sessions[email] = {"jobId": body["jobId"], "dtSessionId": body.get("dtSessionId", "")}
            print(f"  {email}: {body['jobId']} dtSessionId={body.get('dtSessionId')}")
        except urllib.error.HTTPError as e:
            print(f"  {email}: HTTP {e.code} {e.read().decode()[:150]}")
        save_state(state)
        time.sleep(2)  # stagger — be kind to the queue
    return state


def wait_ready(state: dict):
    deadline = time.time() + READY_TIMEOUT_S
    pending = dict(state.get("sessions", {}))
    ready, failed = {}, {}
    while pending and time.time() < deadline:
        for email, s in list(pending.items()):
            try:
                _, body = http_json(f"{ORBITAL}/api/arena/sessions/{s['jobId']}")
            except Exception:
                continue
            st = body.get("status")
            if st in ("ready", "active"):
                ready[email] = s
                del pending[email]
                print(f"  READY {email} ({len(ready)} ready, {len(pending)} pending)")
            elif st in ("failed", "terminated", "expired"):
                failed[email] = st
                del pending[email]
                print(f"  {st.upper()} {email}")
        if pending:
            time.sleep(POLL_INTERVAL_S)
    for email in pending:
        failed[email] = "timeout"
    print(f"\nReady: {len(ready)}  Failed: {len(failed)} {failed if failed else ''}")
    return ready


# ── Telemetry ────────────────────────────────────────────────────────────────

def base_event(email: str, event_type: str, extra: dict) -> dict:
    return {
        "event.type": f"com.dynatrace.enablement.training.{event_type}",
        "userEmail": email,
        "trainingId": TRAINING_ID,
        "trainingTitle": TRAINING_TITLE,
        "trainingType": "lab",
        "trainingCategory": "hands-on",
        "tags": json.dumps(["kubernetes", "bootcamp-loadtest"]),
        "timestamp": None,  # filled at send time
        **extra,
    }


def emit(token: str, event: dict):
    event = dict(event)
    event["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    status, _ = http_json(INGEST, event, bearer=token)
    if status not in (200, 201, 202):
        print(f"    ingest HTTP {status} for {event['event.type']}")


def emit_cohort(n: int):
    """Emit a realistic funnel: everyone starts, most progress, ~70% finish."""
    token = coe_token()
    rng = random.Random(42)
    finishers = 0
    for i in range(1, n + 1):
        email = bot_email(i)
        print(f"  {email}:")
        emit(token, base_event(email, "started", {"stepCount": STEP_COUNT}))
        steps_done = STEP_COUNT if rng.random() < 0.7 else rng.randint(1, STEP_COUNT - 1)
        for s in range(1, steps_done + 1):
            emit(token, base_event(email, "step.completed", {
                "stepIndex": s - 1, "stepCount": STEP_COUNT, "completedSteps": s,
            }))
            time.sleep(0.2)
        correct = 0
        for q in range(QUESTIONS if steps_done == STEP_COUNT else rng.randint(0, 3)):
            ok = rng.random() < 0.8
            correct += ok
            emit(token, base_event(email, "question.answered", {
                "questionIndex": q, "correct": bool(ok), "points": 10 if ok else 0,
            }))
            time.sleep(0.1)
        if steps_done == STEP_COUNT:
            finishers += 1
            score = correct * 10 + rng.randint(0, 9)
            emit(token, base_event(email, "completed", {
                "score": min(score, QUESTIONS * 10), "maxScore": QUESTIONS * 10,
                "timeSpent": rng.randint(300, 1500),
                "completedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "stepCount": STEP_COUNT, "completedSteps": STEP_COUNT,
            }))
            print(f"    completed score={min(score, QUESTIONS*10)}/{QUESTIONS*10}")
        else:
            print(f"    in-progress at step {steps_done}/{STEP_COUNT}")
    print(f"\nEmitted funnel for {n} bots — {finishers} completed, {n - finishers} in progress")


# ── Teardown / status ────────────────────────────────────────────────────────

def terminate():
    state = load_state()
    for email, s in state.get("sessions", {}).items():
        try:
            status, body = http_json(
                f"{ORBITAL}/api/arena/sessions/{s['jobId']}/terminate", {})
            print(f"  {email}: {body.get('status', status)}")
        except urllib.error.HTTPError as e:
            print(f"  {email}: HTTP {e.code}")
        time.sleep(1)
    save_state({})
    print("State cleared.")


def status():
    state = load_state()
    for email, s in state.get("sessions", {}).items():
        try:
            _, body = http_json(f"{ORBITAL}/api/arena/sessions/{s['jobId']}")
            print(f"  {email}: {body.get('status')} {s['jobId']} {s.get('dtSessionId','')}")
        except Exception as e:
            print(f"  {email}: ? ({e})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--emit-only", action="store_true",
                    help="skip provisioning; just emit the telemetry funnel")
    ap.add_argument("--no-emit", action="store_true",
                    help="provision + wait only; skip telemetry")
    ap.add_argument("--terminate", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.terminate:
        return terminate()
    if args.status:
        return status()
    if not args.emit_only:
        print(f"Provisioning {args.sessions} bot sessions for {TRAINING_ID}…")
        state = provision(args.sessions)
        print("\nWaiting for readiness…")
        wait_ready(state)
    if not args.no_emit:
        print(f"\nEmitting telemetry funnel for {args.sessions} bots…")
        emit_cohort(args.sessions)


if __name__ == "__main__":
    sys.exit(main())
