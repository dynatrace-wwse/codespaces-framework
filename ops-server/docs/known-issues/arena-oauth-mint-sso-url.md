# The arena `oauthClientId` provisioning path has never worked

**Found** 2026-08-14, while trying to provision a real Astroshop session to measure it.

`provisioning/dt_token_provisioner.py`:

```python
_OAUTH_TOKEN_URL = "{tenant}/sso/oauth2/token"
```

The OAuth2 client-credentials grant is posted to the **tenant** host. SSO does not live
there — it is `https://sso.dynatrace.com` for production tenants and
`https://sso-sprint.dynatracelabs.com` for sprint. The tenant host answers with a 301 and
the mint fails:

```
Token provisioning failed for dynatrace-wwse/demo-astroshop-problems / …:
Redirect response '301 Moved Permanently' for url
'https://sro97894.apps.dynatrace.com/sso/oauth2/token'
```

`api_arena_provision` then returns `tokenProvisioned: false` with no `DT_OPERATOR_TOKEN`,
and the session dies at `postCreateCommand` a few seconds later.

## Why nobody has hit it

This branch of `api_arena_provision` only runs when a caller supplies
`oauthClientId`/`oauthClientSecret`. In normal operation the **app** mints with its own
identity and passes token *values* in `dtEnv`, which takes the first branch and never
touches this code. So the path is effectively dead except to scripted callers — a load
test, a measurement harness, or anything driving Orbital without the app in front of it.

## The fix

`dashboard/app_deploy.py` already resolves this correctly: `discover_sso(tenant_url)`
returns the right SSO host, and `DEFAULT_SSO` / the `"prod"` entry in its SSO map are the
existing constants. The provisioner should use the same resolution instead of formatting
the tenant URL.

Deliberately **not** fixed in the same pass that found it: it is a credential flow, it is
outside the workshop-pools work, and it wants its own review rather than a change made at
01:00 in the middle of a fleet rehearsal.

## Consequence for testing

Any harness that wants a *real* provisioned environment (tokens minted, lab deployed) must
either go through the app, or this must be fixed first. It is why the Astroshop
steady-state measurement is still outstanding — the manifest-derived figure (6,320 MiB of
declared pod limits across ten components) is evidence, not a measurement.
