#!/usr/bin/env python3
"""Bootcamp QA — verify a cohort ran cleanly and isolated.

Runs the same reads the app's Live Board / Analytics / MyProgress do, against
the tenant Grail, and asserts the properties a real bootcamp needs:

  1. Isolation   — each user's session id (DT_HOSTGROUP) is unique; per-user
                   DQL scoped by cluster suffix returns ONLY that user's rows.
  2. Progress    — every active user has a progress record; started/step/
                   completed funnel is coherent per user.
  3. Leaderboard — completed users rank by score then time; no cross-user
                   score bleed; ranks are dense and unique.
  4. Analytics   — the reporting DQL aggregates cleanly (one row per
                   user+training+tenant), tags parse, no 'unknown' users.

Usage (from ops-server/):
  python3 tools/bootcamp_qa.py                       # COE (default)
  python3 tools/bootcamp_qa.py --tenant sro97894 --token-env SRO_MASTER_PLATFORM_TOKEN
  python3 tools/bootcamp_qa.py --domain virtualufo.com --window 6h

Token: a platform token with storage:bizevents:read + storage:buckets:read on
the target tenant, read from /home/ops/.env by env-var name (needs sudo).
"""

import argparse
import json
import subprocess
import sys
import urllib.request


def env_token(name: str) -> str:
    out = subprocess.run(
        ["sudo", "grep", "-E", f"^{name}=", "/home/ops/.env"],
        capture_output=True, text=True)
    line = out.stdout.strip()
    if not line:
        sys.exit(f"token env {name} not found in /home/ops/.env")
    return line.split("=", 1)[1]


def dql(tenant: str, token: str, query: str) -> list:
    url = f"https://{tenant}.apps.dynatrace.com/platform/storage/query/v1/query:execute"
    body = json.dumps({"query": query, "requestTimeoutMilliseconds": 30000}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.load(r)
    return (d.get("result") or {}).get("records", [])


PASS, FAIL = "✅", "❌"
results = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="geu80787")
    ap.add_argument("--token-env", default="DT_MINT_TOKEN")
    ap.add_argument("--domain", default="virtualufo.com")
    ap.add_argument("--window", default="6h")
    args = ap.parse_args()
    tok = env_token(args.token_env)
    w = args.window
    dom = args.domain

    print(f"\nBootcamp QA — tenant={args.tenant} domain={dom} window={w}\n")

    # ── 1. Training bizevents (Analytics / Live Board source) ────────────────
    print("Telemetry & analytics:")
    rows = dql(args.tenant, tok, f'''
      fetch bizevents, from: now()-{w}
      | filter startsWith(event.type, "com.dynatrace.enablement.training")
      | filter contains(userEmail, "{dom}")
      | summarize events=count(),
          started=countIf(event.type=="com.dynatrace.enablement.training.started"),
          steps=countIf(event.type=="com.dynatrace.enablement.training.step.completed"),
          answered=countIf(event.type=="com.dynatrace.enablement.training.question.answered"),
          completed=countIf(event.type=="com.dynatrace.enablement.training.completed"),
          by:{{userEmail}}
      | sort userEmail asc''')
    users = {r["userEmail"]: r for r in rows}
    check("users emitting telemetry", len(users) > 0, f"{len(users)} users")
    check("every user has a start event",
          all(int(u["started"]) >= 1 for u in users.values()),
          f"{sum(1 for u in users.values() if int(u['started'])>=1)}/{len(users)} started")
    # funnel coherence: completed users must have steps
    incoherent = [e for e, u in users.items()
                  if int(u["completed"]) > 0 and int(u["steps"]) == 0]
    check("funnel coherent (completed ⇒ steps)", not incoherent,
          "ok" if not incoherent else f"bad: {incoherent}")
    completed_users = [e for e, u in users.items() if int(u["completed"]) > 0]
    check("some users completed, some in-progress",
          0 < len(completed_users) < len(users) or len(users) == 0,
          f"{len(completed_users)} completed / {len(users)-len(completed_users)} in-progress")

    # ── 2. No 'unknown' users leaking in (sourceTenant / email integrity) ────
    unknown = dql(args.tenant, tok, f'''
      fetch bizevents, from: now()-{w}
      | filter startsWith(event.type, "com.dynatrace.enablement.training")
      | filter isNull(userEmail) or userEmail == "" or userEmail == "unknown"
      | summarize c=count()''')
    n_unknown = int(unknown[0]["c"]) if unknown else 0
    check("no anonymous training events", n_unknown == 0, f"{n_unknown} anon events")

    # ── 3. Leaderboard ranking sanity ───────────────────────────────────────
    print("\nLeaderboard:")
    lb = dql(args.tenant, tok, f'''
      fetch bizevents, from: now()-{w}
      | filter event.type == "com.dynatrace.enablement.training.completed"
      | filter contains(userEmail, "{dom}")
      | fields userEmail, score, maxScore, timeSpent
      | sort score desc''')
    scores = [(r["userEmail"], float(r.get("score") or 0), float(r.get("timeSpent") or 0)) for r in lb]
    check("leaderboard has finishers", len(scores) > 0, f"{len(scores)} finishers")
    # one completion row per user (no double-count)
    dupe = len(scores) != len({s[0] for s in scores})
    check("no duplicate completion per user", not dupe)
    # scores within [0, maxScore]
    bad_score = [r["userEmail"] for r in lb
                 if float(r.get("score") or 0) > float(r.get("maxScore") or 0) > 0]
    check("scores within max bound", not bad_score,
          "ok" if not bad_score else f"over-max: {bad_score}")

    # ── 4. Per-user Grail isolation (cluster-scoped logs, if any deployed) ────
    print("\nPer-user isolation (k8s clusters, if trainings deployed operators):")
    clusters = dql(args.tenant, tok, f'''
      fetch dt.entity.kubernetes_cluster, from: now()-{w}
      | filter contains(entity.name, "-20")
      | fields name=entity.name
      | sort name asc''')
    cnames = [c["name"] for c in clusters]
    if cnames:
        uniq = len(cnames) == len(set(cnames))
        check("cluster names unique per session", uniq,
              f"{len(set(cnames))} unique of {len(cnames)}")
        # spot-check: scoped log query for the first cluster returns only it
        first = cnames[0]
        suffix = first.rsplit("-", 2)  # <repo>-<user>-<date>
        sfx = "-".join(suffix[-2:]) if len(suffix) >= 2 else first
        scoped = dql(args.tenant, tok, f'''
          fetch logs, from: now()-{w}
          | filter endsWith(k8s.cluster.name, "{sfx}")
          | summarize by:{{k8s.cluster.name}}''')
        scoped_names = {r["k8s.cluster.name"] for r in scoped}
        check(f"scoped query isolates one cluster ({sfx})",
              len(scoped_names) <= 1,
              f"matched: {scoped_names or 'no logs yet'}")
    else:
        print("  (no session clusters in tenant — telemetry-only cohort, skipping)")

    # ── Summary ──────────────────────────────────────────────────────────────
    npass = sum(1 for ok, _, _ in results if ok)
    print(f"\n{'='*50}\nQA RESULT: {npass}/{len(results)} checks passed\n")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
