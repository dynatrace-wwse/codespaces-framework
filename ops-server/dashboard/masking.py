"""PII masking for public (anonymous) reads of Orbital APIs.

Running-training payloads carry learner emails and tenant URLs. Those are
fine for signed-in org members (nginx-verified X-Auth-User) and for the
Dynatrace app (service bearer), but the dashboard and several read APIs are
public — anonymous callers must only ever see masked values.

Pure functions, no FastAPI/Redis — unit-tested in dashboard/test_masking.py.
The identity decision (who is anonymous) lives in app.py; everything here
just transforms payloads.
"""


def mask_email(email) -> str:
    """'maria.gonzalez@dynatrace.com' -> 'ma***@d***'.

    Keeps the first 2 chars of the local part and the first char of the
    domain. Values without an '@' (usernames, hostgroup session ids) are
    treated as a bare local part: 'sergio_hinojosa_2026aug02' -> 'se***'.
    Falsy values pass through unchanged so absent fields stay absent.
    """
    if not email:
        return email
    email = str(email)
    if "@" not in email:
        return f"{email[:2]}***"
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain[:1]}***"


def mask_tenant(tenant) -> str:
    """'https://sro97894.apps.dynatrace.com' -> 'https://sro***'.

    Keeps the scheme (when present) and the first 3 chars of the tenant id
    (the host's first label). Bare ids work too: 'sro97894' -> 'sro***'.
    Falsy values pass through unchanged.
    """
    if not tenant:
        return tenant
    tenant = str(tenant)
    scheme, sep, rest = tenant.partition("://")
    if not sep:
        scheme, rest = "", tenant
    tenant_id = rest.split("/", 1)[0].split(".", 1)[0]
    masked = f"{tenant_id[:3]}***"
    return f"{scheme}://{masked}" if sep else masked


# ── Live-session payload masking ──────────────────────────────────────────────
# shape_summary/shape_detail (live_sessions.py) trust the CALLER-SUPPLIED email
# to decide trainer-only fields (joinCode, roster, joined). That is fine for
# the app's bearer-authed proxy, but an anonymous caller could supply the
# trainer's email — so anonymous responses are post-processed here: the
# trainer-only fields are dropped outright and every email is masked.

def mask_live_summary(item: dict) -> dict:
    """Anonymous view of one GET /api/live/sessions list item."""
    out = dict(item)
    out.pop("joinCode", None)
    if "trainerEmail" in out:
        out["trainerEmail"] = mask_email(out["trainerEmail"])
    return out


def mask_live_detail(item: dict) -> dict:
    """Anonymous view of GET /api/live/sessions/{id} (and transition echoes):
    no joinCode, no roster, no joined list, masked trainer email."""
    out = mask_live_summary(item)
    out.pop("roster", None)
    out.pop("joined", None)
    return out


def mask_readiness(payload: dict) -> dict:
    """Anonymous view of GET /api/live/sessions/{id}/readiness — the roster
    emails are masked; states/jobIds stay (jobIds are already public in the
    dashboard's running list)."""
    return {**payload,
            "results": [{**row, "email": mask_email(row.get("email", ""))}
                        for row in payload.get("results", [])]}


def mask_progress(payload: dict, keep: str = "") -> dict:
    """Non-trainer view of GET /api/live/sessions/{id}/progress.

    The board is meant to be visible to the cohort, so the states, percentages
    and the summary all stay — only the identities are masked, including the
    tenant (which is itself identifying in a cross-tenant workshop). `keep` is
    the caller's own email: their own row stays readable so they can find
    themselves on the board.
    """
    keep = str(keep or "").strip().lower()
    return {**payload,
            "results": [row if str(row.get("email", "")).lower() == keep and keep
                        else {**row,
                              "email": mask_email(row.get("email", "")),
                              "tenant": mask_tenant(row.get("tenant", ""))}
                        for row in payload.get("results", [])]}


def mask_pad(payload: dict) -> dict:
    """Anonymous view of GET /api/live/sessions/{id}/pad — question authors'
    emails are masked (names stay: they are free-text display names)."""
    return {**payload,
            "qa": [{**q, "email": mask_email(q.get("email", ""))}
                   for q in payload.get("qa", [])]}
