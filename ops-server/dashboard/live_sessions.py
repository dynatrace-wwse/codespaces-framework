"""Pure decision logic for live training sessions (bootcamp cohorts).

No Redis, no FastAPI — everything here is deterministic and unit-tested in
dashboard/test_live_sessions.py. The /api/live/* endpoints in app.py stay
thin: they read/write the Redis keys and delegate every decision here.

Redis model (docs/live-training-architecture.md, ops-server/CLAUDE.md):
  live:session:{id}         hash  title, trainingId, ref, trainerEmail,
                                  state (scheduled|open|running|ended|cancelled),
                                  createdAt, startedAt, endedAt
                                  + OPTIONAL workshop fields (EPIC-002):
                                  scheduledAt, timezone, durationMinutes,
                                  maxSeats, joinCode, cancelledAt
  live:session:{id}:roster  set   lowercase invited emails
  live:session:{id}:joined  hash  email -> ISO joinedAt
  live:session:{id}:tenants hash  email -> normalized tenant URL the learner
                                  joined FROM (cross-tenant workshops; absent
                                  for pre-fix joins — backward compatible)
  live:sessions:index       zset  sessionId scored by epoch createdAt
  live:joincode:{code}      str   sessionId (join-by-code lookup, code UPPER)
"""

import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

STATES = ("scheduled", "open", "running", "ended", "cancelled")

# TTL applied to the three session keys when a session ends — matches the
# job:final 7-day retention. The index entry is kept; listing tolerates
# expired members (hgetall returns {} → skip).
SESSION_TTL_SECONDS = 7 * 24 * 3600


# ── Emails ────────────────────────────────────────────────────────────────────

def normalize_email(email) -> str:
    """Canonical form used everywhere: trimmed + lowercased."""
    return (email or "").strip().lower()


def is_valid_email(email) -> bool:
    """Minimal server-side validity: non-empty and contains an '@'."""
    return bool(email) and "@" in email


def normalize_roster(emails) -> list[str]:
    """Normalize each email, drop invalid ones (no '@'), dedupe keeping order."""
    seen, out = set(), []
    for e in emails or []:
        n = normalize_email(e)
        if is_valid_email(n) and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ── Tenants (cross-tenant workshops) ─────────────────────────────────────────
#
# ROOT CONSTRAINT: the trainer's app instance can only mint tokens for ITS OWN
# tenant, so bulk provisioning can never provision a foreign-tenant learner
# correctly. The learner's tenant is captured at join time (app-function stamps
# it server-side); provision-all then only provisions same-tenant learners and
# reports the rest honestly — foreign learners provision on entry from their
# own tenant via the unchanged single-user flow.

FOREIGN_TENANT_MESSAGE = "provisions on entry from their own tenant"
NOT_JOINED_MESSAGE = "hasn't joined yet — will provision on entry"


def normalize_tenant(tenant) -> str:
    """Canonical tenant-URL form for equality checks: trimmed, lowercased,
    no trailing slash (the app sends https://<env>.apps.dynatrace.com)."""
    return (tenant or "").strip().rstrip("/").lower()


def provision_skip_status(has_joined, joined_tenant, workshop_tenant):
    """Decide whether provision-all may provision a roster email.

    Returns None → provision; otherwise the skip status string:
      "not-joined"     — never joined, tenant unknown (provisions on entry)
      "foreign-tenant" — joined from a DIFFERENT tenant than the workshop's
                         provisioning tenant (provisions on entry there)

    Backward compatible: a joined entry WITHOUT a recorded tenant (pre-fix
    join) or a missing workshop tenant keeps the legacy behavior (provision).
    """
    if not has_joined:
        return "not-joined"
    jt = normalize_tenant(joined_tenant)
    wt = normalize_tenant(workshop_tenant)
    if jt and wt and jt != wt:
        return "foreign-tenant"
    return None


def readiness_gap_state(has_joined, joined_tenant, trainer_tenant) -> str:
    """State for a roster email with NO running job and NO failed record.

    With the trainer's tenant supplied (updated app), be honest on the board:
      "not-joined" — the learner never joined (will provision on entry)
      "foreign"    — joined from another tenant (provisions on entry there)
      "none"       — joined same-tenant (or tenant unrecorded), simply not
                     provisioned yet
    Without a trainer tenant (legacy app) always "none" — the old contract.
    """
    tt = normalize_tenant(trainer_tenant)
    if not tt:
        return "none"
    if not has_joined:
        return "not-joined"
    jt = normalize_tenant(joined_tenant)
    if jt and jt != tt:
        return "foreign"
    return "none"


# ── Create validation ─────────────────────────────────────────────────────────

def validate_create(title, training_id, trainer_email, roster) -> dict:
    """Validate + normalize a create request.

    Raises ValueError (→ HTTP 400) when title/trainingId/trainerEmail is
    missing or the roster has no valid email after normalization.
    """
    title = (title or "").strip()
    training_id = (training_id or "").strip()
    trainer = normalize_email(trainer_email)
    if not title:
        raise ValueError("title is required")
    if not training_id:
        raise ValueError("trainingId is required")
    if not is_valid_email(trainer):
        raise ValueError("a valid trainerEmail is required")
    members = normalize_roster(roster)
    if not members:
        raise ValueError("roster must contain at least one valid email")
    return {"title": title, "trainingId": training_id,
            "trainerEmail": trainer, "roster": members}


# ── Workshop scheduling (EPIC-002) ────────────────────────────────────────────

def validate_schedule(scheduled_at, timezone_name, duration_minutes,
                      max_seats) -> dict:
    """Validate + normalize the OPTIONAL workshop scheduling fields.

    Returns storage-ready strings ('' = absent — the hash field is simply not
    written, keeping pre-workshop sessions byte-identical). Raises ValueError
    (→ HTTP 400) on a malformed value.
    """
    out = {"scheduledAt": "", "timezone": "", "durationMinutes": "",
           "maxSeats": ""}
    when = (scheduled_at or "").strip()
    if when:
        try:
            datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"scheduledAt is not a valid ISO8601 timestamp: '{when}'")
        out["scheduledAt"] = when
    tz = (timezone_name or "").strip()
    if tz:
        try:
            ZoneInfo(tz)
        except Exception:
            raise ValueError(f"timezone is not a valid IANA zone name: '{tz}'")
        out["timezone"] = tz
    for field, value in (("durationMinutes", duration_minutes),
                         ("maxSeats", max_seats)):
        if value in (None, "", 0):
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be an integer")
        if n < 0:
            raise ValueError(f"{field} must be >= 0")
        if n:
            out[field] = str(n)
    return out


def initial_state(scheduled_at) -> str:
    """Create WITH scheduledAt → 'scheduled'; without → 'open' (the
    pre-workshop behavior, so existing callers are unaffected)."""
    return "scheduled" if (scheduled_at or "").strip() else "open"


# ── Join codes ────────────────────────────────────────────────────────────────

# A-Z/2-9 minus confusables (I, L, O and the digits 0/1 they mimic).
JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 6


def generate_join_code(rng=secrets) -> str:
    """A 6-char join code from the confusable-free alphabet. Uniqueness is
    enforced by the caller via SET NX on live:joincode:{code}."""
    return "".join(rng.choice(JOIN_CODE_ALPHABET)
                   for _ in range(JOIN_CODE_LENGTH))


def normalize_join_code(code) -> str:
    """Canonical join-code form: trimmed + uppercased (codes are
    case-insensitive on input, stored/looked-up UPPER)."""
    return (code or "").strip().upper()


# ── Roster / trainer gating ───────────────────────────────────────────────────

def is_trainer(email, session) -> bool:
    """True when the caller-supplied email matches the stored trainerEmail.

    The orbital app-function proxy sends no X-Auth headers, so this match is
    the trainer gate (consistent with the open /api/arena/* endpoints)."""
    e = normalize_email(email)
    return bool(e) and e == normalize_email(session.get("trainerEmail"))


def on_roster(email, roster) -> bool:
    """True when the email is on the roster (stored lowercase)."""
    return normalize_email(email) in set(roster or ())


def join_error(state, email, roster):
    """Return (http_status, detail) blocking a join, or None when allowed.

    Joining is allowed in scheduled/open/running (late joiners OK)."""
    if not on_roster(email, roster):
        return 403, "email is not on the session roster"
    if state == "ended":
        return 409, "session has ended"
    if state == "cancelled":
        return 409, "session has been cancelled"
    return None


def join_by_code_error(state, email, roster, max_seats):
    """Return (http_status, detail) blocking a join-by-code, or None.

    Unlike join_error, the email need NOT be on the roster — joining by code
    APPENDS it. Already-rostered emails re-join idempotently and are never
    seat-blocked; new emails are rejected when maxSeats>0 and the roster is
    full."""
    if state == "ended":
        return 409, "session has ended"
    if state == "cancelled":
        return 409, "session has been cancelled"
    if state not in ("scheduled", "open", "running"):
        return 409, f"session is not joinable in state '{state}'"
    if on_roster(email, roster):
        return None
    if max_seats and len(roster or ()) >= max_seats:
        return 409, f"session is full ({max_seats} seats taken)"
    return None


# ── State transitions ─────────────────────────────────────────────────────────

# action -> {current_state: (new_state, changed)}. Absent = illegal.
_TRANSITIONS = {
    "open-registration": {"scheduled": ("open", True),
                          "open": ("open", False)},
    "start": {"scheduled": ("running", True), "open": ("running", True),
              "running": ("running", False)},
    "end":   {"open": ("ended", True), "running": ("ended", True),
              "ended": ("ended", False)},
    "cancel": {"scheduled": ("cancelled", True), "open": ("cancelled", True),
               "cancelled": ("cancelled", False)},
}


def apply_transition(state, action) -> tuple[str, bool]:
    """Return (new_state, changed) for a trainer action; changed=False means
    the action is an idempotent no-op. Raises ValueError on an illegal move
    (e.g. start after ended)."""
    table = _TRANSITIONS.get(action)
    if table is None:
        raise ValueError(f"unknown action '{action}'")
    if state not in table:
        raise ValueError(f"cannot {action} a session in state '{state}'")
    return table[state]


# ── Response shaping ──────────────────────────────────────────────────────────

def is_listed(session, roster, email) -> bool:
    """Listing filter: non-ended sessions where the email is the trainer or
    on the roster."""
    if not session or session.get("state") == "ended":
        return False
    return is_trainer(email, session) or on_roster(email, roster)


# Optional workshop fields echoed in payloads only when stored on the hash —
# absent fields add no keys, keeping pre-workshop payloads byte-identical.
_WORKSHOP_FIELDS = ("scheduledAt", "timezone", "durationMinutes", "maxSeats",
                    "cancelledAt")


def workshop_fields(session, email) -> dict:
    """The optional workshop (EPIC-002) fields of a session payload.

    Ints come back as ints; joinCode is included EXCLUSIVELY for the trainer,
    never for learners."""
    out = {}
    for field in _WORKSHOP_FIELDS:
        value = session.get(field, "")
        if value:
            out[field] = (int(value)
                          if field in ("durationMinutes", "maxSeats") else value)
    if session.get("joinCode") and is_trainer(email, session):
        out["joinCode"] = session["joinCode"]
    return out


def shape_summary(session_id, session, roster, joined, email) -> dict:
    """One item of GET /api/live/sessions?email= (learner + trainer lists)."""
    e = normalize_email(email)
    out = {
        "sessionId":    session_id,
        "title":        session.get("title", ""),
        "trainingId":   session.get("trainingId", ""),
        "state":        session.get("state", ""),
        "trainerEmail": session.get("trainerEmail", ""),
        "joinedCount":  len(joined or {}),
        "rosterCount":  len(roster or ()),
        "createdAt":    session.get("createdAt", ""),
        "startedAt":    session.get("startedAt", ""),
        "isTrainer":    is_trainer(e, session),
        "hasJoined":    e in (joined or {}),
    }
    out.update(workshop_fields(session, e))
    return out


def shape_detail(session_id, session, roster, joined, email) -> dict:
    """Full session state (GET /api/live/sessions/{id}).

    Everyone gets the scalar fields + joined/roster counts; the roster and
    the joined list (who + when) are only included for the trainer."""
    out = {
        "sessionId":    session_id,
        "title":        session.get("title", ""),
        "trainingId":   session.get("trainingId", ""),
        "ref":          session.get("ref", ""),
        "state":        session.get("state", ""),
        "trainerEmail": session.get("trainerEmail", ""),
        "createdAt":    session.get("createdAt", ""),
        "startedAt":    session.get("startedAt", ""),
        "endedAt":      session.get("endedAt", ""),
        "joinedCount":  len(joined or {}),
        "rosterCount":  len(roster or ()),
    }
    out.update(workshop_fields(session, email))
    if is_trainer(email, session):
        out["roster"] = sorted(roster or ())
        out["joined"] = [{"email": k, "joinedAt": v}
                         for k, v in sorted((joined or {}).items())]
    return out


# ── Provisioning readiness / capacity (EPIC-002) ─────────────────────────────

def readiness_state(meta, livelog) -> str:
    """Classify a running arena job for the trainer's readiness board — same
    contract as the session-status endpoint: 'ready' means the executor wrote
    "Daemon ready" to the livelog; unassigned worker means still 'queued'."""
    if livelog and "Daemon ready" in livelog:
        return "ready"
    if meta.get("worker_id") in ("queued", ""):
        return "queued"
    return "provisioning"


def failed_job_email(record, roster, training_id, since="") -> str | None:
    """Map a jobs:completed record to a roster email when it is a FAILED
    daemon job for this session's training, finished after `since` (the
    session's createdAt — older failures are unrelated). None otherwise.

    Arena daemon jobs carry the training id in nightly_run_id
    ("enablement-{trainingId}") and the learner email in requested_by."""
    if record.get("type") != "daemon" or record.get("status") != "failed":
        return None
    if record.get("nightly_run_id") != f"enablement-{training_id}":
        return None
    email = normalize_email(record.get("requested_by"))
    if email not in set(roster or ()):
        return None
    if since and record.get("finished_at", "") < since:
        return None
    return email


def capacity_summary(workers, active_counts, needed) -> dict:
    """GET /api/live/capacity payload from worker heartbeat hashes + per-worker
    active-job counts. Pure math — the endpoint only gathers the inputs."""
    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    items, total_capacity, total_active = [], 0, 0
    for worker in sorted(workers or [], key=lambda w: w.get("worker_id", "")):
        wid = worker.get("worker_id", "")
        capacity = _int(worker.get("capacity"))
        active = _int((active_counts or {}).get(wid))
        total_capacity += capacity
        total_active += active
        items.append({"id": wid, "capacity": capacity, "active": active})
    available = max(total_capacity - total_active, 0)
    needed = max(_int(needed), 0)
    return {"capacity": total_capacity, "active": total_active,
            "available": available, "needed": needed,
            "sufficient": available >= needed, "workers": items}
