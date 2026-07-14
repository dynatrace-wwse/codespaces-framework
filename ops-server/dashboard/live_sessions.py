"""Pure decision logic for live training sessions (bootcamp cohorts).

No Redis, no FastAPI — everything here is deterministic and unit-tested in
dashboard/test_live_sessions.py. The /api/live/* endpoints in app.py stay
thin: they read/write the Redis keys and delegate every decision here.

Redis model (docs/live-training-architecture.md, ops-server/CLAUDE.md):
  live:session:{id}         hash  title, trainingId, ref, trainerEmail,
                                  state (open|running|ended),
                                  createdAt, startedAt, endedAt
  live:session:{id}:roster  set   lowercase invited emails
  live:session:{id}:joined  hash  email -> ISO joinedAt
  live:sessions:index       zset  sessionId scored by epoch createdAt
"""

STATES = ("open", "running", "ended")

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
    """Return (http_status, detail) blocking a join, or None when allowed."""
    if not on_roster(email, roster):
        return 403, "email is not on the session roster"
    if state == "ended":
        return 409, "session has ended"
    return None


# ── State transitions ─────────────────────────────────────────────────────────

# action -> {current_state: (new_state, changed)}. Absent = illegal.
_TRANSITIONS = {
    "start": {"open": ("running", True), "running": ("running", False)},
    "end":   {"open": ("ended", True), "running": ("ended", True),
              "ended": ("ended", False)},
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


def shape_summary(session_id, session, roster, joined, email) -> dict:
    """One item of GET /api/live/sessions?email= (learner + trainer lists)."""
    e = normalize_email(email)
    return {
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
    if is_trainer(email, session):
        out["roster"] = sorted(roster or ())
        out["joined"] = [{"email": k, "joinedAt": v}
                         for k, v in sorted((joined or {}).items())]
    return out
