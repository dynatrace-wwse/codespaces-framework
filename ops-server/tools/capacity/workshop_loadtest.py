#!/usr/bin/env python3
"""End-to-end workshop load test — does delivery work CONSISTENTLY.

    sudo -E /home/ops/ops-venv/bin/python -m tools.capacity.workshop_loadtest \
        --seats 12 --training kubernetes-101

WHAT IT ASSERTS, AND WHY THESE FIVE
-----------------------------------
This is not a density test. Density was measured; the open question is whether
a workshop comes up the same way every time. So each assertion is about a
mechanism that has silently failed before:

  1. SIZED FROM UNITS      the machine count comes from the unit table, and the
                           workers advertise what the planner assumed. They did
                           not: two m6a.4xlarge advertised 30 while every
                           measurement said 20.
  2. ADMITTED IN PHASES    learners are admitted at the per-worker drip rate,
                           not in one burst. A burst is what produced every
                           capacity failure so far.
  3. ISOLATED              workshop learners land on workshop machines, and a
                           self-service learner started mid-workshop does not.
                           This failed once because provision-all did not pass
                           the workshop id, so pool routing saw an untagged
                           session and sent all eight to the daily worker.
  4. ACTUALLY CAME UP      every seat reaches a running session with tokens.
                           Sessions without tokens die at postCreateCommand in
                           about four seconds, which looks like a capacity
                           failure and is not one.
  5. TORN DOWN CLEAN       sessions end, machines go away, and the worker left
                           behind is healthy — no stuck docker waits, no orphan
                           job keys, no short pool. A mass teardown has twice
                           wedged a worker's container reaping.

Assertion 4 is the one that has never actually been exercised: previous runs
had no way to mint learner tokens, so the bots died before they reached the
code that assertion 5 is about. Tokens are minted here and revoked at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from provisioning.dt_token_provisioner import DTTokenProvisioner  # noqa: E402
from provisioning.token_specs import load_token_specs             # noqa: E402
from shared import capacity_units as cu                           # noqa: E402

ORBITAL = os.environ.get("ORBITAL_URL",
                         "https://autonomous-enablements.whydevslovedynatrace.com")
TENANT = os.environ.get("TENANT", "https://sro97894.apps.dynatrace.com")
TRAINER = os.environ.get("TRAINER", "loadtest-trainer@dynatrace.com")

RESULTS: list[tuple[str, bool, str]] = []


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    log(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def redis_cli(*args: str) -> str:
    pw = subprocess.run(["sudo", "grep", "-oP", r"^REDIS_PASSWORD=\K.*",
                         "/home/ops/.env"], capture_output=True, text=True
                        ).stdout.strip()
    out = subprocess.run(["redis-cli", "-a", pw, "--no-auth-warning", *args],
                         capture_output=True, text=True)
    return out.stdout.strip()


def orbital_token() -> str:
    return subprocess.run(["sudo", "grep", "-oP", r"^ORBITAL_TOKEN=\K.*",
                           "/home/ops/.env"], capture_output=True, text=True
                          ).stdout.strip().splitlines()[0]


async def workers(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{ORBITAL}/api/workers")
    return [w for w in r.json().get("workers", []) if w.get("role") != "master"]


async def running_jobs(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{ORBITAL}/api/builds/running")
    d = r.json()
    return d if isinstance(d, list) else d.get("running", d.get("builds", []))


def is_started(job: dict) -> bool:
    """A record is not a running session until a worker has claimed it.

    api_arena_provision writes worker_id="queued" before enqueueing, so a job
    parked behind the pacer appears in /api/builds/running immediately. Counting
    those reported 12/12 up eleven seconds after provision-all, which is faster
    than a k3d cluster can possibly start.
    """
    return (job.get("worker_id") or "") not in ("", "queued")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seats", type=int, default=12)
    ap.add_argument("--training", default="kubernetes-101")
    ap.add_argument("--repo", default="")
    ap.add_argument("--instance-type", default="m6a.4xlarge")
    ap.add_argument("--up-timeout", type=int, default=2400,
                    help="seconds to wait for every seat to be running")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--wait-for-pool", type=int, default=0, metavar="SECONDS",
                    help="wait this long for the control loop to give the workshop "
                         "its OWN machines before provisioning. 0 (default) "
                         "provisions immediately, which lands on the shared daily "
                         "pool because the loop binds the pool on a later tick.")
    args = ap.parse_args()

    minter = os.environ.get("SRO_MINTER_PLATFORM_TOKEN", "")
    if not minter:
        log("SRO_MINTER_PLATFORM_TOKEN unset — bots would die at "
            "postCreateCommand and this would measure nothing. Refusing.")
        return 2

    repo = args.repo or f"dynatrace-wwse/enablement-{args.training}"
    auth = {"Authorization": f"Bearer {orbital_token()}"}
    emails = [f"bot{i:02d}@loadtest.invalid" for i in range(args.seats)]

    async with httpx.AsyncClient(timeout=180) as client:
        # ── 1. what the model says this costs ───────────────────────────────
        units = cu.units_for_repo_static(repo)
        seats_each = cu.seats_per_instance(args.instance_type, units)
        machines = cu.instances_for_seats(args.seats, args.instance_type, units)
        log(f"unit model: {repo.split('/')[-1]} = {units} unit(s)/session, "
            f"{args.instance_type} = {cu.units_for_instance(args.instance_type)} "
            f"units -> {seats_each} seats each")
        log(f"  {args.seats} seats -> {machines} machine(s) (incl. 1 spare)")

        before = await workers(client)
        for w in before:
            log(f"  {w['worker_id']:28} pool={w.get('pool','?'):8} "
                f"{w.get('slots_ready')}/{w.get('slots_total')} "
                f"free={w.get('slots_free')} {w.get('status')}")

        expected = cu.units_for_instance(args.instance_type)
        advertised = {int(w.get("slots_total") or 0) for w in before
                      if w.get("pool", "daily") == "daily"}
        check("1. workers advertise what the planner assumed",
              advertised == {expected},
              f"advertised {sorted(advertised)}, model says {expected}")

        # ── 2. create the workshop and join ─────────────────────────────────
        start = (datetime.now(timezone.utc) + timedelta(minutes=2)
                 ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        r = await client.post(f"{ORBITAL}/api/live/sessions", headers=auth, json={
            "title": f"LOADTEST {datetime.now(timezone.utc):%H:%M}",
            "trainingId": args.training, "trainerEmail": TRAINER,
            "tenant": TENANT, "scheduledAt": start, "durationMinutes": 40,
            "roster": emails,
        })
        ws = r.json().get("sessionId", "")
        if not ws:
            log(f"could not create workshop: {r.status_code} {r.text[:300]}")
            return 1
        log(f"workshop {ws}, {args.seats} seats, starts {start}")

        joined = 0
        for email in emails:
            jr = await client.post(f"{ORBITAL}/api/live/sessions/{ws}/join",
                                   headers=auth,
                                   json={"email": email, "tenant": TENANT})
            # The join endpoint answers {"state":…, "joinedCount":N} — there is
            # no "joined" key. Matching for one reported 0/8 while all eight
            # had joined, which sent an operator hunting a bug that did not
            # exist.
            if "joinedCount" in jr.text:
                joined += 1
        log(f"joined {joined}/{args.seats}")
        if joined != args.seats:
            log("not everyone joined — provision-all skips the rest, and the "
                "run would measure nothing")
            return 1

        await client.post(f"{ORBITAL}/api/live/sessions/{ws}/start",
                          headers=auth, json={"trainerEmail": TRAINER})

        # ── 2b. optionally wait for this workshop's OWN machines ────────────
        # Without this the run measures the daily pool no matter what the control
        # loop does: the loop binds workshop -> pool when it launches, on its next
        # tick, and anything provisioned before that bind is already on the shared
        # arch queue. In production a workshop is scheduled hours out and prewarm
        # runs 45 minutes ahead, so the pool is ready long before any learner. A
        # test that provisions two minutes after creating the workshop exercises
        # the fallback instead, which is why every run so far reported pool
        # ['daily'] however well the loop behaved.
        if args.wait_for_pool:
            log(f"waiting up to {args.wait_for_pool}s for a dedicated pool "
                f"(control loop must be applying, not dry-run)")
            deadline = time.time() + args.wait_for_pool
            pool_name = ""
            while time.time() < deadline:
                rec = await client.get(f"{ORBITAL}/api/workshops/{ws}/fleet", headers=auth)
                body = rec.json() if rec.status_code == 200 else {}
                state, pool_name = body.get("state", ""), body.get("pool", "")
                ready = body.get("ready_workers", 0)
                log(f"  pool={pool_name or '(none yet)'} state={state or '(none)'} ready={ready}")
                if state == "ready":
                    break
                await asyncio.sleep(30)
            if not pool_name:
                log("no dedicated pool appeared — is CONTROL_LOOP_APPLY=1? Refusing, "
                    "because this run would silently measure the daily pool again.")
                return 1

        # ── 3. mint per learner, then provision all at once ─────────────────
        # The OAuth client is what mints `kind: platform` specs — the CI/CD workshop
        # needs one, and a provisioner built from the minter token alone refuses them.
        prov = DTTokenProvisioner(
            tenant_url=TENANT, api_token=minter,
            oauth_client_id=os.environ.get("SRO_CLIENT_ID", ""),
            oauth_client_secret=os.environ.get("SRO_CLIENT_SECRET", ""),
            oauth_resource=os.environ.get("SRO_RESOURCE", ""),
        )
        specs = await load_token_specs(repo)
        if any(s.kind == "platform" for s in specs) and not prov.can_mint_platform:
            log(f"{repo} declares a kind: platform token but SRO_CLIENT_ID/SECRET/RESOURCE\n"
                "are not set — the mint would fail. Refusing.")
            return 2
        per_user: dict[str, dict] = {}
        minted = []
        log(f"minting {args.seats} x {len(specs)} token(s)")
        for email in emails:
            tk = await prov.create_tokens(repo=repo, user_id=email,
                                          specs=specs, expires_in_hours=4)
            minted.append(tk)
            per_user[email] = {"dtEnv": tk.env, "dtTokenIds": tk.token_ids}

        jobs: dict[str, str] = {}
        try:
            t0 = time.time()
            pr = await client.post(
                f"{ORBITAL}/api/live/sessions/{ws}/provision-all",
                headers=auth,
                json={"trainerEmail": TRAINER, "tenant": TENANT,
                      "perUser": per_user})
            body = pr.json()
            statuses = Counter(x.get("status") for x in body.get("results", []))
            log(f"provision-all -> {dict(statuses)}")
            for x in body.get("results", []):
                if x.get("jobId"):
                    jobs[x["email"]] = x["jobId"]

            # ── 4. admission is a drip, not a burst ─────────────────────────
            target = f"queue:pool:{redis_cli('HGET', 'workshop:pools', ws)}" \
                if redis_cli("HGET", "workshop:pools", ws) else "queue:test:amd64"
            pend = int(redis_cli("LLEN", f"queue:pending:{target}") or 0)
            check("2. admitted in phases, not in one burst",
                  pend > 0 or len(jobs) <= 4,
                  f"{pend} parked behind the pacer on {target}")

            # ── 5. wait for every seat, watching where they land ────────────
            deadline = time.time() + args.up_timeout
            up: set[str] = set()
            last = -1
            while time.time() < deadline and len(up) < len(jobs):
                live = {j.get("job_id"): j for j in await running_jobs(client)
                        if is_started(j)}
                up = {j for j in jobs.values() if j in live}
                pend = int(redis_cli("LLEN", f"queue:pending:{target}") or 0)
                if len(up) != last:
                    log(f"  up {len(up)}/{len(jobs)}  pending {pend}  "
                        f"t+{time.time() - t0:.0f}s")
                    last = len(up)
                if len(up) == len(jobs):
                    break
                await asyncio.sleep(20)

            live = {j.get("job_id"): j for j in await running_jobs(client)
                    if is_started(j)}
            placement = Counter(live[j].get("worker_id", "?")
                                for j in jobs.values() if j in live)
            log(f"placement: {dict(placement)}")

            check("3. every seat came up",
                  len(up) == len(jobs),
                  f"{len(up)}/{len(jobs)} running after "
                  f"{time.time() - t0:.0f}s")

            pools_used = set()
            for w in await workers(client):
                if w["worker_id"] in placement:
                    pools_used.add(w.get("pool", "daily"))
            check("4. all seats landed in one pool",
                  len(pools_used) <= 1,
                  f"pools {sorted(pools_used) or ['(none)']}")

        finally:
            if args.keep:
                log(f"--keep: leaving workshop {ws} and {len(jobs)} session(s) up")
            else:
                log("ending workshop (terminates every session)")
                await client.post(f"{ORBITAL}/api/live/sessions/{ws}/end",
                                  headers=auth, json={"trainerEmail": TRAINER})
                # Teardown is asynchronous. Poll rather than guess a duration:
                # a fixed 90 s wait failed a run that was in fact fine, and
                # would equally have passed one that was not.
                deadline = time.time() + 420
                while time.time() < deadline:
                    after = await workers(client)
                    if all(int(w.get("active_jobs") or 0) == 0 for w in after):
                        break
                    await asyncio.sleep(15)
                after = await workers(client)
                for w in after:
                    log(f"  {w['worker_id']:28} "
                        f"{w.get('slots_ready')}/{w.get('slots_total')} "
                        f"free={w.get('slots_free')} active={w.get('active_jobs')} "
                        f"reaper_watching={w.get('reaper_watching')} "
                        f"{w.get('status')}")
                leftover = int(redis_cli("LLEN", f"queue:pending:{target}") or 0)
                healthy = all(
                    int(w.get("slots_free") or 0) == int(w.get("slots_total") or 0)
                    and int(w.get("active_jobs") or 0) == 0
                    for w in after)
                check("5. fleet healthy after teardown", healthy and leftover == 0,
                      f"{leftover} job(s) still parked; "
                      + ", ".join(f"{w['worker_id']} {w.get('slots_free')}/"
                                  f"{w.get('slots_total')} free"
                                  for w in after))

                for tk in minted:
                    await prov.revoke_tokens(tk.token_ids)
                log(f"revoked {sum(len(t.token_ids) for t in minted)} token(s)")

    print("\n" + "=" * 68)
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"\n        {detail}" if detail else ""))
    print("=" * 68)
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
