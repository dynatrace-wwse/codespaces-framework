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


# The 15 scopes Register Tenant needs, as data. The ActiveGate entry is an EITHER-OR:
# the classic scope is missing from some clients' catalogs and the fleet-management twin
# exists exactly where it does not (hpm49270, 2026-08-19), so requiring the classic one by
# name refuses clients that can do the job.
REGISTER_SCOPES: tuple[frozenset, ...] = (
    frozenset({"app-engine:apps:install"}),
    frozenset({"app-engine:apps:run"}),
    frozenset({"app-engine:apps:delete"}),
    frozenset({"settings:objects:read"}),
    frozenset({"settings:objects:write"}),
    frozenset({"app-settings:objects:read"}),
    frozenset({"environment-api:api-tokens:read"}),
    frozenset({"environment-api:api-tokens:write"}),
    frozenset({"environment-api:activegate-tokens:write",
               "fleet-management:activegate.tokens:write"}),
    frozenset({"document:documents:read"}),
    frozenset({"document:documents:write"}),
    frozenset({"document:documents:delete"}),
    frozenset({"document:documents:admin"}),
    frozenset({"platform-token:tokens:write"}),
    frozenset({"platform-token:tokens:manage"}),
)


def missing_from_catalog(catalog) -> list[str]:
    """Which required scopes this client's catalog cannot satisfy.

    An either-or entry is satisfied by any one of its alternatives, and is reported as
    "a or b" so the operator is not told to add a scope their catalog does not offer.
    """
    held = set(catalog or ())
    return [" or ".join(sorted(entry)) for entry in REGISTER_SCOPES if not (entry & held)]


def sso_failure_cause(status: int, err: str, client_id: str = "",
                      client_exists: bool | None = None) -> str:
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
        # SSO answers 400 invalid_request with an EMPTY error_description for THREE
        # different causes — no such client, wrong secret, and scope-not-in-catalog —
        # and the bodies are byte-identical (measured against sso.dynatrace.com,
        # 2026-08-24). The status alone therefore cannot tell them apart, and guessing
        # "catalog gap" sends an operator to re-create a client when their secret was
        # simply wrong. `client_exists` carries the answer from a SCOPE-LESS grant,
        # which is the only thing that separates them (see _client_catalog).
        detail = safe_error_detail(err) if err else ""
        described = bool(err) and "error_description" in err and '"error_description":""' not in err.replace(" ", "")
        if described:
            return f"SSO refused the grant (HTTP 400): {detail}"
        if client_exists is False:
            return ("the client id or secret is wrong, or the client does not exist "
                    "(SSO 400) — this is NOT a scope problem")
        if client_exists is True:
            return ("the scope is not in this OAuth client's catalog (SSO 400, no reason "
                    "given). Scopes cannot be added to an existing client — this needs a "
                    "NEW client")
        return ("SSO 400 with no reason given — this is EITHER a scope missing from the "
                "client's catalog OR a wrong client id/secret; the two are "
                "indistinguishable here")
    return f"SSO refused the grant (HTTP {status})"
