import httpx, json
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
app = FastAPI()
TARGET_PORT = 18080
class _Pool:
    async def hgetall(self, k): return {"host": "127.0.0.1"}
pool = _Pool()
async def _find_job_by_subdomain(subdomain):
    return "spike92", {"app_proxy_port": str(TARGET_PORT), "worker_id": "w"}, {"name": "gitlab", "ingress_host": "gitlab--spike92.staging.autonomous-enablements.whydevslovedynatrace.com"}

@app.api_route(
    "/proxy-subdomain/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_subdomain_app(request: Request, path: str):
    """Reverse-proxy to an app via its wildcard subdomain URL.

    No HTML/CSS rewriting — the app is served at root (/), so all relative
    asset paths resolve naturally without a basePath shim.
    """
    from fastapi.responses import Response as PlainResponse

    subdomain = request.headers.get("x-app-subdomain", "")
    if not subdomain or "--" not in subdomain:
        raise HTTPException(status_code=400, detail="missing or invalid X-App-Subdomain header")

    job_id, meta, app_info = await _find_job_by_subdomain(subdomain)
    if not job_id:
        raise HTTPException(status_code=404, detail=f"no running job found for subdomain '{subdomain}'")

    app_proxy_port = meta.get("app_proxy_port")
    if not app_proxy_port:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:32px;background:#050810;color:#d0d7de'>"
            "<h3 style='color:#f0b429'>App proxy not available</h3>"
            "<p>This job was started before app port forwarding was added.</p>"
            "<p>Terminate and start a new session to enable app preview.</p>"
            "</body></html>",
            status_code=503,
        )

    worker_id = meta.get("worker_id", "")
    if worker_id != "master":
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        target_ip = worker_hash.get("host", "127.0.0.1")
    else:
        target_ip = "127.0.0.1"

    target_url = f"http://{target_ip}:{app_proxy_port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    forward_headers = {
        "Host": app_info["ingress_host"],
    }
    forward_headers["X-Forwarded-Proto"] = "https"  # SPIKE FIX 1
    for h in ("authorization", "git-protocol", "user-agent", "x-csrf-token", "origin", "referer",  # SPIKE FIX 3
              "accept", "accept-language", "cookie",
               "content-type", "cache-control", "x-requested-with"):
        if h in request.headers:
            forward_headers[h] = request.headers[h]
    # accept-encoding intentionally not forwarded — we need plain bytes to
    # rewrite localhost URLs in HTML without re-compressing the body.

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="could not connect to app")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="upstream app timed out")

    skip = {"transfer-encoding", "connection", "keep-alive", "upgrade",
            "proxy-authenticate", "proxy-authorization", "te", "trailers",
            "x-frame-options", "content-security-policy",
            "content-security-policy-report-only",
            "content-encoding", "content-length"}
    resp_headers = [  # SPIKE FIX 2: keep every Set-Cookie (dict collapses them)
        (k, v) for k, v in upstream.headers.multi_items()
        if k.lower() not in skip
    ]

    content = upstream.content
    content_type = upstream.headers.get("content-type", "")

    # Rewrite hardcoded localhost:8080 in HTML bodies.
    # Next.js imageLoader uses NEXT_PUBLIC_FRONTEND_ADDR which is baked to
    # http://localhost:8080 at image build time; browsers can't reach that.
    if "text/html" in content_type and b"localhost:8080" in content:
        public_host = request.headers.get("host", "")
        if public_host:
            pub = f"https://{public_host}".encode()
            content = content.replace(b"http://localhost:8080", pub)
            content = content.replace(b"https://localhost:8080", pub)

    r = PlainResponse(content=content, status_code=upstream.status_code, media_type=content_type or None)
    for k, v in resp_headers:
        if k.lower() != "content-type":
            r.raw_headers.append((k.lower().encode(), v.encode()))
    return r
    return PlainResponse(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


