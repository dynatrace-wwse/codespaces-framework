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

Every emitted bizevent is stamped with ``sourceTenant`` (mirrors the app's
server-side stamp in ingestTrainingEvent.function.ts) so tenant-scoped boards
can filter rehearsal traffic. Default is the COE apps domain; for an SRO
rehearsal pass ``--tenant https://sro97894.apps.dynatrace.com``.

Teardown (``--terminate``) is state-file independent: it drains queued bot
provisions from ``queue:test:amd64`` first (so freed slots can't backfill),
then terminates every live bot session discovered via
``/api/arena/active-sessions`` — plus anything the state file still tracks.
The state file is per-invoking-user (``/tmp/bootcamp_loadtest_state-$USER.json``,
legacy shared path read as fallback) so runs by different users can't collide
on file permissions.
"""

import argparse
import getpass
import os
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ORBITAL = "https://autonomous-enablements.whydevslovedynatrace.com"
COE_APPS = "https://geu80787.apps.dynatrace.com"
# Tenant the training bizevents go to. Default COE (where the Live Board reads).
# For a clean single-tenant SRO simulation, set INGEST_TENANT + INGEST_TOKEN_ENV
# so progress doesn't aggregate across tenants for a recycled identity.
INGEST_TENANT = os.environ.get("INGEST_TENANT", "geu80787")
INGEST_TOKEN_ENV = os.environ.get("INGEST_TOKEN_ENV", "")  # env-var name in /home/ops/.env; empty = decrypt COE remote-grail token
INGEST = f"https://{INGEST_TENANT}.apps.dynatrace.com/platform/classic/environment-api/v2/bizevents/ingest"
TRAINING_ID = "kubernetes-101"
TRAINING_TITLE = "Kubernetes 101"
BOT_DOMAIN = os.environ.get("BOT_DOMAIN", "bootcamp.dev")
BOT_PREFIX = os.environ.get("BOT_PREFIX", "bot")
STEP_COUNT = 4
QUESTIONS = 5
READY_TIMEOUT_S = 20 * 60
POLL_INTERVAL_S = 20
PROVISION_QUEUE = "queue:test:amd64"
# Highest bot index probed by --terminate's live-session scan (env-overridable).
TERMINATE_SCAN_MAX = int(os.environ.get("BOT_SCAN_MAX", "100"))


def state_file_for(user: str) -> str:
    """Per-user state path — avoids cross-user /tmp permission collisions."""
    return f"/tmp/bootcamp_loadtest_state-{user}.json"


STATE_FILE = state_file_for(getpass.getuser())
LEGACY_STATE_FILE = "/tmp/bootcamp_loadtest_state.json"  # pre-per-user path, read-only fallback


def bot_email(i: int) -> str:
    return f"{BOT_PREFIX}{i:02d}@{BOT_DOMAIN}"


BOT_EMAIL_RE = re.compile(rf"^{re.escape(BOT_PREFIX)}\d+@{re.escape(BOT_DOMAIN)}$")


def is_bot_email(email) -> bool:
    """True for load-test bot identities (bot\\d+@bootcamp.dev by default)."""
    return bool(BOT_EMAIL_RE.match((email or "").strip().lower()))


def queue_entry_bot_email(raw):
    """Bot email owning a queued provision payload, or None.

    Queued arena jobs (see api_arena_provision) carry the learner email in
    ``requested_by``. Non-JSON entries and non-bot jobs return None — those
    entries must never be touched (other users' jobs share the queue).
    """
    try:
        j = json.loads(raw)
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    email = (j.get("requested_by") or "").strip().lower()
    return email if is_bot_email(email) else None


def http_json(url: str, payload=None, bearer: str | None = None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
        return resp.status, (json.loads(body) if body.strip() else {})


_ORBITAL_BEARER: str | None = None


def orbital_bearer() -> str:
    """Service bearer for Orbital's /api/arena/* endpoints (arena auth).

    ORBITAL_TOKEN from the environment wins; otherwise sudo-read it from
    /home/ops/.env (same pattern as coe_token below). Cached. Empty result =
    legacy caller: Orbital logs ARENA-LEGACY-CALLER during the compat window
    and 401s once ARENA_AUTH_ENFORCE=1.
    """
    global _ORBITAL_BEARER
    if _ORBITAL_BEARER is None:
        tok = os.environ.get("ORBITAL_TOKEN", "")
        if not tok:
            try:
                out = subprocess.run(
                    ["sudo", "-n", "grep", "-E", "^ORBITAL_TOKEN=", "/home/ops/.env"],
                    capture_output=True, text=True)
                if out.returncode == 0 and "=" in out.stdout:
                    tok = out.stdout.strip().split("=", 1)[1]
            except Exception:
                tok = ""
        _ORBITAL_BEARER = tok.split(",")[0].strip()
    return _ORBITAL_BEARER


def coe_token() -> str:
    """Ingest token for INGEST_TENANT.

    If INGEST_TOKEN_ENV is set, read that plaintext env var from /home/ops/.env
    (e.g. SRO_MASTER_PLATFORM_TOKEN for an SRO run). Otherwise decrypt the COE
    remote-grail platform token (default, matches the Live Board's read tenant).
    """
    if INGEST_TOKEN_ENV:
        out = subprocess.run(["sudo", "grep", "-E", f"^{INGEST_TOKEN_ENV}=", "/home/ops/.env"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip().split("=", 1)[1]
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
    for path in (STATE_FILE, LEGACY_STATE_FILE):
        try:
            return json.load(open(path))
        except Exception:
            continue
    return {}


def save_state(state: dict):
    """Best-effort persist — a stale/foreign-owned /tmp file must not crash a run."""
    try:
        json.dump(state, open(STATE_FILE, "w"), indent=2)
    except Exception as e:
        print(f"  WARNING: could not write state file {STATE_FILE}: {e} — continuing without it")


# ── Provisioning ─────────────────────────────────────────────────────────────

def reconcile_tracked(state_sessions: dict, live: dict) -> tuple[dict, list]:
    """Drop tracked sessions Orbital no longer knows about.

    Teardown already treats Orbital as authoritative (``live_bot_sessions``);
    provisioning used to trust the state file alone, which made a stale file a
    silent no-op: every bot reported "already tracked", nothing was provisioned,
    and the run failed minutes later with every session EXPIRED. The legacy
    shared ``/tmp`` path made this worse — a file from a previous *week*, owned
    by another user, was read as a fallback and could never be written back.

    Returns ``(kept, dropped_emails)``.
    """
    live_ids = {jid for ids in (live or {}).values() for jid in ids}
    kept, dropped = {}, []
    for email, s in (state_sessions or {}).items():
        if (s or {}).get("jobId") in live_ids:
            kept[email] = s
        else:
            dropped.append(email)
    return kept, dropped


def provision(n: int) -> dict:
    state = load_state()
    sessions = state.setdefault("sessions", {})
    if sessions:
        try:
            kept, dropped = reconcile_tracked(sessions, live_bot_sessions(max(n, len(sessions))))
        except Exception as e:  # Orbital unreachable — trust the file rather than double-provision
            print(f"  WARNING: could not reconcile state against Orbital ({e}) — trusting state file")
        else:
            if dropped:
                print(f"  discarding {len(dropped)} tracked session(s) Orbital no longer has: "
                      f"{', '.join(sorted(dropped))}")
            state["sessions"] = sessions = kept
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
            }, bearer=orbital_bearer())
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
                _, body = http_json(f"{ORBITAL}/api/arena/sessions/{s['jobId']}",
                                    bearer=orbital_bearer())
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

def base_event(email: str, event_type: str, extra: dict, tenant: str = COE_APPS) -> dict:
    return {
        "event.type": f"com.dynatrace.enablement.training.{event_type}",
        "userEmail": email,
        "trainingId": TRAINING_ID,
        "trainingTitle": TRAINING_TITLE,
        "trainingType": "lab",
        "trainingCategory": "hands-on",
        # The app's ingest function stamps sourceTenant server-side on every
        # event (ingestTrainingEvent.function.ts) — tenant-scoped boards (SRO)
        # filter on it, so rehearsal traffic must carry it too (--tenant flag).
        "sourceTenant": tenant,
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


def emit_cohort(n: int, tenant: str = COE_APPS):
    """Emit a realistic funnel: everyone starts, most progress, ~70% finish."""
    token = coe_token()
    rng = random.Random(42)
    finishers = 0
    for i in range(1, n + 1):
        email = bot_email(i)
        print(f"  {email}:")
        emit(token, base_event(email, "started", {"stepCount": STEP_COUNT}, tenant))
        steps_done = STEP_COUNT if rng.random() < 0.7 else rng.randint(1, STEP_COUNT - 1)
        for s in range(1, steps_done + 1):
            emit(token, base_event(email, "step.completed", {
                "stepIndex": s - 1, "stepCount": STEP_COUNT, "completedSteps": s,
            }, tenant))
            time.sleep(0.2)
        correct = 0
        for q in range(QUESTIONS if steps_done == STEP_COUNT else rng.randint(0, 3)):
            ok = rng.random() < 0.8
            correct += ok
            emit(token, base_event(email, "question.answered", {
                "questionIndex": q, "correct": bool(ok), "points": 10 if ok else 0,
            }, tenant))
            time.sleep(0.1)
        if steps_done == STEP_COUNT:
            finishers += 1
            score = correct * 10 + rng.randint(0, 9)
            emit(token, base_event(email, "completed", {
                "score": min(score, QUESTIONS * 10), "maxScore": QUESTIONS * 10,
                "timeSpent": rng.randint(300, 1500),
                "completedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "stepCount": STEP_COUNT, "completedSteps": STEP_COUNT,
            }, tenant))
            print(f"    completed score={min(score, QUESTIONS*10)}/{QUESTIONS*10}")
        else:
            print(f"    in-progress at step {steps_done}/{STEP_COUNT}")
    print(f"\nEmitted funnel for {n} bots — {finishers} completed, {n - finishers} in progress")


# ── Teardown / status ────────────────────────────────────────────────────────

def _redis_cli(*args):
    """Run redis-cli against the local (master) Redis, auth from env or /home/ops/.env."""
    pw = os.environ.get("REDIS_PASSWORD", "")
    if not pw:
        try:
            out = subprocess.run(
                ["sudo", "-n", "grep", "-E", "^REDIS_PASSWORD=", "/home/ops/.env"],
                capture_output=True, text=True)
            if out.returncode == 0 and "=" in out.stdout:
                pw = out.stdout.strip().split("=", 1)[1]
        except Exception:
            pw = ""
    env = dict(os.environ)
    if pw:
        env["REDISCLI_AUTH"] = pw  # avoids the -a plaintext-password warning
    return subprocess.run(["redis-cli", *args], capture_output=True, text=True, env=env)


def drain_bot_queue(queue: str = PROVISION_QUEUE) -> int:
    """Remove QUEUED bot provisions so freed slots can't backfill during drain.

    During a mass-terminate, every slot a terminated bot frees immediately pulls
    the next queued bot provision — termination never converges. Only entries
    whose ``requested_by`` matches the bot pattern are LREM'd; other users'
    queued jobs are never touched (never DEL the whole queue).
    """
    out = _redis_cli("lrange", queue, "0", "-1")
    if out.returncode != 0:
        print(f"  WARNING: cannot read {queue} ({out.stderr.strip()[:200] or 'redis-cli failed'})"
              f" — queued bot provisions (if any) NOT drained; they will backfill freed slots")
        return 0
    matched = [raw for raw in out.stdout.splitlines() if raw and queue_entry_bot_email(raw)]
    if not matched:
        print(f"  No queued bot provisions in {queue}.")
        return 0
    print(f"  WARNING: {len(matched)} queued bot provision(s) still in {queue} — "
          f"removing them BEFORE terminating so freed slots don't backfill")
    removed = 0
    for raw in matched:
        res = _redis_cli("lrem", queue, "1", raw)
        try:
            removed += int(res.stdout.strip() or 0) if res.returncode == 0 else 0
        except ValueError:
            pass
    print(f"  Removed {removed}/{len(matched)} queued bot provision(s) from {queue} "
          f"(other users' jobs untouched)")
    return removed


def live_bot_sessions(scan_max: int) -> dict:
    """Live bot sessions discovered from Orbital — state-file independent.

    A killed run leaves live sessions the (stale or unreadable) state file
    doesn't track, so teardown asks Orbital directly:
    GET /api/arena/active-sessions?userId=<bot email> for bot01..bot{scan_max}.
    Returns {email: [jobId, ...]}.
    """
    found = {}
    for i in range(1, scan_max + 1):
        email = bot_email(i)
        try:
            _, body = http_json(
                f"{ORBITAL}/api/arena/active-sessions?"
                + urllib.parse.urlencode({"userId": email}),
                bearer=orbital_bearer())
        except Exception:
            continue
        job_ids = [s["jobId"] for s in body.get("sessions", []) if s.get("jobId")]
        if job_ids:
            found[email] = job_ids
    return found


def merge_terminate_targets(live: dict, state_sessions: dict) -> list:
    """Ordered, de-duplicated (email, jobId) teardown targets.

    Union of the live Orbital scan (authoritative — survives a killed run whose
    state file is stale/partial) and the state file (catches sessions the scan
    missed, e.g. Orbital briefly unreachable for one probe). Live entries come
    first; a jobId tracked by both sources is terminated once.
    """
    targets = {}  # (email, jobId) -> None (ordered set)
    for email, job_ids in (live or {}).items():
        for jid in job_ids:
            targets[(email, jid)] = None
    for email, s in (state_sessions or {}).items():
        jid = (s or {}).get("jobId")
        if jid:
            targets[(email, jid)] = None
    return list(targets)


def terminate(scan_max: int = TERMINATE_SCAN_MAX):
    # 1. Queue first — otherwise every slot we free below pulls a queued bot
    #    provision and termination never converges (2026-08-03 load test).
    drain_bot_queue()

    # 2. Targets = live scan (authoritative) ∪ state file (may be stale/empty).
    print(f"  Scanning live sessions for {BOT_PREFIX}01..{BOT_PREFIX}{scan_max:02d}@{BOT_DOMAIN}…")
    targets = merge_terminate_targets(
        live_bot_sessions(scan_max), load_state().get("sessions", {}))
    if not targets:
        print("  No live or tracked bot sessions found.")
    for email, job_id in targets:
        try:
            status, body = http_json(
                f"{ORBITAL}/api/arena/sessions/{job_id}/terminate", {},
                bearer=orbital_bearer())
            print(f"  {email}: {body.get('status', status)} ({job_id})")
        except urllib.error.HTTPError as e:
            print(f"  {email}: HTTP {e.code} ({job_id})")
        except Exception as e:
            print(f"  {email}: {e} ({job_id})")
        time.sleep(1)
    save_state({})
    try:  # best-effort: clear the legacy shared file too so it can't resurface
        if os.path.exists(LEGACY_STATE_FILE):
            os.remove(LEGACY_STATE_FILE)
    except OSError:
        pass
    print("State cleared.")


def status():
    state = load_state()
    for email, s in state.get("sessions", {}).items():
        try:
            _, body = http_json(f"{ORBITAL}/api/arena/sessions/{s['jobId']}",
                                bearer=orbital_bearer())
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
    ap.add_argument("--terminate", action="store_true",
                    help="full bot teardown: FIRST removes queued bot provisions from "
                         f"{PROVISION_QUEUE} (only entries whose requested_by matches "
                         f"{BOT_PREFIX}<N>@{BOT_DOMAIN} — other users' queued jobs are "
                         "never touched) so freed slots don't backfill, THEN terminates "
                         "every live bot session found via /api/arena/active-sessions "
                         f"(scans bot 1..max(--sessions, {TERMINATE_SCAN_MAX}); override "
                         "with BOT_SCAN_MAX) plus anything in the state file, then "
                         "clears the state file")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--tenant", default=f"https://{INGEST_TENANT}.apps.dynatrace.com",
                    help="sourceTenant stamped on every emitted bizevent "
                         "(default: derived from INGEST_TENANT, i.e. the tenant the "
                         "events are ingested into; pass "
                         "https://sro97894.apps.dynatrace.com for an SRO rehearsal)")
    args = ap.parse_args()

    if args.terminate:
        return terminate(max(args.sessions, TERMINATE_SCAN_MAX))
    if args.status:
        return status()
    if not args.emit_only:
        print(f"Provisioning {args.sessions} bot sessions for {TRAINING_ID}…")
        state = provision(args.sessions)
        print("\nWaiting for readiness…")
        wait_ready(state)
    if not args.no_emit:
        print(f"\nEmitting telemetry funnel for {args.sessions} bots…")
        emit_cohort(args.sessions, args.tenant.rstrip("/"))


if __name__ == "__main__":
    sys.exit(main())
