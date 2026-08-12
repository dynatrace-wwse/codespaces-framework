"""Pure decision logic for live training sessions (bootcamp cohorts).

No Redis, no FastAPI — everything here is deterministic and unit-tested in
dashboard/test_live_sessions.py. The /api/live/* endpoints in app.py stay
thin: they read/write the Redis keys and delegate every decision here.

Redis model (docs/live-training-architecture.md, ops-server/CLAUDE.md):
  live:session:{id}         hash  title, trainingId, ref, trainers (JSON list),
                                  state (scheduled|open|running|ended|cancelled),
                                  roomOpen, createdAt, startedAt, endedAt
                                  + OPTIONAL workshop fields (EPIC-002):
                                  scheduledAt, timezone, durationMinutes,
                                  maxSeats, joinCode, cancelledAt
                                  + pacing (EPIC-007): trainerStep, unlockPath,
                                  gateAhead, pacingBy, pacingAt
                                  + provisioning (PASS 2): provisionRequestedAt,
                                  provisionRequestedBy
  live:session:{id}:roster  set   lowercase invited emails
  live:session:{id}:joined  hash  email -> ISO joinedAt
  live:session:{id}:tenants hash  email -> normalized tenant URL the learner
                                  joined FROM (cross-tenant workshops; absent
                                  for pre-fix joins — backward compatible)
  live:session:{id}:provdone hash email -> how the trainer's provision request
                                  was settled (queued|already-active|failed).
                                  Absence + the session flag is what makes a
                                  request still pending, so a learner who
                                  arrives after the trainer clicked is still
                                  provisioned (cross-tenant, PASS 2)
  live:session:{id}:events  strm  audit trail of the workshop: who joined, who
                                  was requested, which tenant accepted and what
                                  happened. Capped at EVENTS_MAXLEN
  live:sessions:index       zset  sessionId scored by epoch createdAt
  live:joincode:{code}      str   sessionId (join-by-code lookup, code UPPER)

The key PREFIX stays `live:session:` deliberately (EPIC-007 §9): every key is
built by a handful of constructors in app.py, so renaming it to `ws:` would be
mechanical — but it also moves the terminate-all scanner, the expire fan-out and
~1,100 lines of pinned tests for no user-visible gain. What became
workshop-scoped is the ID and the route, not the storage prefix.
"""

import json
import re
import secrets
import time
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


# ── Workshop identity ─────────────────────────────────────────────────────────
#
# A workshop id is the primary key: it addresses the workshop in the URL
# (/live-sessions/{id}), in every API call, in the Redis keys and in the
# bizevents that carry workshopId to Grail. It is what makes two workshops of
# the SAME repo distinguishable — which is the whole point of EPIC-007, since
# the app used to route workshops by repo tail and therefore could not tell them
# apart.
#
# Shape: ws_{base36 epoch-ms}-{3 bytes hex}   →  ws_mshioahd-78ec1f
#
#   ws_       self-identifying: recognisable in a URL, a log line, a Redis scan,
#             and never confusable with a worker job id (which shares the base36
#             timestamp shape — see _new_job_id in app.py).
#   base36 ms lexically sortable by creation time — base36(epoch-ms) is 8 chars
#             wide for every realistic timestamp (and stays 8 until ~2059), so
#             string order is chronological order. Toy values in tests are not
#             fixed-width and do not have that property.
#   3 bytes   24 bits of entropy inside a single millisecond. The old generator
#             used 2 bytes; this is 256x the collision headroom.
#
# Globally unique by construction: one Orbital Redis serves every tenant, and no
# tenant, trainer or repo is encoded in the id. Tenant scoping is a FIELD
# (ownerTenant), never part of the key — a workshop is reachable cross-tenant by
# design.

WORKSHOP_ID_PREFIX = "ws_"

_B36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(number: int) -> str:
    """Lowercase base36, no padding. 0 -> '0'."""
    if number <= 0:
        return "0"
    out = []
    while number:
        number, rem = divmod(number, 36)
        out.append(_B36_DIGITS[rem])
    return "".join(reversed(out))


def new_workshop_id(now_ms=None, rng=secrets) -> str:
    """Mint a workshop id. `now_ms`/`rng` are injectable for tests."""
    stamp = int(now_ms if now_ms is not None else time.time() * 1000)
    return f"{WORKSHOP_ID_PREFIX}{_b36(stamp)}-{rng.token_hex(3)}"


def is_workshop_id(value) -> bool:
    """True for ids minted by new_workshop_id(). Cheap route/param guard."""
    return isinstance(value, str) and value.startswith(WORKSHOP_ID_PREFIX)


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


# ── Trainer team ──────────────────────────────────────────────────────────────
#
# A workshop has up to MAX_TRAINERS trainers, stored as a JSON list in the
# `trainers` hash field. trainers[0] is the creator — the "lead" — and is only
# distinguished for display and for the derived `trainerEmail` echo; every
# trainer holds identical authority, including moving the class pacing pointer
# (last write wins, attributed via pacingBy).
#
# Trainer identity is workshop MEMBERSHIP, never instructors.json and never IAM:
# being named on this list is the only thing that makes someone a trainer of
# this workshop, and it grants nothing anywhere else.

MAX_TRAINERS = 5


def encode_trainers(trainers) -> str:
    """Serialize the team for the Redis hash field."""
    return json.dumps(list(trainers or []))


def trainers_of(session) -> list[str]:
    """
    Read the team off a session hash.

    Falls back to a bare `trainerEmail` when `trainers` is absent. That is not a
    compatibility shim for callers — it is a guard for the deploy window, where a
    record written by the previous build (or created between the Redis wipe and
    the service restart) would otherwise 500 every request that touches it.
    """
    raw = (session or {}).get("trainers") or ""
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
    if not isinstance(parsed, list):
        parsed = [(session or {}).get("trainerEmail") or ""]
    return normalize_roster(parsed)


def lead_trainer(session) -> str:
    """The creator. '' when the session has no readable team."""
    team = trainers_of(session)
    return team[0] if team else ""


def validate_trainers(trainer_email, trainers=None) -> list[str]:
    """
    Build the team from the creator plus any co-trainers.

    The creator is always index 0 whether or not the caller included them, so a
    trainer can never accidentally remove themselves from their own workshop.
    Raises ValueError on a missing creator or a team over the cap.
    """
    lead = normalize_email(trainer_email)
    if not is_valid_email(lead):
        raise ValueError("a valid trainerEmail is required")
    team = [lead] + [t for t in normalize_roster(trainers) if t != lead]
    if len(team) > MAX_TRAINERS:
        raise ValueError(f"a workshop may have at most {MAX_TRAINERS} trainers")
    return team


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


def readiness_gap_state(has_joined, joined_tenant, trainer_tenant,
                        requested=False) -> str:
    """State for a roster email with NO running job and NO failed record.

    With the trainer's tenant supplied (updated app), be honest on the board:
      "not-joined" — the learner never joined (will provision on arrival)
      "requested"  — the trainer asked for them; their own app instance has not
                     picked it up yet (see the provision-request section below)
      "foreign"    — joined from another tenant, no request outstanding
      "none"       — joined same-tenant (or tenant unrecorded), simply not
                     provisioned yet
    Without a trainer tenant (legacy app) always "none" — the old contract.

    "requested" outranks "foreign" because it is the more actionable fact: the
    trainer does not need to know which tenant a learner sits in, they need to
    know whether anything is still going to happen. It deliberately does NOT
    outrank "not-joined" — a learner who never arrived is the one case the
    trainer has to act on, and hiding that behind "requested" would make an
    empty room look like a busy one.
    """
    tt = normalize_tenant(trainer_tenant)
    if not tt:
        return "none"
    if not has_joined:
        return "not-joined"
    if requested:
        return "requested"
    jt = normalize_tenant(joined_tenant)
    if jt and jt != tt:
        return "foreign"
    return "none"


# ── Provision requests (cross-tenant, PASS 2) ────────────────────────────────
#
# The root constraint above means a trainer's app can never provision a
# foreign-tenant learner itself. The learner's OWN app instance can — it is the
# only thing that can mint in that tenant — so the trainer's intent is recorded
# here and PULLED by the learner's browser on its existing 10s session poll.
# Orbital still stores no tenant credential; it stores a flag.
#
# The intent is workshop-level (`provisionRequestedAt` on the session hash) and
# NOT a per-learner queue, so a learner who arrives after the trainer clicked is
# provisioned on arrival with no second click. The per-learner half is only a
# done-marker (`live:session:{id}:provdone`), which is what stops it repeating.

PROVISION_REQUESTED_MESSAGE = "requested — their own tenant will provision it"


def provision_request_pending(session, email, provision_done=None) -> bool:
    """Does this caller have an outstanding trainer provision request?

    Fires in scheduled/open/running — pre-start provisioning is the point: the
    trainer requests environments ahead of the workshop so learners start with
    zero delay, and each learner's own app instance picks the request up the
    next time it is open in their chosen tenant. NEVER after ended/cancelled —
    re-opening a finished workshop days later must not silently build a
    container for whoever loads the page.

    `provision_done` is required evidence, and `None` means "not looked up",
    which is NOT the same as `{}` ("looked up, nobody settled yet"). Answering
    True without having read the done-map would provision someone who was
    already provisioned, so the missing case fails closed.
    """
    if provision_done is None:
        return False
    if session.get("state", "") not in ("scheduled", "open", "running"):
        return False
    if not session.get("provisionRequestedAt", ""):
        return False
    who = normalize_email(email)
    if not who:
        return False
    return who not in provision_done


# ── Audit trail ──────────────────────────────────────────────────────────────
#
# One ordered record of everything that happens to a workshop, written to the
# Redis stream live:session:{id}:events (same XADD shape as :broadcast/:chat).
#
# It serves two callers, which is why it is one mechanism and not two: it is the
# durable log of the provisioning workflow, AND it is what the trainer's client
# reads to toast "someone just joined". A second, separate notification channel
# would drift out of step with the log it is supposed to reflect.

EVENT_JOINED = "joined"
EVENT_REGISTERED = "registered"
EVENT_PROVISION_REQUESTED = "provision-requested"
EVENT_PROVISION_ACCEPTED = "provision-accepted"
EVENT_PROVISION_STARTED = "provision-started"
EVENT_PROVISION_FAILED = "provision-failed"
EVENT_STARTED = "started"
EVENT_ENDED = "ended"

EVENT_KINDS = (
    EVENT_JOINED, EVENT_REGISTERED, EVENT_PROVISION_REQUESTED, EVENT_PROVISION_ACCEPTED,
    EVENT_PROVISION_STARTED, EVENT_PROVISION_FAILED, EVENT_STARTED, EVENT_ENDED,
)

# Cap the stream. A 300-seat workshop emits several events per learner, and an
# uncapped XADD grows for the 30-day life of the session keys.
EVENTS_MAXLEN = 2000


def audit_event(kind, email="", tenant="", actor="", detail="", now=None) -> dict:
    """Flat str->str fields for one XADD.

    Redis stream fields are strings, so everything is stringified here rather
    than at each of the seven call sites. Empty fields are dropped so a reader
    never has to distinguish "absent" from "empty string".
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown audit event kind: {kind}")
    fields = {
        "kind": kind,
        "ts": now or datetime.now().astimezone().isoformat(timespec="seconds"),
        "email": normalize_email(email),
        "tenant": normalize_tenant(tenant),
        "actor": normalize_email(actor),
        "detail": str(detail or "")[:200],
    }
    return {k: v for k, v in fields.items() if v}


def shape_events(entries) -> list[dict]:
    """Redis stream entries -> JSON rows, oldest first.

    `id` is the stream id, which the client sends back as `since` to page
    forward. That is what makes the trainer's arrival toast fire once per
    learner instead of once per poll.
    """
    out = []
    for entry_id, fields in entries or ():
        row = {"id": entry_id}
        row.update({k: v for k, v in (fields or {}).items()})
        out.append(row)
    return out


# ── Create validation ─────────────────────────────────────────────────────────

def validate_create(title, training_id, trainer_email, roster,
                    trainers=None) -> dict:
    """Validate + normalize a create request.

    Raises ValueError (→ HTTP 400) when title/trainingId/trainerEmail is
    missing, or when the trainer team exceeds MAX_TRAINERS.

    The roster MAY be empty (WS-2): every session gets a join code
    unconditionally and join-by-code APPENDS the joiner to the roster, so a
    code-only workshop is fully supported by the data model. The old
    "at least one valid email" rule rejected that legitimate case.

    `trainerEmail` in the result is the lead (== trainers[0]); it is kept so
    callers and payload consumers have a single name to display without having
    to reach into the list.
    """
    title = (title or "").strip()
    training_id = (training_id or "").strip()
    if not title:
        raise ValueError("title is required")
    if not training_id:
        raise ValueError("trainingId is required")
    team = validate_trainers(trainer_email, trainers)
    return {"title": title, "trainingId": training_id,
            "trainers": team, "trainerEmail": team[0],
            "roster": normalize_roster(roster)}


# ── Workshop scheduling (EPIC-002) ────────────────────────────────────────────
#
# MAX_SEATS is a FIELD BOUND, not a delivery guarantee. Nothing here proves the
# fleet can actually serve that many learners: capacity_summary() below is a
# blind sum of worker slots (WORKER_CAPACITY, default 6 per worker) and is
# repo-blind — no per-repo machine size, no seat cost. See EPIC-007 §8: a
# 200-seat workshop needs ~34 workers and the fleet runs 3. Callers MUST NOT
# present an accepted maxSeats as "this workshop can be delivered".

MAX_SEATS = 200

_FIELD_MAX = {"maxSeats": MAX_SEATS}


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
        limit = _FIELD_MAX.get(field)
        if limit and n > limit:
            raise ValueError(f"{field} must be <= {limit}")
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
    """True when the caller-supplied email is on this workshop's trainer team.

    Membership, not equality — a workshop has up to MAX_TRAINERS trainers and
    they all hold the same authority. Every trainer gate in app.py routes
    through here, so this one function is the whole multi-trainer change on the
    authorization side.

    The orbital app-function proxy sends no X-Auth headers, so this match is
    the trainer gate (consistent with the open /api/arena/* endpoints)."""
    e = normalize_email(email)
    return bool(e) and e in trainers_of(session)


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
    # delete = hard-remove. Allowed before a workshop starts, and once it is
    # finished (ended/cancelled) — a finished workshop is a record the trainer
    # may clear, and refusing it left every ended room cluttering the trainer's
    # list until the 7-day TTL. Only "running" is refused: deleting a live room
    # would strand its cohort. The target "deleted" is not a stored state (the
    # entity + index are removed by the endpoint); apply_transition only
    # validates legality — running is absent → raises → endpoint returns 409.
    "delete": {"scheduled": ("deleted", True), "open": ("deleted", True),
               "ended": ("deleted", True), "cancelled": ("deleted", True)},
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
    # Tenant scoping is about the LEAD only.
    #
    # WS-1 exists because one person's address can be signed into several
    # tenants, so "your workshops" filled up with workshops they had created
    # somewhere else. That reasoning covers the creator and nobody else. A
    # co-trainer was named explicitly, by address, exactly like a roster entry —
    # so scoping them by tenant hid the workshop from the very person who was
    # invited to teach it: added on COE, signed into sprint, workshop absent
    # from their home page AND their Workshops list, with no way in.
    #
    # Co-trainers are therefore treated like learners here: named means member,
    # whatever tenant they run on. That is what makes co-teaching cross-tenant.
    if normalize_email(email) != lead_trainer(session):
        return True
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
                    # Which trainer last moved the class pointer, and when.
                    # Only meaningful with >1 trainer, and absent until someone
                    # moves it — so truthy-only echoing is exactly right.
                    "pacingBy", "pacingAt",
                    # Only set once a workshop has finished, so it costs live
                    # payloads nothing and dates the row in the past listing.
                    "endedAt")


def _room_and_gate(session) -> dict:
    """
    The two booleans the app needs on EVERY payload, present even when false.

    These decide what a learner may touch, so "absent" must not have to be read
    as "false" by the client — that implicit-falsiness is precisely the class of
    bug EPIC-007 exists to remove.
    """
    return {"roomOpen": room_open(session),
            "gateAhead": pacing_state(session)["gateAhead"]}


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
        "trainers":     trainers_of(session),
        "trainerEmail": lead_trainer(session),
        "joinedCount":  len(joined or {}),
        "rosterCount":  len(roster or ()),
        "createdAt":    session.get("createdAt", ""),
        "startedAt":    session.get("startedAt", ""),
        "isTrainer":    is_trainer(e, session),
        "hasJoined":    e in (joined or {}),
    }
    out.update(_room_and_gate(session))
    out.update(workshop_fields(session, e))
    return out


def shape_detail(session_id, session, roster, joined, email,
                 provision_done=None, tenants=None) -> dict:
    """Full session state (GET /api/live/sessions/{id}).

    Everyone gets the scalar fields + joined/roster counts; the roster and
    the joined list (who + when + the tenant they checked in from) are only
    included for the trainer. `tenants` is the email→tenant hash from
    _live_tenants_key — optional, so write-echo callers that don't read it
    simply return joined rows without a tenant.

    `provisionRequested` is scoped to the CALLER — it is the pull channel for
    trainer-triggered provisioning, so it must answer "does the person holding
    THIS browser need an environment", never "does anyone". Callers that pass no
    `provision_done` (start/end/patch, which return the detail as an echo of a
    write rather than as a poll) get False and never trigger a provision."""
    out = {
        "sessionId":    session_id,
        "title":        session.get("title", ""),
        "trainingId":   session.get("trainingId", ""),
        "ref":          session.get("ref", ""),
        "state":        session.get("state", ""),
        "trainers":     trainers_of(session),
        "trainerEmail": lead_trainer(session),
        "createdAt":    session.get("createdAt", ""),
        "startedAt":    session.get("startedAt", ""),
        "endedAt":      session.get("endedAt", ""),
        "joinedCount":  len(joined or {}),
        "rosterCount":  len(roster or ()),
        "isTrainer":    is_trainer(email, session),
        "hasJoined":    normalize_email(email) in (joined or {}),
        "provisionRequested": provision_request_pending(
            session, email, provision_done),
        # The request timestamp is the client's dedupe key: the learner's app
        # fires the pull-channel provision once per (session, requestedAt), so a
        # remount while the flag is still true cannot mint a second token pair.
        "provisionRequestedAt": session.get("provisionRequestedAt", ""),
    }
    out.update(_room_and_gate(session))
    out.update(workshop_fields(session, email))
    if is_trainer(email, session):
        out["roster"] = sorted(roster or ())
        out["joined"] = [{"email": k, "joinedAt": v,
                          "tenant": (tenants or {}).get(k, "")}
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


def roster_targets(roster, trainers, include_trainer) -> list[tuple[str, str]]:
    """(email, role) pairs that provision-all and the readiness board walk.

    WS-4: a trainer runs the lab alongside the cohort, so they get an
    environment and a board row of their own. Appended only when asked for and
    only when they are not already an invited learner — a trainer who put
    themselves on the roster stays a single 'learner' row rather than
    appearing twice.

    `trainers` takes the whole team (a bare string is accepted for the
    single-trainer callers). Every co-trainer gets their own row, in team order,
    so a second trainer is provisionable and visible on the board like the first.

    Roster order is sorted for a stable board; the trainers are always last.
    """
    targets = [(e, LEARNER_ROLE) for e in sorted(roster or ())]
    if isinstance(trainers, str):
        trainers = [trainers]
    if include_trainer:
        seen = set(roster or ())
        for trainer in normalize_roster(trainers):
            if trainer not in seen:
                seen.add(trainer)
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

PACING_FIELDS = ("trainerStep", "unlockPath", "gateAhead", "pacingBy",
                 "pacingAt")


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def pacing_state(session) -> dict:
    """The pacing fields of a session, normalized.

    Two independent trainer switches. Absent still means OFF here — this
    function is the reader and must stay honest about what is stored — but new
    workshops are CREATED with gateAhead="1" (see api_live_session_create), so
    a trainer who touches nothing gets "hold the class here" on and solutions
    withheld. Workshops created before that default keep their stored values:

      unlockPath  release solutions to learners who fall behind (steps <= the
                  class pointer).
      gateAhead   hold learners who run ahead at the class pointer.

    pacingBy/pacingAt attribute the last pointer move. With up to MAX_TRAINERS
    trainers all able to move it (last write wins), co-trainers need to see who
    moved the class and when.
    """
    session = session or {}
    return {
        "trainerStep": _as_int(session.get("trainerStep"), 0),
        "unlockPath": str(session.get("unlockPath") or "") == "1",
        "gateAhead": str(session.get("gateAhead") or "") == "1",
        "pacingBy": session.get("pacingBy") or "",
        "pacingAt": session.get("pacingAt") or "",
    }


def class_pointer_of(session) -> int:
    """Public form of _class_pointer for callers holding the raw session hash —
    the highest 1-based step a gated learner may open right now."""
    return _class_pointer(pacing_state(session))


def _class_pointer(state) -> int:
    """
    The class pointer as a gating ceiling, floored at step 1.

    trainerStep is 0 until a trainer first moves it, and steps are 1-based. A
    literal reading would lock a gated learner out of step 1 before the trainer
    has touched anything — so an unmoved pointer means "the class is on step 1".
    """
    return max(state["trainerStep"], 1)


def solution_visible(step, session, is_trainer=False) -> bool:
    """May THIS viewer see the solution for `step` in this workshop?

    step is the learner's 1-based step number. A trainer always may — that is
    role, not pacing, and it does not depend on any toggle. A learner may only
    when unlockPath is on AND the step is at or behind the class pointer:
    a straggler can catch up on the step the class is currently doing, which is
    the one they are actually stuck on.

    Note the ceiling is NOT floored here: with the pointer unmoved (0) nothing is
    released, which is the correct default — a workshop that has not started has
    released no solutions.
    """
    if is_trainer:
        return True
    state = pacing_state(session)
    if not state["unlockPath"]:
        return False
    return _as_int(step, -1) <= state["trainerStep"]


def step_visible(step, session, is_trainer=False) -> bool:
    """May THIS viewer OPEN `step`?

    Default (gateAhead off) every step is openable and the learner paces
    themselves. With gateAhead on, a learner cannot open a step beyond the class
    pointer — the knob for a cohort where someone is racing ahead.

    This governs the step BODY only. Titles stay visible either way so the
    learner can see the shape of the workshop, and solutions remain governed
    separately by solution_visible().
    """
    if is_trainer:
        return True
    state = pacing_state(session)
    if not state["gateAhead"]:
        return True
    return _as_int(step, 10 ** 9) <= _class_pointer(state)


# ── The room (EPIC-007) ───────────────────────────────────────────────────────
#
# A workshop has TWO gates, and they are deliberately separate:
#
#   roomOpen        the trainer has opened the virtual classroom. Learners may
#                   chat, ask questions, raise a hand and see the board. The
#                   trainer flips this AFTER writing the welcome note and pad,
#                   so nobody walks into an empty room.
#   state=running   the trainer has started the workshop. Only now may a learner
#                   start an environment and open the lab steps.
#
# A learner can always ENTER (the lobby is never gated) and can be in the room
# without being able to start the training. Collapsing these into one flag would
# force the trainer to either open an unprepared room or keep the cohort staring
# at a locked page while they set it up.

def room_open(session) -> bool:
    """True when the trainer has opened the virtual classroom."""
    session = session or {}
    if session.get("state") in ("ended", "cancelled"):
        return False
    return str(session.get("roomOpen") or "") == "1"


def room_closed_reason(session) -> str:
    """Why the room is unavailable, for a 409 body. '' when it is open."""
    state = (session or {}).get("state") or ""
    if state == "ended":
        return "this workshop has ended"
    if state == "cancelled":
        return "this workshop was cancelled"
    if not room_open(session):
        return "the trainer has not opened the room yet"
    return ""


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
