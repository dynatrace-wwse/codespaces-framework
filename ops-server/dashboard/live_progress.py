"""Pure logic for the workshop progress proxy (PROG-1).

Cross-tenant workshop progress is NOT duplicated. Every training bizevent is
written exactly once, to the central COE tenant (a learner on a foreign tenant
forwards it there via the app's remote-grail routing; a learner on COE writes it
there directly). Orbital — which already holds a COE token with
`storage:bizevents:read` — queries COE with DQL on the trainer's behalf and
serves the result to whichever tenant asks. So the trainer sees the whole cohort
regardless of which tenants the learners are running on, and nothing is
double-counted.

This module is Redis-free and HTTP-free: it builds the DQL and shapes the
records. `app.py` owns the token, the HTTP call and the cache. Tested in
dashboard/test_live_progress.py.

Event shape (app: api/ingestTrainingEvent.function.ts + ui/.../trainingEvents.ts):
  event.type    com.dynatrace.enablement.training.{started,step.completed,
                question.answered,completed}
  userEmail     learner identity (the cross-tenant join key)
  trainingKey   namespace-free training id ("kubernetes-101") — see trainingRepo.ts
  workshopId    Orbital live-session id, stamped while the learner sits in a
                running workshop; absent for self-paced runs
  workshopName  workshop title (Analytics filters by it)
  sourceTenant  server-stamped tenant the event was fired from
"""

from datetime import datetime, timedelta, timezone

from .live_sessions import normalize_tenant

STATES = ("not-started", "in-progress", "completed")

STARTED = "com.dynatrace.enablement.training.started"
STEP_COMPLETED = "com.dynatrace.enablement.training.step.completed"
QUESTION_ANSWERED = "com.dynatrace.enablement.training.question.answered"
COMPLETED = "com.dynatrace.enablement.training.completed"

# Hard ceiling on records pulled per query — a 300-seat workshop with ~30
# sections tops out around 10k events; beyond that the board is unreadable
# anyway and the query cost is not worth it.
MAX_RECORDS = 20000


def escape_dql_string(value) -> str:
    """Escape a value for embedding in a DQL double-quoted string literal.

    Every string that reaches the query comes from a caller (session id, email,
    training id), so this is the injection boundary: backslash first, then the
    quote, then strip the control characters that would let a value break out of
    the literal across a line.
    """
    s = str(value or "")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return "".join(ch for ch in s if ch >= " " and ch != "\x7f")


def training_key(id_or_repo) -> str:
    """Collapse the catalog-id / repo-name namespaces to one key.

    Server-side mirror of trainingKey() in
    ui/app/training/utils/trainingRepo.ts — the app stamps the same value on
    every event, so the two MUST stay in step: "enablement-kubernetes-101",
    "lab-enablement-kubernetes-101" and "kubernetes-101" all key to
    "kubernetes-101".
    """
    s = str(id_or_repo or "").strip().lower()
    if s.startswith("lab-"):
        s = s[4:]
    if s.startswith("enablement-"):
        s = s[len("enablement-"):]
    return s


def since_timestamp(session, now=None, grace_hours=2, max_lookback_hours=72) -> str:
    """Lower time bound for the DQL, as an ISO8601 UTC string.

    Anchored on when the workshop began (startedAt, else scheduledAt, else
    createdAt) minus a grace window, because learners routinely open the
    training a little before the trainer hits start. Clamped to
    max_lookback_hours so an old, long-running workshop can never turn into an
    unbounded Grail scan; falls back to the same clamp when no usable
    timestamp is on the session.
    """
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(hours=max_lookback_hours)
    anchor = None
    for field in ("startedAt", "scheduledAt", "createdAt"):
        raw = str((session or {}).get(field) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        anchor = parsed - timedelta(hours=grace_hours)
        break
    if anchor is None or anchor < floor:
        anchor = floor
    return anchor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_progress_query(workshop_id, key_or_id, emails, since_iso) -> str:
    """DQL for one workshop's training events on COE.

    Matches on the stamped `workshopId` OR on (trainingKey + a roster email)
    since `since_iso`. The second arm is not redundant: a learner who opened the
    training before joining the workshop — or who is running an app version that
    predates the stamp — still shows up on the board, and the workshop's own
    start time bounds it so unrelated self-paced runs are not swept in.
    """
    wid = escape_dql_string(workshop_id)
    key = escape_dql_string(training_key(key_or_id))
    roster = [escape_dql_string(e) for e in (emails or []) if e]
    since = escape_dql_string(since_iso)

    match = f'workshopId == "{wid}"'
    if key and roster:
        addrs = ", ".join(f'"{e}"' for e in sorted(set(roster)))
        match = f'{match} or (trainingKey == "{key}" and in(userEmail, {{{addrs}}}))'

    return (
        f'fetch bizevents, from: toTimestamp("{since}")\n'
        f'| filter event.provider == "DynatraceEnablement"\n'
        f"| filter isNotNull(userEmail)\n"
        f"| filter {match}\n"
        f"| fields timestamp, eventType = event.type, userEmail, trainingId, trainingKey,\n"
        f"         workshopId, workshopName, sourceTenant, stepIndex, completedSteps,\n"
        f"         stepCount, score, maxScore, passed, startedAt, completedAt\n"
        f"| sort timestamp asc\n"
        f"| limit {MAX_RECORDS}"
    )


def _num(value):
    """Grail scalars arrive as int/float/str depending on ingest — normalize to
    int, or None when the field is absent or unparseable (never guess a 0: a
    missing stepCount must not read as "0 sections")."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    """Integer percentage, or None when the denominator is unknown/zero."""
    if part is None or not whole:
        return None
    return max(0, min(100, round(part * 100 / whole)))


def _email(value) -> str:
    return str(value or "").strip().lower()


def shape_progress(records, roster=None) -> dict:
    """Fold raw Grail records into one row per learner plus a summary.

    Every roster email gets a row even with zero events ("not-started") — a
    board that silently omits the people who haven't started is the one thing a
    trainer cannot act on. Attendees who joined by code and are not on the
    roster appear too, from their events alone.
    """
    rows: dict[str, dict] = {}

    def row(email: str) -> dict:
        return rows.setdefault(email, {
            "email": email, "state": "not-started",
            "startedAt": "", "completedAt": "", "lastEventAt": "",
            "completedSteps": None, "stepCount": None, "progressPct": None,
            "score": None, "maxScore": None, "scorePct": None,
            "questionsAnswered": 0, "questionsPassed": 0,
            "tenant": "", "workshopId": "", "workshopName": "",
        })

    for email in sorted({_email(e) for e in (roster or []) if _email(e)}):
        row(email)

    for rec in records or []:
        email = _email(rec.get("userEmail"))
        if not email:
            continue
        r = row(email)
        event_type = str(rec.get("eventType") or rec.get("event.type") or "")
        ts = str(rec.get("timestamp") or "")
        if ts > r["lastEventAt"]:
            r["lastEventAt"] = ts
        if rec.get("sourceTenant"):
            # Normalized so the board doesn't show sro97894-1 for a learner the
            # roster/join records as sro97894 (TEN-1).
            r["tenant"] = normalize_tenant(rec["sourceTenant"])
        if rec.get("workshopId"):
            r["workshopId"] = str(rec["workshopId"])
        if rec.get("workshopName"):
            r["workshopName"] = str(rec["workshopName"])

        if event_type == STARTED:
            started = str(rec.get("startedAt") or ts)
            # Keep the EARLIEST start: a retake must not reset the clock the
            # trainer is reading elapsed time from.
            if started and (not r["startedAt"] or started < r["startedAt"]):
                r["startedAt"] = started
        elif event_type == STEP_COMPLETED:
            done = _num(rec.get("completedSteps"))
            if done is None:
                idx = _num(rec.get("stepIndex"))
                done = idx + 1 if idx is not None else None
            if done is not None and (r["completedSteps"] is None or done > r["completedSteps"]):
                r["completedSteps"] = done
            total = _num(rec.get("stepCount"))
            if total:
                r["stepCount"] = total
        elif event_type == QUESTION_ANSWERED:
            r["questionsAnswered"] += 1
            if rec.get("passed") in (True, "true", "True", 1, "1"):
                r["questionsPassed"] += 1
        elif event_type == COMPLETED:
            completed = str(rec.get("completedAt") or ts)
            # Keep the LATEST completion (a retake supersedes the first pass).
            if completed >= r["completedAt"]:
                r["completedAt"] = completed
                r["score"] = _num(rec.get("score"))
                r["maxScore"] = _num(rec.get("maxScore"))
            total = _num(rec.get("stepCount"))
            if total:
                r["stepCount"] = total

    for r in rows.values():
        if r["completedAt"]:
            r["state"] = "completed"
            r["progressPct"] = 100
            # A completion means every section is done — the step events may be
            # missing (older app, or the learner jumped) but the fact is not.
            if r["stepCount"] and (r["completedSteps"] or 0) < r["stepCount"]:
                r["completedSteps"] = r["stepCount"]
        elif r["startedAt"] or r["completedSteps"] is not None or r["questionsAnswered"]:
            r["state"] = "in-progress"
            r["progressPct"] = _pct(r["completedSteps"], r["stepCount"])
        r["scorePct"] = _pct(r["score"], r["maxScore"])

    results = sorted(rows.values(), key=lambda x: x["email"])
    return {"results": results, "summary": summarize(results)}


def summarize(rows) -> dict:
    """Cohort headline for the board: how many are where, and how far along."""
    counts = {s: 0 for s in STATES}
    pcts = []
    for r in rows or []:
        counts[r.get("state", "not-started")] = counts.get(r.get("state", "not-started"), 0) + 1
        if r.get("state") == "completed":
            pcts.append(100)
        elif r.get("progressPct") is not None:
            pcts.append(r["progressPct"])
    return {
        "total": len(rows or []),
        "notStarted": counts["not-started"],
        "inProgress": counts["in-progress"],
        "completed": counts["completed"],
        # Averaged over learners whose progress is actually known — a cohort of
        # 10 where 2 have measurable progress must not read as 20% done.
        "avgProgressPct": round(sum(pcts) / len(pcts)) if pcts else None,
        "measured": len(pcts),
    }
