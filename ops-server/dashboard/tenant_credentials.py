"""Shape checks for the credentials an SE pastes into Register Tenant.

These are input-validation, not authorisation: they answer "is this the right KIND of
thing?" before anything is sent to SSO. Getting that wrong is not a theoretical concern.
On 2026-08-24 `saikkoj` registered `bnk46244` six times; the first three attempts pasted a
PLATFORM TOKEN (`dt0s16.GP6CHX54`, `dt0s16.YM7EA5CJ`) into the OAuth client-id field.
Orbital forwarded it to SSO verbatim, SSO answered `400 invalid_request` with an empty
`error_description` for every scope family, and the preflight reported that as
"SSO refused environment-api:api-tokens:write" — i.e. it blamed the tenant's scopes for a
credential that was never an OAuth client at all.

The public tenant checker has rejected that shape since 2026-08-11
(`ops-server/tools/tenant-check-page/app.py`), which is exactly why the checker could show
green while the register refused: the checker CANNOT BE HANDED the input that failed. These
patterns are lifted from there verbatim so the two front doors agree on what they accept.

Deliberately NOT here: the tenant URL. `content_service.classify_tenant()` already owns that
gate (403 on any non-Dynatrace domain) and accepts a wider, authoritative set of hosts than
the checker page's narrower regex.
"""

import re

# dt0s02 = account OAuth client. dt0s16 = platform token, dt0c01 = classic API token,
# dt0g02 = ActiveGate token — none of which can complete a client_credentials grant.
CLIENT_ID_RE = re.compile(r"^dt0s02\.[A-Z0-9]{6,12}$")
SECRET_RE = re.compile(r"^dt0s02\.[A-Z0-9]{6,12}\.[A-Z0-9]{40,90}$")
URN_RE = re.compile(r"^urn:dtaccount:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# What a wrong prefix actually is, so the message can name it instead of guessing.
_TOKEN_KINDS = {
    "dt0s16": "a platform token",
    "dt0s08": "a platform token",
    "dt0c01": "a classic API token",
    "dt0g02": "an ActiveGate token",
    "dt0s01": "a legacy OAuth client secret (paste the full dt0s02.<id>.<secret>)",
}

_OAUTH_CLIENT_HINT = ("Register Tenant needs an account OAuth client (dt0s02…) from "
                      "myaccount.dynatrace.com → Identity & access management → OAuth clients.")


def _kind_of(value: str) -> str:
    """Name the credential family a pasted value belongs to, or "" when unrecognised."""
    return _TOKEN_KINDS.get(value.split(".", 1)[0], "")


def credential_problem(client_id: str, client_secret: str, account_urn: str) -> str:
    """The first thing wrong with this credential triple, as a sentence for the operator.

    Empty string means the shapes are fine — it says nothing about whether the client holds
    the scopes, which only the live preflight can answer.
    """
    if not CLIENT_ID_RE.match(client_id):
        kind = _kind_of(client_id)
        if kind:
            return (f"clientId looks like {kind} (`{client_id.split('.', 1)[0]}…`), not an "
                    f"OAuth client. {_OAUTH_CLIENT_HINT}")
        return (f"clientId is not an OAuth client id — expected `dt0s02.XXXXXXXX`. "
                f"{_OAUTH_CLIENT_HINT}")

    if not SECRET_RE.match(client_secret):
        kind = _kind_of(client_secret)
        if kind:
            return (f"clientSecret looks like {kind}, not an OAuth client secret. The secret "
                    f"is the full `dt0s02.<id>.<secret>` string shown ONCE when the client was "
                    f"created — it cannot be read back later, so a lost secret means a new client.")
        return ("clientSecret is not an OAuth client secret — expected the full "
                "`dt0s02.<id>.<secret>` string shown once at creation time.")

    # The secret embeds its own client id. A mismatched pair is a paste error that would
    # otherwise surface as an opaque SSO 401 attributed to the wrong tenant.
    if client_secret.split(".")[1] != client_id.split(".")[1]:
        return (f"clientSecret belongs to a different OAuth client "
                f"(`dt0s02.{client_secret.split('.')[1]}`) than clientId (`{client_id}`). "
                f"Paste the id and secret from the same client.")

    if not URN_RE.match(account_urn):
        return ("accountUrn must be `urn:dtaccount:<uuid>` exactly as shown in Account "
                "Management when the OAuth client was created.")
    return ""


def sso_failure_cause(status: int, err: str, client_id: str = "") -> str:
    """Why SSO refused a client_credentials grant, in the operator's terms.

    `_oauth_bearer` returns the same (status, body) for causes that need OPPOSITE fixes, and
    the preflight used to render all of them as "SSO refused <scope>" — which sends an
    operator to re-create a client when the real problem was the credential they pasted.
    Same failure class as the `rejected` vs `unreachable` conflation that misdiagnosed 26
    deploys in the APAC incident: one status, several meanings, one message.
    """
    from shared.log_safety import safe_error_detail

    if status == 0:
        return f"SSO was unreachable ({err or 'no response'}) — this is NOT evidence about scopes"
    if client_id and not CLIENT_ID_RE.match(client_id):
        problem = credential_problem(client_id, "dt0s02.X." + "A" * 40,
                                     "urn:dtaccount:00000000-0000-0000-0000-000000000000")
        return problem or "the pasted clientId is not an OAuth client"
    if status == 401:
        return "the client id or secret is wrong (HTTP 401) — not a scope problem"
    if status == 403:
        return "SSO rejected this client (HTTP 403)"
    if status == 400:
        # SSO stamps no reason when the scope simply is not in the client's catalog. An
        # empty error_description on a well-formed client is therefore the catalog gap —
        # and the ONLY case where "create a new client" is the right advice.
        detail = safe_error_detail(err) if err else ""
        if not err or "error_description" not in err or '"error_description":""' in err.replace(" ", ""):
            return ("the scope is not in this OAuth client's catalog (SSO 400, no reason given). "
                    "Scopes cannot be added to an existing client — this needs a NEW client")
        return f"SSO refused the grant (HTTP 400): {detail}"
    return f"SSO refused the grant (HTTP {status})"
