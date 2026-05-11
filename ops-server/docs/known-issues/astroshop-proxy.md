# Known Issue: Astroshop In-Dashboard Proxy — Incomplete Navigation

**Status:** Open  
**Component:** `ops-server/dashboard/app.py` — `proxy_job_app` / `_rewrite_proxy_body`  
**Symptom:** Astroshop loads partially but product images may not render and page may appear blank on initial load in some browsers/cache states.

---

## Background

The ops dashboard proxies apps running inside the Sysbox/k3d cluster via `/apps/{job_id}/{app_name}/`. The proxy rewrites HTML/CSS attributes and injects a JS shim so the app works from a sub-path it was never built for.

Astroshop is a Next.js app built assuming it runs at root (`/`). The proxy serves it at `/apps/{job_id}/astroshop/`.

---

## What Is Fixed

| Problem | Fix Applied |
|---------|-------------|
| Root-relative `src`/`action`/`data-src` attributes | Rewritten to `/apps/{job_id}/astroshop/...` |
| `http://localhost:8080//icons/...` absolute URLs | Origin stripped, double-slash normalised, prefixed |
| `srcSet="URL 1x, URL 2x"` (React camelCase) | Case-insensitive split-and-rewrite per descriptor |
| `<link href>` (CSS, icons, preloads) | Rewritten via tag-scoped regex |
| `<a href>` (navigation links) | **Not rewritten** — must stay as app routes for Next.js router |
| `fetch()` / `XMLHttpRequest` root-relative calls | JS shim prefixes `B + url` |
| `fetch()` / `XMLHttpRequest` with `http://localhost:...` | JS shim strips origin, uses path only |
| `history.pushState/replaceState` — iframe URL escapes proxy | Patched in shim; already-prefixed URLs guarded by `indexOf(B) !== 0` |
| CSS `url(...)` with root-relative and localhost paths | Rewritten in `_rewrite_css_url` |

---

## Root Cause of Remaining Issue

Next.js is a **single-page application with server-side routing awareness**. It initialises its router using `window.__NEXT_DATA__.page` (correct, `/`) but sets `router.asPath` from `window.location.pathname` (`/apps/{job_id}/astroshop/`).

When Next.js generates prefetch requests for `<Link>` components or internal data fetches (`_next/data/{buildId}/{page}.json`), these are based on the **internal route** (`/product/123`), not the proxy path — which is correct, and the `fetch()` shim transparently rewrites those root-relative fetch calls to go through the proxy.

The unresolved tension: **Next.js's `router.asPath` diverges from `router.pathname`** throughout the session. Some components in production Next.js apps use `router.asPath` to construct canonical URLs, structured data, or conditional rendering. Mismatched `asPath` causes React hydration warnings and can cause affected components to render incorrectly.

Additionally, on the first render with a cold browser cache, if a blocking script fetch fails before React hydration completes (e.g., due to a browser-cached stale `X-Frame-Options: DENY` header), the page may appear blank until hard-refreshed (`Ctrl+Shift+R`).

---

## What Would Properly Fix It

The clean solution is to configure Next.js `basePath` at build time:

```js
// next.config.js
module.exports = {
  basePath: '/apps/<job_id>/<app_name>',
}
```

This is not feasible here because the base path is dynamic (per-job, per-app).

**Viable alternative — Iframe + same-origin sub-domain:**  
Serve the app at a stable sub-path (e.g., `https://app-{job_id}.autonomous-enablements.whydevslovedynatrace.com/`) using wildcard DNS + wildcard TLS cert. The iframe src would be this URL; no rewriting needed. Requires a wildcard cert and nginx server block for `*.autonomous-enablements.whydevslovedynatrace.com`.

**Partial mitigation — `basePath` in the framework's Next.js template:**  
If the framework controls the Next.js `next.config.js`, inject `basePath` at container start time using the proxy prefix for that session. The `deployApp` helper could write a `NEXT_PUBLIC_BASE_PATH` env var and the Next.js config could read it. This would require a rebuild of the Next.js app inside the container.

---

## Debugging Checklist

If astroshop appears blank:
1. Hard-refresh (`Ctrl+Shift+R`) to clear cached headers.
2. Open DevTools → Network — check for `/_next/data/...` requests; they should go to `/apps/{job_id}/astroshop/_next/data/...`.
3. Check DevTools Console for React hydration errors.
4. On the master: `sudo journalctl -fu ops-dashboard` and look for 502/504 or repeated 404 patterns.
5. Verify the job's `app_proxy_port` is set: `redis-cli hget job:running:{job_id} app_proxy_port`.
6. Test direct upstream connectivity: `curl -s -o /dev/null -w "%{http_code}" http://{worker_ip}:{port}/ -H "Host: astroshop.{ip}.sslip.io"`.
