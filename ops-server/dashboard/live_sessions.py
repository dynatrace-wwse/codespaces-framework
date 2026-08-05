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

import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

STATES = ("scheduled", "open", "running", "ended", "cancelled")

# TTL applied to the session keys when a session ends. The index entry is kept;
# listing tolerates expired members (hgetall returns {} → skip).
#
# 30 days, to match the completion record and the pad export (live_pad's
# EXPORT_TTL_SECONDS). It used to be 7, which meant the artefacts of a finished
# workshop outlived the workshop itself: the frozen results survived for a month
# while the session hash they are listed against disappeared after a week, so a
# trainer looking for a three-week-old cohort found nothing and the record was
# unreachable for its remaining 23 days.
SESSION_TTL_SECONDS = 30 * 24 * 3600


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


_TENANT_RUNTIME_SUFFIX = re.compile(r"^(https?://[a-z0-9]+)-\d{1,3}(\.)")


def normalize_tenant(tenant) -> str:
    """Canonical tenant-URL form for equality checks: trimmed, lowercased,
    no trailing slash (the app sends https://<env>.apps.dynatrace.com).

    The app-function runtime's getEnvironmentUrl() returns the environment with a
    numeric suffix — https://sro97894-1.apps.dynatrace.com — while the browser
    sends the bare id. Both name the same tenant, so the suffix is stripped;
    without that, one side joining from the browser and the other from a function
    compares unequal and provision-all reports a false "foreign-tenant" skip.
    """
    t = (tenant or "").strip().rstrip("/").lower()
    return _TENANT_RUNTIME_SUFFIX.sub(r"\1\2", t)


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
    missing.

    The roster MAY be empty (WS-2): every session gets a join code
    unconditionally and join-by-code APPENDS the joiner to the roster, so a
    code-only workshop is fully supported by the data model. The old
    "at least one valid email" rule rejected that legitimate case.
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
    return {"title": title, "trainingId": training_id,
            "trainerEmail": trainer, "roster": normalize_roster(roster)}


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


def initial_state(scheduled_at=None) -> str:
    """Always 'open' — a workshop is joinable by code the moment it exists.

    The separate "open registration" step (scheduled → open) was removed: it
    was redundant, since a created workshop is already joinable by its code.
    `scheduledAt`, if given, is now purely a display time and does NOT gate
    joining. `scheduled` remains a valid stored state only for sessions created
    before this change (back-compat); no new session is created in it."""
    return "open"


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


def join_error(state, email, roster, session=None):
    """Return (http_status, detail) blocking a join, or None when allowed.

    Joining is allowed in scheduled/open/running (late joiners OK).

    The trainer may always join their OWN workshop without being on the roster.
    A trainer is never on their own roster (the roster is the invite list), so
    the plain membership check 403'd them out of their own room — they could
    start it but never enter it. The trainer legitimately needs a seat: they
    provision their own environment (WS-4/RFE-D) and appear in the Virtual
    Classroom as an attendee.
    """
    if not on_roster(email, roster) and not is_trainer(email, session or {}):
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
    # delete = hard-remove, allowed ONLY before a workshop has started. The
    # target "deleted" is not a stored state (the entity + index are removed by
    # the endpoint); apply_transition only validates legality. running/ended/
    # cancelled are absent → apply_transition raises → endpoint returns 409.
    "delete": {"scheduled": ("deleted", True), "open": ("deleted", True)},
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

def is_listed(session, roster, email, tenant="") -> bool:
    """Listing filter: non-ended, non-cancelled sessions where the email is the
    trainer or on the roster.

    Tenant scoping (WS-1) applies to the TRAINER side only. A workshop belongs
    to the tenant it was created from (`ownerTenant`), so a trainer signed into
    another tenant no longer sees it under "your workshops" — that was the
    reported "admin of tenant 1 sees a workshop created in tenant 2".

    Learners are NEVER tenant-filtered: joining a workshop from whatever tenant
    the learner runs is the whole point of a cross-tenant workshop.

    Backward compatible in both directions: a session without `ownerTenant`
    (created before this change) and a caller that sends no tenant (older app)
    both keep the previous, unscoped behavior.
    """
    # Cancelled workshops are hidden alongside ended ones (RFE): a learner
    # should never see a cancelled workshop in their list. The trainer manages
    # cancellation from the board, not the list.
    if not session or session.get("state") in ("ended", "cancelled"):
        return False
    return is_member(session, roster, email, tenant)


def is_member(session, roster, email, tenant="") -> bool:
    """Whether this email belongs to this workshop at all, ignoring its state.

    The membership half of is_listed, split out so the past-workshop listing can
    reuse exactly the same rule. Duplicating it would let the two views disagree
    about who may see a workshop, which is a disclosure bug waiting to happen.
    """
    if not session:
        return False
    if on_roster(email, roster):
        return True
    if not is_trainer(email, session):
        return False
    owner = normalize_tenant(session.get("ownerTenant"))
    caller = normalize_tenant(tenant)
    return not (owner and caller) or owner == caller


def is_past(session, roster, email, tenant="") -> bool:
    """A finished workshop this email attended or hosted.

    Deliberately a SEPARATE view rather than a relaxation of is_listed. Ended
    workshops must stay out of the live surfaces — the home banner, the upcoming
    card, the classroom router all treat "listed" as "go here now" — but they
    must stop vanishing from the people who were in them, which is what
    is_listed's early return caused: a trainer pressed End and the workshop, its
    cohort, its scores and its questions were simply gone.
    """
    return bool(session) and session.get("state") in ("ended", "cancelled") \
        and is_member(session, roster, email, tenant)


# Optional workshop fields echoed in payloads only when stored on the hash —
# absent fields add no keys, keeping pre-workshop payloads byte-identical.
# repoUrl/branch resolve the content namespace for learners (WS-3): the stored
# trainingId is the Orbital CATALOG id, which is not a repo name. ownerTenant is
# deliberately NOT echoed — it is an internal scoping field (see is_listed).
_WORKSHOP_FIELDS = ("scheduledAt", "timezone", "durationMinutes", "maxSeats",
                    "cancelledAt", "repoUrl", "branch", "description",
                    "trainerStep", "unlockPath",
                    # Only set once a workshop has finished, so it costs live
                    # payloads nothing and dates the row in the past listing.
                    "endedAt")


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


TRAINER_ROLE = "trainer"
LEARNER_ROLE = "learner"


def roster_targets(roster, trainer_email, include_trainer) -> list[tuple[str, str]]:
    """(email, role) pairs that provision-all and the readiness board walk.

    WS-4: a trainer runs the lab alongside the cohort, so they get an
    environment and a board row of their own. Appended only when asked for and
    only when they are not already an invited learner — a trainer who put
    themselves on the roster stays a single 'learner' row rather than
    appearing twice.

    Roster order is sorted for a stable board; the trainer is always last.
    """
    targets = [(e, LEARNER_ROLE) for e in sorted(roster or ())]
    trainer = normalize_email(trainer_email)
    if include_trainer and trainer and trainer not in set(roster or ()):
        targets.append((trainer, TRAINER_ROLE))
    return targets


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


# ── Trainer pacing / "unlock path with mine" ─────────────────────────────────
#
# Solutions are trainer-only by default (RFE-E). That is right for the default
# case but it makes the trainer the ONLY unblock path, which does not survive a
# 100-seat room: one person cannot walk five stuck learners through five
# different steps at once.
#
# The pacing model instead ties learner solution visibility to where the TRAINER
# has got to. The trainer always sees every solution (trainers are not always
# experts on the content they deliver, and "Run solution" is how they recover a
# broken demo). When they turn "unlock path with mine" on, a learner may see the
# solution for any step the trainer has already MOVED PAST — strictly before the
# trainer's current step. So the trainer walking from 3 to 4 releases step 3, and
# step 4 stays sealed until they move to 5.
#
# The effect is that the answers trail the teaching by exactly one step: nobody
# can read ahead, and nobody is ever permanently stuck on something the room has
# already covered. A training that should never reveal answers simply leaves the
# toggle off.

PACING_FIELDS = ("trainerStep", "unlockPath")


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def pacing_state(session) -> dict:
    """The pacing fields of a session, normalized."""
    session = session or {}
    return {
        "trainerStep": _as_int(session.get("trainerStep"), 0),
        "unlockPath": str(session.get("unlockPath") or "") == "1",
    }


def solution_visible(step, session, is_trainer=False) -> bool:
    """May THIS viewer see the solution for `step` in this workshop?

    step is the learner's 1-based step number. The trainer always may. A learner
    may only when the toggle is on AND the trainer has moved strictly past that
    step.
    """
    if is_trainer:
        return True
    state = pacing_state(session)
    if not state["unlockPath"]:
        return False
    return _as_int(step, -1) < state["trainerStep"]


# ── Description (workshop RFE) ───────────────────────────────────────────────

# Long enough for "what you'll build and what you need to bring", short enough
# that it stays a summary and renders in a card without truncation logic.
DESCRIPTION_MAX_CHARS = 250


def clean_description(text) -> str:
    """Normalize a workshop description: collapse whitespace, hard cap.

    Truncates rather than rejecting — a description is a nicety, and losing a
    workshop create to a 251st character would be a poor trade.
    """
    collapsed = " ".join(str(text or "").split())
    return collapsed[:DESCRIPTION_MAX_CHARS]
