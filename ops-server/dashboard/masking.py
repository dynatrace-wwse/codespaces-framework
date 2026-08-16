"""PII masking for public (anonymous) reads of Orbital APIs.

Running-training payloads carry learner emails and tenant URLs. Those are
fine for signed-in org members (nginx-verified X-Auth-User) and for the
Dynatrace app (service bearer), but the dashboard and several read APIs are
public — anonymous callers must only ever see masked values.

Pure functions, no FastAPI/Redis — unit-tested in dashboard/test_masking.py.
The identity decision (who is anonymous) lives in app.py; everything here
just transforms payloads.

`scrub_for_log` is re-exported here. It is not masking — it is the other
direction of the same idea: a value the caller controls must not be able to
change the SHAPE of what we emit, whether that is an HTML attribute, a JSON
payload, or a journald line. It moved to `shared/` once the webhook server and
the provisioning library needed it too; dashboard code may keep reaching for it
as `masking.scrub_for_log`, which is the same function object.
"""

from shared.log_safety import scrub_for_log  # noqa: F401  (re-export)


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
    # Trainer-only, added with the tenant-binding split. `bindings` is strictly
    # WORSE to leak than `joined`: it maps every learner's email to the tenant
    # they will run in, and it covers people who never checked in. `seats`
    # is dropped alongside it because it is part of the same trainer block.
    out.pop("bindings", None)
    out.pop("seats", None)
    return out


def mask_readiness(payload: dict) -> dict:
    """Anonymous view of GET /api/live/sessions/{id}/readiness — the roster
    emails and bound tenants are masked; states/jobIds stay (jobIds are already
    public in the dashboard's running list).

    The tenant is masked on the same grounds as mask_progress and
    mask_attendees: in a cross-tenant workshop knowing which tenant someone
    runs in is itself identifying.
    """
    return {**payload,
            "results": [{**row,
                         "email": mask_email(row.get("email", "")),
                         # Only when the row has one: a row without a tenant
                         # must not gain an empty field it never had.
                         **({"tenant": mask_tenant(row["tenant"])}
                            if row.get("tenant") else {}),
                         # Same grounds as `tenant`: which tenant someone's
                         # environment runs in is identifying. `attendance` and
                         # `tenantMismatch` are not — they say nothing about
                         # WHICH tenant — so they stay readable.
                         **({"envTenant": mask_tenant(row["envTenant"])}
                            if row.get("envTenant") else {})}
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


def mask_attendees(rows: list, keep: str = "") -> list:
    """Non-trainer view of the Virtual Room's attendee rail (RFE-C).

    The rail exists so a learner can see who is in the room, so the display
    names, presence and role all stay — only the email and the tenant, which
    is itself identifying in a cross-tenant workshop, are masked. `keep` is
    the caller's own address so they can still find themselves.
    """
    keep = str(keep or "").strip().lower()
    return [row if str(row.get("email", "")).lower() == keep and keep
            else {**row,
                  "email": mask_email(row.get("email", "")),
                  "tenant": mask_tenant(row.get("tenant", ""))}
            for row in rows or []]


def mask_events(rows: list) -> list:
    """Non-trainer view of the workshop audit trail.

    The trail is a cohort-wide record — who joined, whose tenant provisioned
    what — so it is exactly the shape of disclosure BUG-MASK-1 was about. The
    ordering, the kinds and the timestamps carry no identity and stay, so a
    learner's client can still page the stream; every address and tenant is
    masked, including `actor`, since the trainer's own address is as
    identifying as a learner's.

    No `keep` parameter: unlike the attendee rail and the chat, nothing in the
    trail is addressed to a learner, so there is no reason to unmask their own
    row and no need to hand this function the caller's identity.
    """
    masked = []
    for row in rows or []:
        # Only rewrite fields the row actually has: audit_event drops empties,
        # so adding "email": "" here would make an absent field look present.
        out = dict(row)
        for field, fn in (("email", mask_email), ("actor", mask_email),
                          ("tenant", mask_tenant)):
            if out.get(field):
                out[field] = fn(out[field])
        masked.append(out)
    return masked


def mask_chat(messages: list, keep: str = "") -> list:
    """Non-trainer view of the chat transcript: the message text and the
    display name are the point of a chat and stay as sent; the sender's
    address is masked, except the caller's own."""
    keep = str(keep or "").strip().lower()
    return [msg if str(msg.get("email", "")).lower() == keep and keep
            else {**msg, "email": mask_email(msg.get("email", ""))}
            for msg in messages or []]


def mask_pad(payload: dict) -> dict:
    """Anonymous view of GET /api/live/sessions/{id}/pad — question authors'
    emails are masked (names stay: they are free-text display names)."""
    return {**payload,
            "qa": [{**q, "email": mask_email(q.get("email", ""))}
                   for q in payload.get("qa", [])]}
