#!/usr/bin/env python3
"""Measure what one session of a training actually costs, in capacity units.

    sudo -E /home/ops/ops-venv/bin/python -m tools.capacity.measure_repo \
        --training astroshop-problems --count 3

WHY THIS EXISTS
---------------
``shared/capacity_units.REPO_UNITS`` decides how many machines a workshop buys.
Every entry in it that was not measured has been wrong, and wrong in the
expensive direction at least once. This is the only way to add an entry
honestly: bring real sessions up, with their labs deployed, and read the
worker's memory.

WHAT IT MEASURES, AND WHAT IT DOES NOT
--------------------------------------
It reads the worker's own ``mem_used_gb`` before and after, and divides by the
number of sessions that actually came up. That is *committed* memory — the
number capacity planning uses — and it deliberately excludes the transient
provisioning peak, which belongs to the per-slot limit, not to the plan.

Post-create numbers understate by roughly half: a Kubernetes-101 session goes
from 857 MiB to 1,609 MiB once its lab actually runs. So this waits for the
session to be RUNNING and then holds for ``--settle`` seconds before reading.
A measurement taken any earlier will oversell the box.

TOKENS
------
Sessions need real DT tokens or they die at ``postCreateCommand`` in about four
seconds, which looks exactly like a capacity failure and is not one. Tokens are
minted here with the same code path the app uses and revoked at the end, so a
run leaves nothing behind on the tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from provisioning.dt_token_provisioner import DTTokenProvisioner  # noqa: E402
from provisioning.token_specs import load_token_specs             # noqa: E402
from shared import capacity_units as cu                           # noqa: E402

ORBITAL = os.environ.get("ORBITAL_URL",
                         "https://autonomous-enablements.whydevslovedynatrace.com")
TENANT = os.environ.get("TENANT", "https://sro97894.apps.dynatrace.com")


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


async def workers(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{ORBITAL}/api/workers")
    return [w for w in r.json().get("workers", []) if w.get("role") != "master"]


def mem_used_mb(ws: list[dict]) -> float:
    return sum(float(w.get("mem_used_gb") or 0) for w in ws) * 1024


async def running_jobs(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{ORBITAL}/api/builds/running")
    d = r.json()
    return d if isinstance(d, list) else d.get("running", d.get("builds", []))


async def provision(client: httpx.AsyncClient, training: str, user: str,
                    dt_env: dict, hours: int) -> dict:
    r = await client.post(f"{ORBITAL}/api/arena/provision", json={
        "trainingId": training, "userId": user, "tenantUrl": TENANT,
        "dtEnv": dt_env, "sessionHours": hours,
    }, timeout=120)
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code} {r.text[:200]}"}
    return r.json()


async def terminate(client: httpx.AsyncClient, job_id: str) -> None:
    try:
        await client.post(f"{ORBITAL}/api/arena/sessions/{job_id}/terminate",
                          json={"userId": "measure-repo"}, timeout=60)
    except Exception as exc:
        log(f"  terminate {job_id} failed: {exc}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--training", required=True,
                    help="catalog id, e.g. astroshop-problems")
    ap.add_argument("--repo", default="",
                    help="repo name for token specs (default: derived)")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--settle", type=int, default=1800,
                    help="max seconds to wait for memory to plateau")
    ap.add_argument("--sample", type=int, default=60,
                    help="seconds between memory samples")
    ap.add_argument("--plateau-pct", type=float, default=2.0,
                    help="percent spread over 3 samples that counts as settled")
    ap.add_argument("--timeout", type=int, default=1500,
                    help="seconds to wait for sessions to come up")
    ap.add_argument("--keep", action="store_true",
                    help="leave sessions running (for manual inspection)")
    args = ap.parse_args()

    minter = os.environ.get("SRO_MINTER_PLATFORM_TOKEN", "")
    if not minter:
        log("SRO_MINTER_PLATFORM_TOKEN is not set — sessions would die at "
            "postCreateCommand and the run would measure nothing. Refusing.")
        return 2

    repo = args.repo or f"dynatrace-wwse/{args.training}"
    async with httpx.AsyncClient() as client:
        before_workers = await workers(client)
        baseline = mem_used_mb(before_workers)
        log(f"baseline memory across {len(before_workers)} worker(s): "
            f"{baseline:,.0f} MiB")
        for w in before_workers:
            log(f"  {w['worker_id']:28} {w.get('slots_ready')}/{w.get('slots_total')} "
                f"free={w.get('slots_free')} mem={float(w.get('mem_used_gb') or 0):.2f} GiB")

        # ── mint ────────────────────────────────────────────────────────────
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
        log(f"token specs for {repo}: {[s.env_var for s in specs]}")

        minted: list = []
        jobs: dict[str, str] = {}
        try:
            for i in range(args.count):
                user = f"measure{i:02d}@loadtest.invalid"
                tk = await prov.create_tokens(repo=repo, user_id=user,
                                              specs=specs, expires_in_hours=4)
                minted.append(tk)
                res = await provision(client, args.training, user, tk.env, 4)
                job_id = res.get("jobId", "")
                if job_id:
                    jobs[user] = job_id
                    log(f"  {user} -> job {job_id} "
                        f"{'(paced: waiting)' if res.get('paced') else ''}")
                else:
                    log(f"  {user} -> NO JOB: {json.dumps(res)[:200]}")

            if not jobs:
                log("nothing was queued — cannot measure")
                return 1

            # ── wait for them to actually be running ────────────────────────
            deadline = time.time() + args.timeout
            up: set[str] = set()
            while time.time() < deadline and len(up) < len(jobs):
                live = {j.get("job_id") for j in await running_jobs(client)}
                up = {j for j in jobs.values() if j in live}
                log(f"  running {len(up)}/{len(jobs)}")
                if len(up) == len(jobs):
                    break
                await asyncio.sleep(20)

            if not up:
                log("no session reached RUNNING — check the job logs, this is "
                    "not a capacity result")
                return 1

            # ── wait for memory to PLATEAU, not for a timer ─────────────────
            # A fixed hold measured Astroshop at 495 MiB/session with the
            # workers at 1.4% CPU — the containers existed, the labs had not
            # deployed, and the number was a third of what k8s-101 costs. "The
            # job is running" says nothing about whether k3d and the demo are
            # up. So the run ends when the worker's memory stops climbing, and
            # says so if it never plateaus.
            log(f"waiting for memory to plateau (sample {args.sample}s, "
                f"stable when 3 samples move <{args.plateau_pct}%, "
                f"max {args.settle}s)")
            history: list[float] = []
            plateau = False
            settle_deadline = time.time() + args.settle
            while time.time() < settle_deadline:
                await asyncio.sleep(args.sample)
                ws_now = await workers(client)
                used = mem_used_mb(ws_now)
                history.append(used)
                delta = ((used - history[-2]) / max(1.0, history[-2]) * 100
                         if len(history) > 1 else 100.0)
                cpu = max((float(w.get("cpu_pct") or 0) for w in ws_now),
                          default=0.0)
                log(f"  +{used - baseline:>7,.0f} MiB over baseline "
                    f"({delta:+.1f}% since last, peak cpu {cpu:.1f}%)")
                if len(history) >= 4:
                    window = history[-4:]
                    spread = (max(window) - min(window)) / max(1.0, min(window)) * 100
                    if spread < args.plateau_pct:
                        plateau = True
                        log(f"  plateau: {spread:.2f}% spread over "
                            f"{3 * args.sample}s")
                        break

            after = await workers(client)
            peak = mem_used_mb(after)
            if not plateau:
                log("WARNING: memory never plateaued inside --settle. The figure "
                    "below is a LOWER BOUND — the labs were still growing.")
            per_session = (peak - baseline) / len(up)
            units = max(1, -(-int(per_session) // cu.UNIT_MEMORY_MB))

            print("\n" + "=" * 68)
            print(f"  {args.training}  ({len(up)} session(s) measured)")
            print("=" * 68)
            print(f"  baseline                 {baseline:>10,.0f} MiB")
            print(f"  with sessions            {peak:>10,.0f} MiB")
            print(f"  per session (committed)  {per_session:>10,.0f} MiB")
            print(f"  unit size                {cu.UNIT_MEMORY_MB:>10,} MiB"
                  f"   ({cu.UNIT_DESCRIPTION})")
            print(f"  --> UNITS PER SESSION    {units:>10}")
            print()
            k8s = cu.units_for_repo_static(cu.UNIT_REFERENCE_REPO)
            print(f"  seats on m6a.4xlarge     "
                  f"{cu.seats_per_instance('m6a.4xlarge', units):>10}"
                  f"   (k8s-101 gets {cu.seats_per_instance('m6a.4xlarge', k8s)})")
            print(f"  slot memory limit        "
                  f"{cu.slot_memory_cap_mb(units):>10,} MiB")
            print()
            print("  Publish it (takes effect on the next control tick, no deploy):")
            print(f"    redis-cli HSET repo:units {repo.split('/')[-1]} "
                  f"'{{\"units\": {units}, \"measured_on\": "
                  f"\"{datetime.now(timezone.utc):%Y-%m-%d}\", \"measured_with\": "
                  f"\"{len(up)} sessions, "
                  f"{'plateaued' if plateau else 'NOT plateaued — lower bound'}"
                  f"\"}}'")
            print("=" * 68 + "\n")
            for w in after:
                log(f"  {w['worker_id']:28} active={w.get('active_jobs')} "
                    f"free={w.get('slots_free')} "
                    f"mem={float(w.get('mem_used_gb') or 0):.2f} GiB "
                    f"cpu={w.get('cpu_pct')}%")
            return 0
        finally:
            if args.keep:
                log(f"--keep: leaving {len(jobs)} session(s) and their tokens up")
            else:
                log(f"tearing down {len(jobs)} session(s)")
                for job_id in jobs.values():
                    await terminate(client, job_id)
                for tk in minted:
                    await prov.revoke_tokens(tk.token_ids)
                log("tokens revoked")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
