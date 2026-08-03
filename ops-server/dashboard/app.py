"""Dashboard — web UI and API for the multi-arch ops platform."""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import fcntl
import pty
import struct
import termios

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import redis.asyncio as redis

from webhook.config import REDIS_URL, FRAMEWORK_DIR

# GitHub token used to dispatch workflow_run events. Required for the
# /api/builds/trigger endpoint. Generate a fine-grained PAT with
# `actions:write` and `contents:read` for the org's repos.
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_API   = "https://api.github.com"
# Org used to gate writer-role checks. Anyone who is a member of this org
# (verified by oauth2-proxy + the GH /orgs/.../memberships endpoint) gets
# the 'writer' role and can execute actions; everyone else is 'guest'.
GH_ORG   = os.environ.get("OAUTH2_GITHUB_ORG", "dynatrace-wwse")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops-dashboard")

app = FastAPI(title="Enablement Ops Dashboard", version="2.0.0")

# OTel must wrap the app BEFORE it starts serving (middleware can't be added
# later). Traces (FastAPI/Redis/httpx) + host gauges → COE; no OneAgent by design.
try:
    from dashboard.otel_setup import init_otel as _init_otel
    _OTEL_ACTIVE = _init_otel(app, service_name="orbital-dashboard")
except Exception as _exc:  # telemetry must never block operations
    logging.getLogger("ops-dashboard").warning("OTel init failed (continuing without): %s", _exc)
    _OTEL_ACTIVE = False

# Content distribution service (multi-tenant content delivery, Phase 1):
# serves curated profiles + proxies private-repo content with the Orbital
# GitHub token, gated by a per-tenant X-Content-Key.
from dashboard.content_service import (  # noqa: E402
    router as content_router, classify_tenant, resolve_profile, _load_profile,
)
app.include_router(content_router)

# SSO-delegated app deploy (Phase 1: OAuth flow + audit).
from dashboard.app_deploy import router as deploy_router  # noqa: E402
app.include_router(deploy_router)

# Per-user GitHub OAuth broker + user-owned Codespace launch/relay (Codespaces
# launch toggle). Compute runs in the learner's own GitHub account; Orbital holds
# only a short-lived per-user token and relays terminal/logs/app-URL. Additive —
# unused unless the app's provisioning-mode is set to "codespace".
from dashboard.github_oauth import router as github_oauth_router  # noqa: E402
app.include_router(github_oauth_router)
from dashboard.codespace_service import router as codespace_router  # noqa: E402
app.include_router(codespace_router)

# EC2 spot-worker fleet scaling (aws CLI, no boto3) — see dashboard/fleet.py.
from dashboard import fleet  # noqa: E402

# Live training sessions (bootcamp cohorts) — pure decision logic lives in
# dashboard/live_sessions.py (tested without Redis); endpoints below stay thin.
from dashboard import live_sessions  # noqa: E402

# Structured workshop pad (EPIC-002) — pure decision logic in
# dashboard/live_pad.py (tested without Redis); endpoints below stay thin.
from dashboard import live_pad  # noqa: E402

# PII masking for anonymous (public) reads — pure transforms in
# dashboard/masking.py (tested without Redis).
from dashboard import masking  # noqa: E402

_PROFILES_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Content Profiles</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#0d1117;color:#e6edf3}
header{padding:14px 22px;background:#161b22;border-bottom:1px solid #30363d;font-weight:600}
main{max-width:920px;margin:0 auto;padding:22px}
.p{border:1px solid #30363d;border-radius:8px;margin:14px 0;background:#161b22}
.p h3{margin:0;padding:12px 16px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center}
textarea{width:100%;box-sizing:border-box;min-height:240px;font-family:ui-monospace,monospace;font-size:12px;
  background:#0d1117;color:#e6edf3;border:0;border-radius:0 0 8px 8px;padding:12px;resize:vertical}
button{background:#6c6cff;color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer;font-weight:600}
button.sec{background:#30363d}
.msg{font-size:12px;margin-left:10px;opacity:.8}
.bar{padding:10px 16px;border-top:1px solid #30363d;display:flex;gap:8px;align-items:center}
input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px}
</style></head><body>
<header>Content Profiles — curate what each tenant receives</header>
<main>
<div id=list>Loading…</div>
<div class=p><div class=bar>
  <input id=newid placeholder="new-profile-id">
  <button onclick="addProfile()">+ New profile</button>
</div></div>
</main>
<script>
const API="/api/content/admin/profiles";
async function load(){
  const r=await fetch(API);
  if(!r.ok){document.getElementById('list').innerHTML='<p>Sign in as an org member to manage profiles.</p>';return;}
  const {profiles}=await r.json();
  document.getElementById('list').innerHTML=profiles.map(p=>card(p)).join('')||'<p>No profiles.</p>';
}
function card(p){
  const id=p.profileId;
  return `<div class=p><h3><span>${id}</span>
    <span><button onclick="save('${id}')">Save</button>
    ${id==='all'||id==='default'?'':`<button class=sec onclick="del('${id}')">Delete</button>`}
    <span class=msg id="m-${id}"></span></span></h3>
    <textarea id="t-${id}">${JSON.stringify(p,null,2).replace(/</g,'&lt;')}</textarea></div>`;
}
async function save(id){
  let body; try{body=JSON.parse(document.getElementById('t-'+id).value);}catch(e){return msg(id,'Invalid JSON');}
  const r=await fetch(API+'/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json(); msg(id, r.ok?`saved (${j.sources} sources)`:(j.detail||'error'));
}
async function del(id){ if(!confirm('Delete '+id+'?'))return; await fetch(API+'/'+id,{method:'DELETE'}); load(); }
function addProfile(){
  const id=document.getElementById('newid').value.trim(); if(!id)return;
  const tmpl={profileId:id,description:"",sources:[{key:"",category:"",categoryLabel:"",repo:"dynatrace-wwse/REPO",branch:"main"}]};
  fetch(API+'/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(tmpl)}).then(load);
}
function msg(id,t){const e=document.getElementById('m-'+id);if(e)e.textContent=t;}
load();
</script></body></html>"""


@app.get("/profiles", response_class=HTMLResponse)
async def profiles_page():
    """Profile management UI. The page is public; its API calls are writer-gated."""
    return HTMLResponse(_PROFILES_PAGE)


_TENANTS_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Content Delivery</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#0d1117;color:#e6edf3}
header{padding:14px 22px;background:#161b22;border-bottom:1px solid #30363d;font-weight:600}
header a{color:#9d9dff;margin-left:14px;font-weight:400;font-size:14px}
main{max-width:760px;margin:0 auto;padding:22px}
h3{border-bottom:1px solid #30363d;padding-bottom:6px}
table{width:100%;border-collapse:collapse;margin:8px 0}
td,th{padding:6px 8px;border-bottom:1px solid #21262d;text-align:left}
input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px}
select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px}
button{background:#6c6cff;color:#fff;border:0;border-radius:6px;padding:7px 16px;cursor:pointer;font-weight:600}
button.sec{background:#30363d}
.msg{font-size:13px;margin-left:10px;opacity:.85}
.hint{opacity:.6;font-size:13px}
</style></head><body>
<header>Content Delivery — which profile each tenant receives
  <a href="/profiles">edit profiles →</a></header>
<main>
<p class=hint>Authentication is by Dynatrace domain. Defaults apply per environment class;
a tenant id override wins. Tenant id = the subdomain (e.g. <code>geu80787</code>).</p>
<h3>Domain defaults</h3>
<table id=defaults></table>
<h3>Tenant overrides</h3>
<table id=tenants><tbody></tbody></table>
<div style="margin:8px 0">
  <input id=newtid placeholder="tenant-id (e.g. geu80787)" size=24>
  <select id=newpid></select>
  <button class=sec onclick="addRow()">+ Add tenant</button>
</div>
<div style="margin-top:18px">
  <button onclick="save()">Save delivery table</button>
  <span class=msg id=msg></span>
</div>
</main>
<script>
const API="/api/content/admin/tenant-map";
let PROFILES=[], DOMAINS=[];
function opts(sel){return PROFILES.map(p=>`<option ${p===sel?'selected':''}>${p}</option>`).join('');}
async function load(){
  const r=await fetch(API);
  if(!r.ok){document.body.innerHTML='<p style=padding:22px>Sign in as an org member to manage delivery.</p>';return;}
  const {map,profiles,domains}=await r.json(); PROFILES=profiles; DOMAINS=domains;
  document.getElementById('newpid').innerHTML=opts(profiles[0]);
  document.getElementById('defaults').innerHTML='<tr><th>Domain</th><th>Default profile</th></tr>'+
    domains.map(d=>`<tr><td>${d}</td><td><select id="d-${d}">${opts((map.defaults||{})[d])}</select></td></tr>`).join('');
  const tb=document.querySelector('#tenants tbody'); tb.innerHTML='';
  Object.entries(map.tenants||{}).forEach(([t,p])=>tb.appendChild(row(t,p)));
}
function row(tid,pid){
  const tr=document.createElement('tr');
  tr.innerHTML=`<td><code>${tid}</code></td><td><select>${opts(pid)}</select></td>
    <td><button class=sec onclick="this.closest('tr').remove()">remove</button></td>`;
  tr.dataset.tid=tid; return tr;
}
function addRow(){
  const tid=document.getElementById('newtid').value.trim(); if(!tid)return;
  document.querySelector('#tenants tbody').appendChild(row(tid,document.getElementById('newpid').value));
  document.getElementById('newtid').value='';
}
async function save(){
  const defaults={}; DOMAINS.forEach(d=>defaults[d]=document.getElementById('d-'+d).value);
  const tenants={}; document.querySelectorAll('#tenants tbody tr').forEach(tr=>{
    tenants[tr.dataset.tid]=tr.querySelector('select').value;});
  const r=await fetch(API,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({defaults,tenants})});
  const j=await r.json(); document.getElementById('msg').textContent=r.ok?`saved (${j.tenants} tenant override(s))`:(j.detail||'error');
}
load();
</script></body></html>"""


@app.get("/tenants", response_class=HTMLResponse)
async def tenants_page():
    """Delivery-table UI (tenant → profile). Page public; API writer-gated."""
    return HTMLResponse(_TENANTS_PAGE)


_CONTENT_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Content Delivery</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#0d1117;color:#e6edf3}
header{padding:14px 22px;background:#161b22;border-bottom:1px solid #30363d;font-weight:600}
header a{color:#9d9dff;margin-left:14px;font-weight:400;font-size:14px}
main{max-width:980px;margin:0 auto;padding:22px}
h2{font-size:16px;border-bottom:1px solid #30363d;padding-bottom:6px;margin-top:30px}
.card{border:1px solid #30363d;border-radius:8px;background:#161b22;padding:14px;margin:10px 0}
input,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 10px}
input{width:340px} table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 8px;border-bottom:1px solid #21262d;text-align:left}
button{background:#6c6cff;color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;font-weight:600}
button.sec{background:#30363d} button.danger{background:#8b2c2c} .hint{opacity:.6;font-size:13px}
.chip{display:inline-block;background:#21262d;border-radius:10px;padding:2px 9px;margin:2px;font-size:12px}
.repos{column-count:2;margin-top:8px} .repos label{display:block;font-size:13px;padding:2px 0}
.msg{margin-left:10px;font-size:13px} pre{white-space:pre-wrap;background:#0d1117;padding:8px;border-radius:6px}
</style></head><body>
<header>Content Delivery — profiles · tenants · preview
  <a href="/deploy">tenant registration →</a></header>
<main>
<p class=hint>Profiles = named sets of training repos. The delivery table maps each domain
(prod/sprint/dev) to a default profile, and lets you override any tenant. Resolution:
tenant override &gt; domain default &gt; <code>all</code>.</p>

<h2>① Preview — what does a tenant receive?</h2>
<div class=card>
  <input id=ptenant placeholder="https://abc12345.apps.dynatrace.com">
  <button onclick="resolve()">Resolve</button><span class=msg id=pmsg></span>
  <div id=presult></div>
</div>

<h2>② Delivery table</h2>
<div class=card>
  <b>Domain defaults</b>
  <table id=defaults></table>
  <b style="display:block;margin-top:12px">Tenant overrides</b>
  <table id=tenants><tbody></tbody></table>
  <div style="margin-top:8px">
    <input id=newtid placeholder="tenant id (e.g. geu80787)" style="width:220px">
    <select id=newtp></select>
    <button class=sec onclick="addTenant()">+ tenant</button>
  </div>
  <div style="margin-top:12px"><button onclick="saveDelivery()">Save delivery table</button><span class=msg id=dmsg></span></div>
</div>

<h2>③ Profiles</h2>
<div id=profiles></div>
<div class=card>
  <b>New / edit profile</b><br>
  <input id=pfid placeholder="profile id (a-z0-9-_)" style="width:220px">
  <input id=pfdesc placeholder="description" style="width:420px"><br>
  <div class=repos id=pfrepos></div>
  <div style="margin-top:8px"><button onclick="saveProfile()">Save profile</button><span class=msg id=fmsg></span></div>
</div>
</main>
<script>
let DATA={profiles:[],map:{defaults:{},tenants:{}},domains:[],catalog:[]};
const opts=(sel)=>DATA.profiles.map(p=>`<option ${p.profileId===sel?'selected':''}>${p.profileId}</option>`).join('');
async function load(){
  const r=await fetch('/api/content/admin/overview');
  if(!r.ok){document.body.innerHTML='<p style=padding:22px>Sign in as an org member to manage content.</p>';return;}
  DATA=await r.json();
  // delivery defaults
  document.getElementById('defaults').innerHTML='<tr><th>Domain</th><th>Default profile</th></tr>'+
    DATA.domains.map(d=>`<tr><td>${d}</td><td><select id="d-${d}">${opts((DATA.map.defaults||{})[d])}</select></td></tr>`).join('');
  // tenant overrides
  const tb=document.querySelector('#tenants tbody'); tb.innerHTML='';
  Object.entries(DATA.map.tenants||{}).forEach(([t,p])=>tb.appendChild(trow(t,p)));
  document.getElementById('newtp').innerHTML=opts(DATA.profiles[0]&&DATA.profiles[0].profileId);
  // profiles list
  document.getElementById('profiles').innerHTML=DATA.profiles.map(p=>{
    const used=[...DATA.domains.filter(d=>(DATA.map.defaults||{})[d]===p.profileId).map(d=>d+' default'),
      ...Object.entries(DATA.map.tenants||{}).filter(([,v])=>v===p.profileId).map(([t])=>t)];
    return `<div class=card><b>${p.profileId}</b> <span class=hint>${p.description||''}</span>
      <button class=sec style="float:right" onclick="editProfile('${p.profileId}')">edit</button>
      ${p.profileId==='all'||p.profileId==='core'?'':`<button class=danger style="float:right;margin-right:6px" onclick="delProfile('${p.profileId}')">delete</button>`}
      <div>${(p.sources||[]).map(s=>`<span class=chip>${s.repo.split('/').pop()}</span>`).join('')}</div>
      ${used.length?`<div class=hint style="margin-top:6px">used by: ${used.join(', ')}</div>`:''}</div>`;
  }).join('');
  renderRepoPicker([]);
}
function trow(tid,pid){const tr=document.createElement('tr');
  tr.innerHTML=`<td><code>${tid}</code></td><td><select>${opts(pid)}</select></td><td><button class=sec onclick="this.closest('tr').remove()">remove</button></td>`;
  tr.dataset.tid=tid; return tr;}
function addTenant(){const t=document.getElementById('newtid').value.trim();if(!t)return;
  document.querySelector('#tenants tbody').appendChild(trow(t,document.getElementById('newtp').value));document.getElementById('newtid').value='';}
async function saveDelivery(){
  const defaults={}; DATA.domains.forEach(d=>defaults[d]=document.getElementById('d-'+d).value);
  const tenants={}; document.querySelectorAll('#tenants tbody tr').forEach(tr=>tenants[tr.dataset.tid]=tr.querySelector('select').value);
  const r=await fetch('/api/content/admin/tenant-map',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({defaults,tenants})});
  const j=await r.json(); document.getElementById('dmsg').textContent=r.ok?`✓ saved (${j.tenants} override(s))`:('✗ '+(j.detail||'error')); if(r.ok) load();
}
function renderRepoPicker(selected){
  document.getElementById('pfrepos').innerHTML=DATA.catalog.map(c=>{
    const id='r_'+btoa(c.repo).replace(/=/g,'');
    return `<label><input type=checkbox id="${id}" data-repo="${c.repo}" data-cat="${c.category}" data-label="${c.categoryLabel}" data-branch="${c.branch}" ${selected.includes(c.repo)?'checked':''}> ${c.repo.split('/').pop()} <span class=hint>(${c.category})</span></label>`;
  }).join('');
}
function editProfile(id){const p=DATA.profiles.find(x=>x.profileId===id);
  document.getElementById('pfid').value=p.profileId; document.getElementById('pfdesc').value=p.description||'';
  renderRepoPicker((p.sources||[]).map(s=>s.repo)); window.scrollTo(0,document.body.scrollHeight);}
async function saveProfile(){
  const id=document.getElementById('pfid').value.trim(), desc=document.getElementById('pfdesc').value.trim();
  if(!id){document.getElementById('fmsg').textContent='profile id required';return;}
  const sources=[...document.querySelectorAll('#pfrepos input:checked')].map(c=>({repo:c.dataset.repo,category:c.dataset.cat,categoryLabel:c.dataset.label,branch:c.dataset.branch}));
  if(!sources.length){document.getElementById('fmsg').textContent='pick at least one repo';return;}
  const r=await fetch('/api/content/admin/profiles/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:desc,sources})});
  const j=await r.json(); document.getElementById('fmsg').textContent=r.ok?`✓ saved (${j.sources} repos)`:('✗ '+(j.detail||'error'));
  if(r.ok){document.getElementById('pfid').value='';document.getElementById('pfdesc').value='';load();}
}
async function delProfile(id){if(!confirm('Delete profile '+id+'?'))return;
  const r=await fetch('/api/content/admin/profiles/'+id,{method:'DELETE'}); if(r.ok)load(); else document.getElementById('fmsg').textContent='✗ delete failed';}
async function resolve(){
  const t=document.getElementById('ptenant').value.trim(); if(!t)return;
  document.getElementById('pmsg').textContent='resolving…'; document.getElementById('presult').innerHTML='';
  const r=await fetch('/api/content/manifest?tenant='+encodeURIComponent(t));
  const j=await r.json();
  if(!r.ok){document.getElementById('pmsg').textContent='✗ '+(j.detail||'error');return;}
  document.getElementById('pmsg').textContent='';
  document.getElementById('presult').innerHTML=`<div style="margin-top:8px">tenant <b>${j.tenant}</b> · domain <b>${j.domain}</b> · profile <b>${j.profileId}</b> · ${j.sources.length} repo(s)</div>
    <table style="margin-top:6px"><tr><th>repo</th><th>category</th><th>sha</th></tr>${j.sources.map(s=>`<tr><td>${s.repo}</td><td>${s.category||''}</td><td><code>${(s.version||'?').slice(0,8)}</code></td></tr>`).join('')}</table>`;
}
load();
</script></body></html>"""


@app.get("/content", response_class=HTMLResponse)
async def content_page():
    """Content delivery console — profiles + delivery table + preview. Page public; API writer-gated."""
    return HTMLResponse(_CONTENT_PAGE)

DASHBOARD_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")

pool: redis.Redis | None = None


@app.on_event("startup")
async def startup():
    global pool
    pool = redis.from_url(REDIS_URL, decode_responses=True)
    log.info("Dashboard connected to Redis")
    # 5-min ops snapshot (tenants/trainings/workers/queues) → COE gauges + log line.
    if _OTEL_ACTIVE:
        try:
            from dashboard import ops_snapshot
            asyncio.get_running_loop().create_task(ops_snapshot.snapshot_loop(pool))
        except Exception as exc:  # telemetry must never block operations
            log.warning("ops snapshot start failed (continuing without): %s", exc)


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.aclose()


# ── Role gating ──────────────────────────────────────────────────────────────
# oauth2-proxy authenticates the user (must be a GH_ORG member to sign in) and
# nginx forwards X-Auth-User on protected paths. Inside that set, we further
# split between 'writer' (org member with role admin/member, allowed to act)
# and 'guest' (read-only). The org-role check is cached 10 minutes per user
# in Redis to avoid hitting the GH API on every request.

async def _resolve_role(user: str) -> dict:
    """Return {role, org_role} for a GitHub username.

    Trust model: oauth2-proxy is configured with ``github_org = <GH_ORG>``,
    which means a valid session cookie already guarantees the caller is an
    active org member. nginx only sets ``X-Auth-User`` after that check
    succeeds, so by the time we see a username here the caller is already
    a member — they are a 'writer'.

    We additionally try ``/orgs/{org}/memberships/{user}`` to enrich the
    response with org_role (admin/member). The lookup needs a token with
    'Members: read' on the org; if the token lacks that scope (403/404)
    we still return writer because oauth2-proxy did the authoritative
    check already. role is only 'guest' when there is no authenticated
    user (empty username).
    """
    if not user:
        return {"role": "guest", "org_role": "", "user": ""}

    cache_key = f"auth:role:{user}"
    cached = await pool.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    org_role = ""
    if GH_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{GH_API}/orgs/{GH_ORG}/memberships/{user}",
                    headers={
                        "Authorization": f"Bearer {GH_TOKEN}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("state") == "active":
                        org_role = data.get("role", "member")
                elif resp.status_code in (403, 404):
                    # Token lacks Members:read scope, or user not found.
                    # Don't downgrade to guest — oauth2-proxy already vouched.
                    log.info(
                        "org-role enrich for %s skipped (HTTP %d) — "
                        "trusting oauth2-proxy session",
                        user, resp.status_code,
                    )
        except Exception as e:
            log.warning("org-role lookup for %s failed: %s", user, e)

    payload = {
        # Authenticated via oauth2-proxy ⇒ org member ⇒ writer.
        "role": "writer",
        "org_role": org_role or "member",
        "user": user,
    }
    try:
        await pool.set(cache_key, json.dumps(payload), ex=600)
    except Exception:
        pass
    return payload


async def _require_writer(request: Request) -> dict:
    """FastAPI dependency-style guard for action endpoints.

    Returns the resolved role payload. Raises 401/403 if the caller is not
    a writer. nginx sets X-Auth-User only after oauth2-proxy validates the
    session; without that header we treat the request as anonymous.
    """
    user = request.headers.get("x-auth-user", "")
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "sign_in": "/oauth2/sign_in"},
        )
    role = await _resolve_role(user)
    if role.get("role") != "writer":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "user": user,
                "reason": f"User {user} is not a member of {GH_ORG}; "
                          "actions are restricted to org members.",
            },
        )
    return role


# ── Service bearer + PII masking (public reads) ──────────────────────────────
# The Dynatrace app's `orbital` app function sends `Authorization: Bearer
# <token>` (from the tenant's orbital-config app-settings secret). ORBITAL_TOKEN
# in /home/ops/.env holds the accepted value(s) (comma-separated to allow
# rotation). Callers presenting it are "service" callers and get FULL payloads;
# signed-in org members (nginx-verified X-Auth-User) also get full payloads;
# everyone else is anonymous and gets emails/tenants masked (dashboard is
# public — see dashboard/masking.py).
#
# X-Auth-User is only trustworthy on nginx locations that either hard-gate
# (auth_request) or opportunistically set AND clear it — the catch-all and
# arena locations clear it explicitly so it cannot be spoofed by clients.

ORBITAL_TOKENS = tuple(
    t.strip() for t in os.environ.get("ORBITAL_TOKEN", "").split(",") if t.strip())


def _is_service_caller(request: Request) -> bool:
    """True when the request carries a bearer matching a configured
    ORBITAL_TOKEN. Always False when no token is configured (fail closed:
    an empty config must not turn every caller into a service caller)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    presented = auth[7:].strip()
    return any(hmac.compare_digest(presented, t) for t in ORBITAL_TOKENS)


def _has_full_access(request: Request) -> bool:
    """Masking decision for public reads: service bearer OR a signed-in org
    member (X-Auth-User is set by nginx only after oauth2-proxy validated
    the session, and cleared on anonymous fallbacks)."""
    return bool(request.headers.get("x-auth-user", "")) or _is_service_caller(request)


async def _require_service_or_writer(request: Request) -> None:
    """Auth gate for live-session write endpoints: the app's service bearer
    OR a signed-in writer. 401 otherwise (403 for a signed-in non-member)."""
    if _is_service_caller(request):
        return
    if request.headers.get("x-auth-user", ""):
        await _require_writer(request)
        return
    raise HTTPException(
        status_code=401,
        detail={"error": "unauthorized",
                "reason": "this endpoint requires the Orbital service bearer "
                          "or a signed-in org member session"},
    )


async def _require_arena_auth(request: Request) -> None:
    """Auth gate for the arena session endpoints (provision, exec, shell-token,
    terminate, session lookups) — with a COMPATIBILITY WINDOW.

    Passes for the app's service bearer (orbitalFetch sends it from the
    tenant's orbital-config secret) or a signed-in org member. While
    ARENA_AUTH_ENFORCE != "1" (the compat window), anonymous callers are STILL
    ALLOWED but logged loudly (ARENA-LEGACY-CALLER) so every remaining
    unauthenticated caller can be inventoried from the journal before
    enforcement flips. Setting ARENA_AUTH_ENFORCE=1 in /home/ops/.env turns
    anonymous access into a 401.

    The env var is read per-request on purpose: flipping enforcement must not
    depend on module import order, and tests can toggle it directly.
    """
    if _is_service_caller(request):
        return
    if request.headers.get("x-auth-user", ""):
        await _require_writer(request)
        return
    if os.environ.get("ARENA_AUTH_ENFORCE", "") != "1":
        client_ip = (request.headers.get("x-real-ip", "")
                     or (request.client.host if request.client else "unknown"))
        log.warning("ARENA-LEGACY-CALLER: %s %s from %s",
                    request.method, request.url.path, client_ip)
        return
    raise HTTPException(
        status_code=401,
        detail={"error": "unauthorized",
                "reason": "this endpoint requires the Orbital service bearer "
                          "or a signed-in org member session"},
    )


@app.get("/api/auth/role")
async def api_auth_role(request: Request):
    """Resolve the caller's role for the dashboard UI.

    Returns 'guest' if not signed in or not a member of the org; 'writer'
    if the user is a verified org member. The frontend uses this to hide
    or disable action buttons for guests.
    """
    user = request.headers.get("x-auth-user", "")
    if not user:
        return {"role": "guest", "org_role": "", "user": ""}
    return await _resolve_role(user)


# ── UI Routes ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Fleet overview dashboard."""
    # no-cache: the HTML carries no version hint, so without this browsers may
    # heuristically cache it and keep serving a stale page (e.g. after a deploy
    # fix). The doc is tiny; always revalidate so users get current markup +
    # the current ?v= asset references.
    return templates.TemplateResponse(request, "index.html",
                                      headers={"Cache-Control": "no-cache, must-revalidate"})


# ── API Routes ───────────────────────────────────────────────────────────────


def _tag_str(v) -> str:
    """Extract the tag from a fleet:release-tags value (new dict or legacy str)."""
    if isinstance(v, dict):
        return v.get("tag", "")
    return v or ""


def _tag_ts(v) -> float:
    """Extract the fetch timestamp; legacy str values have no ts → 0 (always stale)."""
    if isinstance(v, dict):
        try:
            return float(v.get("ts", 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


@app.get("/api/repos")
async def api_repos():
    """List all repos with the latest build matrix.

    Merges two data sources:
      - ``jobs:completed``     — local worker results (primary, links to /api/jobs/<id>/log)
      - ``ci:<repo>:*:main``   — GHA workflow_run events (used as fallback)
    """
    import yaml

    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    completed_raw = await pool.lrange("jobs:completed", -1500, -1)
    local_matrix: dict[str, dict] = {}
    for raw in completed_raw:
        job = json.loads(raw)
        if job.get("type") != "integration-test":
            continue
        repo = job["repo"]
        arch = job.get("arch") or job.get("result", {}).get("arch") or job.get("worker_arch") or "arm64"
        result = job.get("result", {}) or {}
        local_matrix.setdefault(repo, {})[arch] = {
            "passed": bool(result.get("passed")),
            "status": job.get("status", "completed"),
            "duration": int(result.get("duration_seconds", 0)),
            "finished_at": job.get("finished_at", ""),
            "job_id": job.get("job_id", ""),
            "source": "local",
        }

    # History sparklines: last 10 integration-test builds per (repo, arch)
    history_matrix: dict[str, dict[str, list]] = {}
    for raw in reversed(completed_raw):  # newest first
        try:
            hj = json.loads(raw)
        except Exception:
            continue
        if hj.get("type") != "integration-test":
            continue
        hr = hj.get("repo", "")
        ha = hj.get("arch") or hj.get("result", {}).get("arch") or hj.get("worker_arch") or "arm64"
        hres = hj.get("result", {}) or {}
        history_matrix.setdefault(hr, {}).setdefault(ha, [])
        if len(history_matrix[hr][ha]) < 10:
            history_matrix[hr][ha].append({
                "passed": bool(hres.get("passed")),
                "status": hj.get("status", "completed"),
                "finished_at": hj.get("finished_at", ""),
                "job_id": hj.get("job_id", ""),
            })

    # Pull latest_tag from fleet:release-tags (24 h TTL, populated by the
    # status-summary endpoint on each run so it survives the 5-min status cache).
    async def _fetch_latest_tag(repo_full: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo_full}/releases/latest",
                    headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
                )
                if resp.is_success:
                    return repo_full, resp.json().get("tag_name", "")
        except Exception:
            pass
        return repo_full, ""

    # Per-repo tag cache: {repo: {"tag": str, "ts": epoch_seconds}}.
    # Stale entries (older than the refresh window) are re-fetched so a new
    # GitHub release surfaces within the window instead of waiting out the 24 h
    # key TTL. Legacy plain-string values are tolerated (treated as stale).
    FLEET_TAG_REFRESH_S = 1800  # 30 min

    release_map: dict = {}
    try:
        cached_tags = await pool.get("fleet:release-tags")
        if cached_tags:
            release_map = json.loads(cached_tags)
    except Exception:
        pass

    # Fleet shows only sync-managed repos. sync_managed: false repos (e.g.
    # workshop-destination-automation) are not part of Orbital/CI/sync — they
    # can still appear on the GitHub Pages registry via generate-json.
    displayed_repos = [
        r["repo"] for r in data.get("repos", [])
        if r.get("status") == "active"
        and r.get("listed") is not False
        and r.get("sync_managed") is not False
    ]

    # Re-fetch repos missing from cache OR whose cached tag has gone stale.
    if GH_TOKEN:
        now_ts = datetime.now(timezone.utc).timestamp()
        stale = [
            rp for rp in displayed_repos
            if rp not in release_map
            or (now_ts - _tag_ts(release_map.get(rp))) > FLEET_TAG_REFRESH_S
        ]
        if stale:
            results = await asyncio.gather(*[_fetch_latest_tag(rp) for rp in stale])
            updated = False
            for repo, tag in results:
                if tag:
                    release_map[repo] = {"tag": tag, "ts": now_ts}
                    updated = True
            if updated:
                try:
                    await pool.set("fleet:release-tags", json.dumps(release_map), ex=86400)
                except Exception:
                    pass

    repos_out = []
    for r in data.get("repos", []):
        if r.get("status") != "active":
            continue
        if r.get("listed") is False:
            continue
        if r.get("sync_managed") is False:
            continue
        repo_full = r["repo"]
        builds: dict[str, dict] = dict(local_matrix.get(repo_full, {}))

        # Fall back to GHA workflow_run records for any arch we don't have locally
        async for key in pool.scan_iter(match=f"ci:{repo_full}:*:main"):
            wf_data = await pool.hgetall(key)
            if not wf_data:
                continue
            workflow = wf_data.get("workflow", "")
            arch = next((a for a in ("arm64", "amd64") if workflow.lower().endswith(a)), None)
            if not arch or arch in builds:
                continue
            builds[arch] = {
                "passed": wf_data.get("conclusion") == "success",
                "duration": int(wf_data.get("duration_seconds", 0)),
                "finished_at": wf_data.get("finished_at", ""),
                "run_url": wf_data.get("run_url", ""),
                "source": "github-actions",
            }

        repos_out.append({
            "name": r["name"],
            "repo": repo_full,
            "arch": r.get("arch", "both"),
            "duration": r.get("duration", "1h"),
            "ci": r.get("ci", True),
            "builds": builds,
            "history": history_matrix.get(repo_full, {}),
            "latest_tag": _tag_str(release_map.get(repo_full)),
            # App-delivered labs (repos.yaml tag `dynatrace-app`) can run the
            # full e2e training test — gates the "Training test" fleet action.
            "training_test": "dynatrace-app" in (r.get("tags") or []),
        })

    return {"repos": repos_out, "total": len(repos_out)}


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str):
    """Plain-text log for a completed job. Redis (7-day TTL) first, then the
    master's on-disk log file — master-run jobs stay readable forever even
    after the Redis copy expires."""
    from fastapi.responses import PlainTextResponse
    content = await pool.get(f"job:log:{job_id}")
    if content is None and re.fullmatch(r"[A-Za-z0-9._-]+", job_id):
        path = Path("/home/ops/logs") / f"{job_id}.log"
        try:
            content = path.read_text(errors="replace")
        except OSError:
            content = None
    if content is None:
        return PlainTextResponse(
            f"No log found for job {job_id}.\n"
            "Either the job never ran (deferred/cancelled), it ran on a worker "
            "and its 7-day Redis copy expired, or it ran on GitHub Actions "
            "(use the run URL instead).",
            status_code=404,
        )
    return PlainTextResponse(content)


@app.get("/api/workers")
async def api_workers():
    """List registered workers and their status.

    Master ARM worker writes to ``worker:master-arm64`` with role=master;
    AMD agents write to ``worker:<id>`` with role=agent (default). Workers
    are sorted master-first so the dashboard pins the master at the top.
    """
    worker_keys = []
    async for key in pool.scan_iter("worker:*"):
        # Skip port-pool lists (worker:<id>:app_ports_free) — they are Redis
        # lists, not hashes, and would cause a WRONGTYPE error on hgetall.
        if key.endswith(":app_ports_free"):
            continue
        worker_keys.append(key)

    workers = []
    for key in worker_keys:
        try:
            data = await pool.hgetall(key)
        except Exception:
            continue
        if data:
            data["worker_id"] = key.replace("worker:", "")
            data.setdefault("role", "agent")
            workers.append(data)

    # Master first, then alphabetical
    workers.sort(key=lambda w: (0 if w.get("role") == "master" else 1, w["worker_id"]))
    return {"workers": workers, "total": len(workers)}


@app.get("/api/branches/all")
async def api_all_branches():
    """Aggregate the union of branches across all active repos.

    Returns ``{branches: [{name, repos: [...]}]}`` so the UI can offer a
    cross-repo branch picker that shows which repos have a given branch
    (e.g. ``fix/badges-and-rum-ids`` on 9 repos). Each repo's branch list
    is fetched from ``/api/repos/{owner}/{repo}/branches`` (Redis-cached).
    """
    import yaml
    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    active = [
        r["repo"] for r in data.get("repos", [])
        if r.get("status") == "active"
    ]

    branch_to_repos: dict[str, list[str]] = {}

    async def fetch_one(repo_full: str):
        cache_key = f"repo:branches:{repo_full}"
        cached = await pool.get(cache_key)
        if cached:
            try:
                payload = json.loads(cached)
                return repo_full, payload.get("branches", []) or []
            except Exception:
                pass
        owner, repo = repo_full.split("/", 1)
        proc = await asyncio.create_subprocess_exec(
            "gh", "api", f"/repos/{owner}/{repo}/branches", "--paginate",
            "--jq", "[.[] | .name]",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return repo_full, ["main"]
        try:
            branches = json.loads(stdout.decode())
        except Exception:
            branches = ["main"]
        await pool.set(cache_key, json.dumps({"branches": branches}), ex=600)
        return repo_full, branches

    results = await asyncio.gather(
        *(fetch_one(r) for r in active),
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, Exception):
            continue
        repo_full, branches = item
        for b in branches:
            branch_to_repos.setdefault(b, []).append(repo_full)

    rows = []
    for name, repos in branch_to_repos.items():
        rows.append({"name": name, "repos": sorted(repos), "count": len(repos)})
    # main first, then most-shared, then alpha
    rows.sort(key=lambda r: (0 if r["name"] == "main" else 1, -r["count"], r["name"]))
    return {"branches": rows, "total_repos": len(active)}


@app.post("/api/builds/trigger-fleet")
async def api_trigger_fleet(request: Request):
    """Trigger an integration test for a single branch across multiple repos.

    Body: ``{branch: "<name>", arch: "arm64|amd64|both", repos?: [...]}``

    If ``repos`` is omitted, queues a build for every active repo that has
    that branch (per the cached branch list). Returns the list of jobs
    queued and any repos that were skipped because the branch doesn't
    exist on them.
    """
    role = await _require_writer(request)
    body = await request.json()
    branch = (body.get("branch") or "").strip()
    arch = body.get("arch", "both")
    explicit = body.get("repos") or []
    if not branch:
        raise HTTPException(400, "branch is required")

    # Resolve the candidate repo set. If `repos` is provided, validate each
    # actually has the branch (using the cached branch list).
    aggregate = await api_all_branches()
    by_branch = {b["name"]: b["repos"] for b in aggregate["branches"]}
    candidates = explicit or by_branch.get(branch, [])
    has_branch = set(by_branch.get(branch, []))
    targets = [r for r in candidates if r in has_branch]
    skipped = [r for r in candidates if r not in has_branch]

    arches = ["arm64", "amd64"] if arch == "both" else [arch]
    timestamp = datetime.now(timezone.utc).isoformat()
    queued = []
    fleet_run_id = f"fleet-{int(datetime.now(timezone.utc).timestamp())}"
    for repo in targets:
        for a in arches:
            job = {
                "type": "integration-test",
                "repo": repo,
                "arch": a,
                "queue": f"test:{a}",
                "ref": branch,
                "timestamp": timestamp,
                "trigger": "fleet",
                "nightly_run_id": fleet_run_id,
                "requested_by": role["user"],
                # Provenance persisted into the job record (resolvable by job id).
                "user": role["user"],
                "tenant_user": request.headers.get("x-auth-email", ""),
            }
            await pool.rpush(f"queue:test:{a}", json.dumps(job))
            queued.append({"repo": repo, "arch": a})

    return {
        "status": "queued",
        "branch": branch,
        "fleet_run_id": fleet_run_id,
        "queued": queued,
        "skipped_no_branch": skipped,
        "requested_by": role["user"],
    }


@app.post("/api/ghpages/trigger")
async def api_trigger_ghpages(request: Request):
    """Queue a local deploy-ghpages job for a single repo+branch.

    Body: ``{repo: "owner/name", ref: "<branch>"}``
    Runs the same steps as deploy-ghpages.yaml on the local worker.
    """
    role = await _require_writer(request)
    body = await request.json()
    repo = (body.get("repo") or "").strip()
    ref  = (body.get("ref") or "main").strip()
    if not repo:
        raise HTTPException(400, "repo is required")

    ts     = int(time.time() * 1000)
    job_id = f"deploy-ghpages-{repo.split('/')[-1]}-{ts}"
    job    = {
        "job_id":       job_id,
        "type":         "deploy-ghpages",
        "repo":         repo,
        "ref":          ref,
        "branch":       ref,
        "trigger":      "dashboard",
        "requested_by": role["user"],
        "timestamp":    datetime.utcnow().isoformat(),
    }
    await pool.rpush("queue:agent", json.dumps(job))
    log.info("GH Pages queued: %s @ %s by %s (job_id=%s)", repo, ref, role["user"], job_id)
    return {"status": "queued", "job_id": job_id, "repo": repo, "ref": ref}


@app.post("/api/ghpages/trigger-fleet")
async def api_trigger_ghpages_fleet(request: Request):
    """Queue local deploy-ghpages jobs for every fleet repo that has the chosen branch.

    Body: ``{branch: "<name>", repos?: [...]}``
    Each repo gets its own job queued to queue:agent.
    """
    role = await _require_writer(request)
    body = await request.json()
    branch   = (body.get("branch") or "").strip()
    explicit = body.get("repos") or []
    if not branch:
        raise HTTPException(400, "branch is required")

    aggregate = await api_all_branches()
    by_branch = {b["name"]: b["repos"] for b in aggregate["branches"]}
    candidates = explicit or by_branch.get(branch, [])
    has_branch = set(by_branch.get(branch, []))
    targets    = [r for r in candidates if r in has_branch]
    skipped    = [r for r in candidates if r not in has_branch]

    queued: list[str] = []
    ts = int(time.time() * 1000)
    for repo_full in targets:
        job_id = f"deploy-ghpages-{repo_full.split('/')[-1]}-{ts}"
        job    = {
            "job_id":       job_id,
            "type":         "deploy-ghpages",
            "repo":         repo_full,
            "ref":          branch,
            "branch":       branch,
            "trigger":      "dashboard",
            "requested_by": role["user"],
            "timestamp":    datetime.utcnow().isoformat(),
        }
        await pool.rpush("queue:agent", json.dumps(job))
        queued.append(repo_full)

    log.info(
        "GH Pages fleet queued: branch=%s queued=%d skipped=%d by=%s",
        branch, len(queued), len(skipped), role["user"],
    )
    return {
        "status":              "queued",
        "branch":              branch,
        "dispatched":          queued,
        "dispatched_count":    len(queued),
        "errors":              [],
        "skipped_no_branch":   skipped,
        "requested_by":        role["user"],
    }


@app.post("/api/agent/fix-ci")
async def api_agent_fix_ci(request: Request):
    """Queue a fix-ci agent job for a failed integration test. Restricted to sergiohinojosa."""
    role = await _require_writer(request)
    if role.get("user") != "sergiohinojosa":
        raise HTTPException(status_code=403, detail="Fix-with-AI is currently restricted to sergiohinojosa")
    body = await request.json()
    repo        = (body.get("repo") or "").strip()
    branch      = (body.get("branch") or "main").strip()
    arch        = (body.get("arch") or "arm64").strip()
    failed_job_id = (body.get("failed_job_id") or "").strip()
    failed_step   = (body.get("failed_step") or "").strip()
    instructions  = (body.get("instructions") or "").strip()

    if not repo:
        raise HTTPException(400, "repo is required")

    # Fetch the failed log from Redis for the agent to analyze
    failed_log = ""
    if failed_job_id:
        raw = await pool.get(f"job:log:{failed_job_id}")
        if raw:
            # Cap at 12KB — enough context, won't blow up the prompt
            failed_log = raw[-12288:] if len(raw) > 12288 else raw

    import uuid as _uuid
    ts = int(time.time() * 1000)
    repo_name = repo.split("/")[-1]
    job_id = f"fix-ci-{repo_name}-{ts}-{_uuid.uuid4().hex[:6]}"

    job = {
        "job_id":        job_id,
        "type":          "fix-ci",
        "repo":          repo,
        "ref":           branch,
        "branch":        branch,
        "arch":          arch,
        "trigger":       "dashboard",
        "requested_by":  role["user"],
        "timestamp":     datetime.utcnow().isoformat(),
        "failed_job_id": failed_job_id,
        "failed_log":    failed_log,
        "failed_step":   failed_step,
        "instructions":  instructions,
    }

    await pool.rpush("queue:agent", json.dumps(job))
    log.info("Queued fix-ci agent job %s for %s@%s by %s", job_id, repo, branch, role["user"])
    return {"job_id": job_id, "status": "queued", "repo": repo, "branch": branch}


@app.post("/api/agent/fix-pr")
async def api_agent_fix_pr(request: Request):
    """Queue a fix-ci agent job scoped to an open PR. Restricted to sergiohinojosa."""
    role = await _require_writer(request)
    user = role.get("user", "")
    if user != "sergiohinojosa":
        raise HTTPException(status_code=403, detail="Fix-with-AI is currently restricted to sergiohinojosa")
    body = await request.json()
    repo         = (body.get("repo") or "").strip()
    pr_number    = body.get("pr_number")
    branch       = (body.get("branch") or "main").strip()
    instructions = (body.get("instructions") or "").strip()
    if not repo or not pr_number:
        raise HTTPException(400, "repo and pr_number are required")

    # Fetch the most recent failed GHA log (Test Codespace / devcontainer) for this repo+branch
    failed_log = await _fetch_gha_failed_log(repo, branch)
    failed_job_id = ""

    import uuid as _uuid
    ts = int(time.time() * 1000)
    repo_name = repo.split("/")[-1]
    job_id = f"fix-pr-{repo_name}-{ts}-{_uuid.uuid4().hex[:6]}"
    job = {
        "job_id":        job_id,
        "type":          "fix-ci",
        "repo":          repo,
        "ref":           branch,
        "branch":        branch,
        "arch":          "arm64",
        "trigger":       "dashboard",
        "requested_by":  user,
        "git_author_email": "hj.sergio@gmail.com",
        "timestamp":     datetime.utcnow().isoformat(),
        "pr_number":     pr_number,
        "failed_job_id": failed_job_id,
        "failed_log":    failed_log,
        "instructions":  instructions,
        "context":       "fix-pr",
    }
    await pool.rpush("queue:agent", json.dumps(job))
    log.info("Queued fix-pr job %s for %s PR#%s by %s", job_id, repo, pr_number, user)
    return {"job_id": job_id, "status": "queued", "repo": repo, "pr_number": pr_number}


@app.post("/api/agent/fix-issue")
async def api_agent_fix_issue(request: Request):
    """Queue a fix-issue agent job. Restricted to sergiohinojosa."""
    role = await _require_writer(request)
    user = role.get("user", "")
    if user != "sergiohinojosa":
        raise HTTPException(status_code=403, detail="Fix-with-AI is currently restricted to sergiohinojosa")
    body = await request.json()
    repo         = (body.get("repo") or "").strip()
    issue_number = body.get("issue_number")
    instructions = (body.get("instructions") or "").strip()
    if not repo or not issue_number:
        raise HTTPException(400, "repo and issue_number are required")

    # Fetch the issue title + body so the agent has the actual report to triage.
    # Without this the prompt builder has no title/body (the failure that crashed
    # fix-issue with KeyError: 'title').
    meta = await _fetch_issue_meta(repo, issue_number)

    import uuid as _uuid
    ts = int(time.time() * 1000)
    repo_name = repo.split("/")[-1]
    job_id = f"fix-issue-{repo_name}-{ts}-{_uuid.uuid4().hex[:6]}"
    job = {
        "job_id":        job_id,
        "type":          "fix-issue",
        "repo":          repo,
        "ref":           "main",
        "branch":        "main",
        "arch":          "arm64",
        "trigger":       "dashboard",
        "requested_by":  user,
        "git_author_email": "hj.sergio@gmail.com",
        "timestamp":     datetime.utcnow().isoformat(),
        "issue_number":  issue_number,
        "title":         meta["title"],
        "body":          meta["body"],
        "issue_url":     meta["url"],
        "instructions":  instructions,
        "context":       "fix-issue",
    }
    await pool.rpush("queue:agent", json.dumps(job))
    log.info("Queued fix-issue job %s for %s #%s (%r) by %s",
             job_id, repo, issue_number, meta["title"][:60], user)
    return {"job_id": job_id, "status": "queued", "repo": repo, "issue_number": issue_number}


@app.get("/api/builds/running")
async def api_builds_running(request: Request):
    """Currently executing tests, plus pending queue depths.

    Workers write a ``job:running:<run_id>`` HASH when they pick up a job and
    delete it when done. Concurrency per (repo, branch, arch) is enforced via
    ``running:lock:<triple>`` STRING keys (see workers/manager.py and
    worker-agent/agent.py).

    Public endpoint — anonymous callers get arena_user/arena_tenant masked
    (learner emails + tenant URLs are PII); signed-in org members and the
    app's service bearer get full values.
    """
    full = _has_full_access(request)
    queues = {}
    for arch in ("arm64", "amd64"):
        queues[arch] = await pool.llen(f"queue:test:{arch}")
    queues["agent"] = await pool.llen("queue:agent")
    queues["sync"]  = await pool.llen("queue:sync")

    running = []
    async for key in pool.scan_iter(match="job:running:*"):
        # Tolerate the legacy STRING shape until all workers are on the
        # post-lock-fix code. New shape is HASH at job:running:{run_id};
        # legacy is STRING at job:running:{repo}:{arch}.
        key_type = await pool.type(key)
        if key_type == "hash":
            meta = await pool.hgetall(key)
            if not meta or not meta.get("repo"):
                continue
            running.append({
                "repo": meta.get("repo"),
                "arch": meta.get("arch"),
                "branch": meta.get("branch"),
                "job_id": meta.get("job_id"),
                "ref": meta.get("ref"),
                "started_at": meta.get("started_at"),
                "worker_id": meta.get("worker_id"),
                "type": meta.get("type", "integration-test"),
                "arena_user": meta.get("arena_user") if full
                              else masking.mask_email(meta.get("arena_user")),
                "arena_tenant": meta.get("arena_tenant") if full
                                else masking.mask_tenant(meta.get("arena_tenant")),
                "provider": meta.get("provider", "sysbox"),
                "stage": meta.get("stage"),
            })
        elif key_type == "string":
            parts = key.split(":", 3)
            if len(parts) < 4:
                continue
            repo, arch = parts[2], parts[3]
            raw = await pool.get(key)
            try:
                meta = json.loads(raw) if raw else {}
            except Exception:
                meta = {}
            running.append({
                "repo": repo,
                "arch": arch,
                "branch": meta.get("ref"),
                "job_id": meta.get("job_id"),
                "ref": meta.get("ref"),
                "started_at": meta.get("started_at"),
                "worker_id": meta.get("worker_id"),
                "type": meta.get("type", "integration-test"),
            })

    # Surface deferred jobs so the dashboard can show "queued behind a running test"
    deferred = []
    async for key in pool.scan_iter(match="deferred:*"):
        triple = key.split(":", 1)[1]
        depth = await pool.llen(key)
        if depth:
            deferred.append({"triple": triple, "depth": depth})

    return {"queues": queues, "running": running, "deferred": deferred}


@app.get("/api/jobs/{job_id}/livelog")
async def api_job_livelog(job_id: str):
    """Plain-text live log for an in-flight test (updated ~1s by the worker).

    Returns 404 if the job is no longer in the running set — even if the
    livelog Redis key hasn't expired yet — so the dashboard correctly falls
    back to the final stored log and hides the Terminate button.
    """
    from fastapi.responses import PlainTextResponse
    if not await pool.exists(f"job:running:{job_id}"):
        return PlainTextResponse("(no livelog — job may have finished)", status_code=404)
    content = await pool.get(f"job:livelog:{job_id}")
    if content is None:
        return PlainTextResponse("(no livelog — job may have finished)", status_code=404)
    return PlainTextResponse(content)


@app.post("/api/jobs/{job_id}/terminate")
async def api_terminate_job(job_id: str, request: Request):
    """Request termination of a running job.

    Publishes the job_id on the ``ops:terminate`` pub/sub channel; whichever
    worker owns the job kills its Sysbox container, marks status='terminated',
    and runs the normal cleanup path (DEL running:lock, DEL job:running, drain
    deferred). Returns 404 if the job is not currently running. Writer role
    required.
    """
    role = await _require_writer(request)
    if not await pool.exists(f"job:running:{job_id}"):
        # Check if it completed — gives a friendlier message than a bare 404.
        in_completed = await pool.exists(f"job:log:{job_id}")
        detail = (
            f"Job {job_id} has already completed — check the History tab for results."
            if in_completed else
            f"Job {job_id} is not running (it may have completed or never started)."
        )
        raise HTTPException(404, detail)
    requested_by = role["user"]

    # Revoke provisioned DT tokens for this session (best-effort, non-blocking)
    meta = await pool.hgetall(f"job:running:{job_id}")
    # Only Orbital-minted tokens carry a stored auth cred here. App-minted tokens
    # (multi-tenancy) are revoked by the app itself — Orbital holds no tenant cred.
    if meta.get("mint_kind") == "platform" and meta.get("dt_token_ids") and meta.get("dt_tenant_url"):
        # Orbital-minted platform tokens (gen3) — revoke via the account OAuth client (from env).
        try:
            token_ids = json.loads(meta["dt_token_ids"])
            prov = _gen3_platform_provisioner(meta["dt_tenant_url"])
            if prov:
                asyncio.create_task(prov.revoke_tokens(token_ids))
                log.info("Revoking %d platform token(s) for session %s", len(token_ids), job_id)
        except Exception as exc:
            log.warning("Could not initiate platform-token revocation for %s: %s", job_id, exc)
    elif (meta.get("dt_token_ids") and meta.get("dt_tenant_url")
            and (meta.get("dt_auth_token") or meta.get("dt_oauth_client_id"))):
        from provisioning import DTTokenProvisioner
        try:
            token_ids = json.loads(meta["dt_token_ids"])
            provisioner = DTTokenProvisioner(
                tenant_url=meta["dt_tenant_url"],
                api_token=meta.get("dt_auth_token", ""),
                oauth_client_id=meta.get("dt_oauth_client_id", ""),
                oauth_client_secret=meta.get("dt_oauth_client_secret", ""),
            )
            asyncio.create_task(provisioner.revoke_tokens(token_ids))
            log.info("Revoking %d DT token(s) for session %s", len(token_ids), job_id)
        except Exception as exc:
            log.warning("Could not initiate token revocation for %s: %s", job_id, exc)

    # Codespace jobs have no Sysbox worker to signal — delete the Codespace directly
    # (as the learner, via their stored GitHub token) instead of publishing ops:terminate.
    if meta.get("provider") == "codespace":
        from dashboard.codespace_service import delete_codespace
        try:
            await delete_codespace(meta.get("dtUser", ""), job_id)
        except Exception as exc:
            log.warning("Codespace delete failed for %s: %s", job_id, exc)
            raise HTTPException(502, f"Could not delete codespace {job_id}: {exc}")
        # Record the session in history (machine size, tenant, creation-log pointer)
        # before the running hash is gone, so the History tab shows Codespace sessions.
        try:
            hist = {
                "job_id": job_id, "type": "codespace", "provider": "codespace",
                "repo": meta.get("repo", ""), "ref": meta.get("ref", ""),
                "status": "terminated", "machine": meta.get("machine", ""),
                "machine_display": meta.get("machine_display", ""),
                "tenant": meta.get("arena_tenant", ""), "stage": meta.get("stage", ""),
                "arena_user": meta.get("arena_user", ""), "web_url": meta.get("web_url", ""),
                "started_at": meta.get("started_at", ""),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "requested_by": requested_by,
            }
            await pool.rpush("jobs:completed", json.dumps(hist))
            await pool.ltrim("jobs:completed", -500, -1)
        except Exception as exc:
            log.warning("Could not write codespace history for %s: %s", job_id, exc)
        await pool.delete(f"job:running:{job_id}")
        log.info("Codespace %s terminated by %s", job_id, requested_by)
        return {"status": "terminated", "job_id": job_id, "requested_by": requested_by}

    # Flag terminating=1 BEFORE publishing so the worker's durable reconciler
    # force-kills the job even if this fire-and-forget pub/sub message is missed
    # (worker restart / dropped connection). Without the flag a missed message
    # leaks the daemon container forever.
    await pool.hset(f"job:running:{job_id}", "terminating", "1")
    await pool.publish("ops:terminate", job_id)
    log.info("Termination requested for %s by %s", job_id, requested_by)
    return {"status": "termination_requested", "job_id": job_id, "requested_by": requested_by}


@app.get("/api/queue/list")
async def api_queue_list(request: Request):
    """Contents of the pending test queues (arm64 + amd64), unordered.

    Public — requested_by (a learner email for queued arena jobs) is masked
    for anonymous callers."""
    full = _has_full_access(request)
    result = []
    for arch in ("arm64", "amd64"):
        items = await pool.lrange(f"queue:test:{arch}", 0, -1)
        for position, raw in enumerate(items):
            try:
                j = json.loads(raw)
            except Exception:
                continue
            result.append({
                "queue":        f"queue:test:{arch}",
                "arch":         arch,
                "position":     position,
                "job_id":       j.get("job_id", ""),
                "repo":         j.get("repo", ""),
                "ref":          j.get("ref") or j.get("branch") or j.get("head_branch") or "main",
                "type":         j.get("type", "integration-test"),
                "queued_at":    j.get("timestamp") or j.get("queued_at"),
                "requested_by": j.get("requested_by", "") if full
                                else masking.mask_email(j.get("requested_by", "")),
            })
    return {"items": result, "total": len(result)}


@app.delete("/api/queue/item")
async def api_queue_delete_item(job_id: str, request: Request):
    """Remove a pending job from a test queue by job_id. Writer role required."""
    await _require_writer(request)
    removed = 0
    for arch in ("arm64", "amd64"):
        items = await pool.lrange(f"queue:test:{arch}", 0, -1)
        for raw in items:
            try:
                j = json.loads(raw)
                if j.get("job_id") == job_id:
                    count = await pool.lrem(f"queue:test:{arch}", 1, raw)
                    removed += count
                    break
            except Exception:
                pass
        if removed:
            break
    if not removed:
        raise HTTPException(404, f"Job {job_id} not found in any test queue")
    log.info("Queue item %s deleted", job_id)
    return {"removed": removed, "job_id": job_id}


@app.delete("/api/queue/clear")
async def api_queue_clear(request: Request, arch: str | None = None):
    """Remove all pending jobs from test queue(s). Writer role required.

    Pass ``?arch=arm64`` or ``?arch=amd64`` to clear a single arch queue;
    omit for both queues.
    """
    await _require_writer(request)
    arches = [arch] if arch in ("arm64", "amd64") else ["arm64", "amd64"]
    total = 0
    cleared: dict[str, int] = {}
    for a in arches:
        key = f"queue:test:{a}"
        count = await pool.llen(key)
        if count:
            await pool.delete(key)
        cleared[a] = count
        total += count
    log.info("Queue cleared: %s (total=%d) by %s", cleared, total, (await _resolve_role(request))["user"])
    return {"cleared": cleared, "total": total}


@app.post("/api/builds/rerun/{job_id}")
async def api_rerun_job(job_id: str, request: Request):
    """Re-queue a completed job from history. Writer role required."""
    role = await _require_writer(request)
    completed_raw = await pool.lrange("jobs:completed", -1500, -1)
    original = None
    for raw in completed_raw:
        try:
            j = json.loads(raw)
            if j.get("job_id") == job_id:
                original = j
                break
        except Exception:
            pass
    if not original:
        raise HTTPException(404, f"Job {job_id} not found in history")
    repo = original.get("repo", "")
    ref = original.get("ref") or original.get("head_branch") or "main"
    arch = original.get("arch") or original.get("result", {}).get("arch") or "arm64"
    job_type = original.get("type", "integration-test")
    if job_type not in ("integration-test",):
        raise HTTPException(400, f"Re-run not supported for job type '{job_type}'")
    new_job_id = _new_job_id()
    new_job = {
        "job_id":    new_job_id,
        "repo":      repo,
        "ref":       ref,
        "arch":      arch,
        "type":      job_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger":   f"rerun-by-{role['user']}",
        "user":        role["user"],
        "tenant_user": request.headers.get("x-auth-email", ""),
    }
    await pool.rpush(f"queue:test:{arch}", json.dumps(new_job))
    log.info("Re-queued %s@%s (%s) as %s by %s", repo, ref, arch, new_job_id, role["user"])
    return {"job_id": new_job_id, "status": "queued", "repo": repo, "ref": ref, "arch": arch}


@app.get("/log/{job_id}", response_class=HTMLResponse)
async def view_log_fullscreen(job_id: str):
    """Standalone fullscreen log viewer for a single job.

    Polls /api/jobs/{job_id}/livelog every 2s; falls back to /log on 404.
    Same ANSI-rendering pipeline as the in-dashboard modal, but its own page
    so users can pop logs out into a separate window/tab and tail at scale.
    """
    return HTMLResponse("""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>log: """ + job_id + """</title>
<style>
  body { margin:0; background:#0d1117; color:#c9d1d9; font:13px/1.5 ui-monospace,monospace; }
  header { padding:8px 14px; background:#161b22; border-bottom:1px solid #30363d;
           display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  header h1 { margin:0; font-size:14px; font-weight:600; flex:1; min-width:200px;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  header .status { padding:2px 8px; border-radius:10px; font-size:11px;
                   background:#1f6feb22; color:#58a6ff; }
  header .status.done { background:#23863622; color:#3fb950; }
  header .status.failed { background:#da363322; color:#f85149; }
  header .status.terminated { background:#d2932922; color:#d29922; }
  header input[type=search] {
    background:#0d1117; border:1px solid #30363d; color:#c9d1d9;
    padding:4px 8px; border-radius:4px; font-size:12px; width:220px;
    font-family:inherit;
  }
  header input[type=search]:focus { outline:none; border-color:#58a6ff; }
  header button {
    background:#21262d; border:1px solid #30363d; color:#c9d1d9;
    padding:3px 9px; border-radius:4px; font-size:11px; cursor:pointer;
    font-family:inherit;
  }
  header button:hover { background:#30363d; }
  header .count { font-family:ui-monospace,monospace; color:#8b949e; min-width:50px; font-size:11px; }
  pre { margin:0; padding:14px; white-space:pre-wrap; word-break:break-word;
        height:calc(100vh - 50px); overflow:auto; }
  pre.nowrap { white-space:pre; word-break:normal; overflow-x:auto; }
  mark.log-match { background:rgba(251,191,36,.32); color:inherit; border-radius:2px; padding:0; }
  mark.log-match.current { background:#58a6ff; color:#06121b; box-shadow:0 0 0 2px rgba(88,166,255,.5); }
  .ansi-bold { font-weight:bold; }
  .ansi-red { color:#f85149; } .ansi-green { color:#3fb950; }
  .ansi-yellow { color:#d29922; } .ansi-blue { color:#58a6ff; }
  .ansi-magenta { color:#bc8cff; } .ansi-cyan { color:#39c5cf; }
  .ansi-white { color:#c9d1d9; } .ansi-gray { color:#8b949e; }
</style>
</head><body>
<header>
  <h1>""" + job_id + """</h1>
  <input type="search" id="search" placeholder="Search… (Enter / Shift+Enter)" autocomplete="off">
  <button id="prev" title="Previous (Shift+Enter)">◀</button>
  <button id="next" title="Next (Enter)">▶</button>
  <span class="count" id="count"></span>
  <button id="wrap" title="Toggle wrap (W)">↩ Wrap</button>
  <span class="status" id="status">running</span>
</header>
<pre id="log">Loading…</pre>
<script>
const JOB_ID = """ + json.dumps(job_id) + """;
const ANSI_RE = /\\x1b\\[([0-9;]*)m/g;
const COLORS = {30:'gray',31:'red',32:'green',33:'yellow',34:'blue',35:'magenta',36:'cyan',37:'white',
                90:'gray',91:'red',92:'green',93:'yellow',94:'blue',95:'magenta',96:'cyan',97:'white'};
function escapeHtml(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function escapeRegex(s){return s.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&')}
function ansiToHtml(text){
  let out='', open=0, last=0;
  text.replace(ANSI_RE,(m,codes,i)=>{
    out += escapeHtml(text.slice(last,i));
    last = i + m.length;
    const parts = codes ? codes.split(';').map(Number) : [0];
    for(const c of parts){
      if(c===0){ while(open-->0) out+='</span>'; open=0; }
      else if(c===1){ out+='<span class="ansi-bold">'; open++; }
      else if(COLORS[c]){ out+='<span class="ansi-'+COLORS[c]+'">'; open++; }
    }
    return m;
  });
  out += escapeHtml(text.slice(last));
  while(open-->0) out+='</span>';
  return out;
}
const pre = document.getElementById('log');
const statusEl = document.getElementById('status');
const searchEl = document.getElementById('search');
const countEl = document.getElementById('count');
const WRAP_KEY = 'fullscreen-log-wrap';
let poll = null, currentHtml = '', term = '', idx = 0, total = 0;

function getWrap(){ return localStorage.getItem(WRAP_KEY) === '1'; }  // default: noWrap
function applyWrap(){
  const w = getWrap();
  pre.classList.toggle('nowrap', !w);
  document.getElementById('wrap').textContent = w ? '↩ Wrap' : '→ NoWrap';
}
function render(scroll){
  if(!term){ pre.innerHTML = currentHtml; total = 0; idx = -1; countEl.textContent = ''; return; }
  const tmp = document.createElement('div');
  tmp.innerHTML = currentHtml;
  const re = new RegExp(escapeRegex(term), 'gi');
  total = 0;
  function walk(n){
    if(n.nodeType === 3){
      const t = n.nodeValue;
      if(!re.test(t)) return;
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while((m = re.exec(t)) !== null){
        if(m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
        const mk = document.createElement('mark');
        mk.className = 'log-match';
        mk.textContent = m[0];
        frag.appendChild(mk);
        total++; last = m.index + m[0].length;
        if(m[0].length === 0) re.lastIndex++;
      }
      if(last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
      n.parentNode.replaceChild(frag, n);
    } else {
      Array.from(n.childNodes).forEach(walk);
    }
  }
  walk(tmp);
  pre.innerHTML = '';
  while(tmp.firstChild) pre.appendChild(tmp.firstChild);
  if(total === 0){ countEl.textContent = '0 / 0'; return; }
  if(idx < 0 || idx >= total) idx = 0;
  highlight(scroll);
}
function highlight(scroll){
  const marks = document.querySelectorAll('#log mark.log-match');
  marks.forEach(m => m.classList.remove('current'));
  countEl.textContent = total ? (idx+1) + ' / ' + total : '0 / 0';
  if(!marks.length) return;
  const cur = marks[idx];
  if(cur){ cur.classList.add('current'); if(scroll) cur.scrollIntoView({block:'center', behavior:'smooth'}); }
}
function move(d){ if(!total) return; idx = (idx + d + total) % total; highlight(true); }

document.getElementById('prev').onclick = ()=>move(-1);
document.getElementById('next').onclick = ()=>move(1);
document.getElementById('wrap').onclick = ()=>{ localStorage.setItem(WRAP_KEY, getWrap() ? '0' : '1'); applyWrap(); };
searchEl.addEventListener('input', ()=>{ term = searchEl.value; idx = 0; render(true); });
searchEl.addEventListener('keydown', e=>{ if(e.key === 'Enter'){ e.preventDefault(); move(e.shiftKey ? -1 : 1); } });
document.addEventListener('keydown', e=>{
  if(e.target === searchEl) return;
  if(e.key === '/'){ e.preventDefault(); searchEl.focus(); }
  else if(e.key === 'w' || e.key === 'W'){ e.preventDefault(); document.getElementById('wrap').click(); }
});

async function tick(){
  try {
    let res = await fetch('/api/jobs/'+JOB_ID+'/livelog');
    let live = true;
    if(res.status===404){
      res = await fetch('/api/jobs/'+JOB_ID+'/log');
      live = false;
      if(poll){ clearInterval(poll); poll=null; }
      const t = await fetch('/api/jobs/'+JOB_ID+'/status').catch(()=>null);
      if(t && t.ok){
        const j = await t.json();
        statusEl.textContent = j.status || 'finished';
        statusEl.className = 'status ' + (j.status||'done');
      } else {
        statusEl.textContent = 'finished';
        statusEl.className = 'status done';
      }
    }
    if(res.ok){
      const text = await res.text();
      const wasAtBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      currentHtml = ansiToHtml(text);
      render(false);                     // don't scroll on auto-refresh
      if(wasAtBottom && !term) pre.scrollTop = pre.scrollHeight;
    }
  } catch(e){}
}
applyWrap();
tick();
poll = setInterval(tick, 2000);
</script>
</body></html>""")


@app.get("/api/jobs/{job_id}/status")
async def api_job_status(job_id: str):
    """Resolve the final status of a job for the fullscreen viewer header.

    Returns the most recent record from jobs:completed matching this id, or
    ``running`` if it's still in flight.
    """
    if await pool.exists(f"job:running:{job_id}"):
        return {"job_id": job_id, "status": "running"}
    completed = await pool.lrange("jobs:completed", -200, -1)
    for raw in reversed(completed):
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("job_id") == job_id:
            return {"job_id": job_id, "status": j.get("status", "completed")}
    return {"job_id": job_id, "status": "unknown"}


# Curated catalog of sync CLI commands surfaced in the Synchronizer tab.
# Destructive commands (tag, release, push-update) are listed but flagged for
# extra confirmation in the UI.
SYNC_COMMANDS = [
    {
        "id": "status",
        "label": "Status",
        "description": "Show framework-version drift across the fleet.",
        "args": ["status", "--json"],
        "destructive": False,
        "icon": "📊",
    },
    {
        "id": "list",
        "label": "List repos",
        "description": "List all registered repos (CI status, framework version pin).",
        "args": ["list"],
        "destructive": False,
        "icon": "📋",
    },
    {
        "id": "list-ci-enabled",
        "label": "List CI-enabled",
        "description": "Only repos with ci: true.",
        "args": ["list", "--ci-enabled"],
        "destructive": False,
        "icon": "✓",
    },
    {
        "id": "list-pr",
        "label": "Open PRs",
        "description": "List open framework-update PRs across the fleet.",
        "args": ["list-pr"],
        "destructive": False,
        "icon": "🔀",
    },
    {
        "id": "ci-status",
        "label": "CI status",
        "description": "Roll-up of CI run status per repo.",
        "args": ["ci-status"],
        "destructive": False,
        "icon": "🟢",
    },
    {
        "id": "validate",
        "label": "Validate",
        "description": "Validate repos.yaml and local repo state.",
        "args": ["validate"],
        "destructive": False,
        "icon": "✔️",
    },
    {
        "id": "diff",
        "label": "Diff (preview push-update)",
        "description": "Preview what push-update would change for the next version.",
        "args": ["diff"],
        "destructive": False,
        "icon": "🔍",
    },
    {
        "id": "list-issues",
        "label": "List issues",
        "description": "Open issues across repos with label filtering.",
        "args": ["list-issues"],
        "destructive": False,
        "icon": "🐛",
    },
    {
        "id": "clone",
        "label": "Clone all repos",
        "description": "Clone (or pull) every sync-managed repo locally.",
        "args": ["clone"],
        "destructive": False,
        "icon": "⬇️",
    },
]


@app.get("/api/sync/commands")
async def api_sync_commands():
    """List curated sync commands available in the UI."""
    return {"commands": SYNC_COMMANDS}


@app.post("/api/sync/run")
async def api_sync_run(request: Request):
    """Enqueue a sync command for execution.

    Body: {"command": "<id>"} where id matches one of SYNC_COMMANDS.
    Enqueues a sync-command job into queue:sync; the worker streams output
    to job:livelog:{job_id} and persists final log to job:log:{job_id}.
    """
    role = await _require_writer(request)
    body = await request.json()
    cmd_id = body.get("command", "")
    spec = next((c for c in SYNC_COMMANDS if c["id"] == cmd_id), None)
    if spec is None:
        raise HTTPException(400, f"Unknown sync command: {cmd_id}")

    requested_by = role["user"]
    timestamp = datetime.now(timezone.utc).isoformat()
    import uuid
    job_id = f"sync-{spec['id']}-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:6]}"

    job = {
        "type": "sync-command",
        "command_id": spec["id"],
        "command_label": spec["label"],
        "args": spec["args"],
        "queue": "sync",
        "timestamp": timestamp,
        "requested_by": requested_by,
        "repo": "dynatrace-wwse/codespaces-framework",  # synthetic for telemetry
        "job_id": job_id,
    }
    await pool.rpush("queue:sync", json.dumps(job))
    return {"status": "queued", "command": spec["id"], "job_id": job_id}


@app.get("/api/sync/history")
async def api_sync_history(limit: int = 50):
    """Past sync command runs. Sync jobs are rare and roll off the 1500-cap jobs:completed
    list, so merge the dedicated sync:jobs:completed archive (cap 200; dedupe by job_id) —
    same pattern as Agentic History. Otherwise 'recent runs' looks empty."""
    completed_raw = await pool.lrange("jobs:completed", -1500, -1)
    sync_raw = await pool.lrange("sync:jobs:completed", -200, -1)
    completed_raw = _merge_agent_history(completed_raw, sync_raw)
    rows = []
    for raw in reversed(completed_raw):
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("type") != "sync-command":
            continue
        result = j.get("result", {}) or {}
        rows.append({
            "job_id": j.get("job_id", ""),
            "command_id": j.get("command_id", ""),
            "command_label": j.get("command_label", ""),
            "status": j.get("status", "completed"),
            "exit_code": result.get("exit_code"),
            "duration": int(result.get("duration_seconds", 0)),
            "started_at": j.get("timestamp"),
            "finished_at": j.get("finished_at"),
            "requested_by": j.get("requested_by", ""),
        })
        if len(rows) >= limit: break
    return {"rows": rows}


# ── Synchronizer live-data tabs ───────────────────────────────────────────────
# These endpoints power the Status / PRs / Issues sub-tabs inside the
# Synchronizer view.  They run gh CLI commands inline (not via the job queue)
# and cache results in Redis for 5 minutes so repeated tab-switches are free.

async def _gh_json(cache_key: str, *gh_args: str, ttl: int = 300) -> dict:
    """Run a gh command, cache JSON result in Redis, return parsed dict."""
    cached = await pool.get(cache_key)
    if cached:
        return json.loads(cached)
    proc = await asyncio.create_subprocess_exec(
        "gh", *gh_args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace")[:500], "rows": []}
    try:
        data = json.loads(stdout.decode())
    except Exception:
        return {"error": "JSON parse error", "rows": [], "raw": stdout.decode()[:500]}
    payload = {"rows": data if isinstance(data, list) else data, "cached_at": datetime.now(timezone.utc).isoformat()}
    await pool.set(cache_key, json.dumps(payload), ex=ttl)
    return payload


@app.get("/api/sync/status-summary")
async def api_sync_status_summary():
    """Framework-version drift across the fleet via sync status --json.

    Runs ``python3 -m sync.cli status --json`` (cached 5 min) and returns the
    parsed rows so the UI can render a sortable drift table without opening a
    log stream.
    """
    cache_key = "sync:status-summary"
    cached = await pool.get(cache_key)
    if cached:
        payload = json.loads(cached)
        # Back-fill fleet:release-tags if it's missing (e.g. after a restart).
        if not await pool.exists("fleet:release-tags"):
            now_ts = datetime.now(timezone.utc).timestamp()
            release_tags = {
                row["repo"]: {"tag": row["latest_tag"], "ts": now_ts}
                for row in payload.get("rows", [])
                if row.get("repo") and row.get("latest_tag")
            }
            if release_tags:
                await pool.set("fleet:release-tags", json.dumps(release_tags), ex=86400)
        return payload

    sync_dir = FRAMEWORK_DIR
    proc = await asyncio.create_subprocess_exec(
        "python3", "-m", "sync.cli", "status", "--json",
        cwd=str(sync_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(sync_dir), "PYTHONUNBUFFERED": "1"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "sync status timed out after 60 s", "rows": []}
    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace")[:500], "rows": []}
    # Output may contain non-JSON preamble lines; find first '[' or '{'
    raw = stdout.decode(errors="replace")
    json_start = next((i for i, c in enumerate(raw) if c in ("[", "{")), None)
    if json_start is None:
        return {"error": "No JSON in sync status output", "rows": [], "raw": raw[:500]}
    try:
        data = json.loads(raw[json_start:])
    except Exception as exc:
        return {"error": f"JSON parse: {exc}", "rows": [], "raw": raw[:500]}
    rows = data if isinstance(data, list) else data.get("repos", data.get("rows", []))
    payload = {"rows": rows, "cached_at": datetime.now(timezone.utc).isoformat()}
    await pool.set(cache_key, json.dumps(payload), ex=300)
    # Also persist a long-lived repo→tag map used by /api/repos (survives the
    # 5-min status cache so the fleet page always shows release tags).
    now_ts = datetime.now(timezone.utc).timestamp()
    release_tags = {
        row["repo"]: {"tag": row["latest_tag"], "ts": now_ts}
        for row in rows
        if row.get("repo") and row.get("latest_tag")
    }
    if release_tags:
        await pool.set("fleet:release-tags", json.dumps(release_tags), ex=86400)
    return payload


async def _fetch_gha_failed_log(repo: str, branch: str) -> str:
    """Fetch failed-step logs from GitHub Actions for repo+branch.

    Finds the most recent failed 'Test Codespace (devcontainer)' run and
    returns its --log-failed output (build container / start container /
    integration.sh steps), capped at 16 KB.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", "run", "list",
        "--repo", repo,
        "--branch", branch,
        "--limit", "10",
        "--json", "databaseId,name,conclusion,status",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0 or not stdout:
        return ""
    try:
        runs = json.loads(stdout.decode())
    except Exception:
        return ""

    run_id = None
    for run in runs:
        if run.get("conclusion") not in ("failure", "timed_out"):
            continue
        name = (run.get("name") or "").lower()
        if not run_id:
            run_id = str(run["databaseId"])
        if "codespace" in name or "devcontainer" in name:
            run_id = str(run["databaseId"])
            break

    if not run_id:
        return ""

    proc = await asyncio.create_subprocess_exec(
        "gh", "run", "view", run_id,
        "--repo", repo,
        "--log-failed",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0 or not stdout:
        return ""

    log_text = stdout.decode(errors="replace")
    return log_text[-16384:] if len(log_text) > 16384 else log_text


async def _fetch_issue_meta(repo: str, issue_number) -> dict:
    """Fetch a GitHub issue's title + body at trigger time (uncached — must be fresh).

    Returns {"title": str, "body": str, "state": str, "url": str}. On any failure
    returns empty strings so the caller can still enqueue (the agent degrades to a
    title-less prompt rather than the job crashing). Never raises.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "title,body,state,url",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("gh issue view timed out for %s #%s", repo, issue_number)
        return {"title": "", "body": "", "state": "", "url": ""}
    if proc.returncode != 0:
        log.warning("gh issue view failed for %s #%s: %s",
                    repo, issue_number, stderr.decode(errors="replace")[:300])
        return {"title": "", "body": "", "state": "", "url": ""}
    try:
        data = json.loads(stdout.decode())
    except Exception:
        log.warning("gh issue view JSON parse error for %s #%s", repo, issue_number)
        return {"title": "", "body": "", "state": "", "url": ""}
    return {
        "title": data.get("title", "") or "",
        "body":  data.get("body", "") or "",
        "state": data.get("state", "") or "",
        "url":   data.get("url", "") or "",
    }


# GitHub returns these on a burst of concurrent gh calls (secondary rate limit /
# abuse detection) — transient, worth retrying with backoff.
_GH_TRANSIENT_ERRORS = ("401", "Requires authentication", "rate limit", "secondary rate", "abuse")


async def _gh_pr_ci(repo_nwo: str, pr_number: int, retries: int = 3) -> dict:
    """Fetch statusCheckRollup + headRefName for one PR via gh pr view (no cache).

    Retries on transient GitHub API failures (HTTP 401 / secondary rate limit),
    which fire when many gh calls hit the API at once. Returns ``{"_error": ...}``
    after exhausting retries so callers can tell 'CI fetch failed' apart from
    'PR genuinely has no checks' (an empty rollup).
    """
    last_err = ""
    for attempt in range(retries):
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "view", str(pr_number), "-R", repo_nwo,
            "--json", "statusCheckRollup,headRefName",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            try:
                return json.loads(stdout.decode())
            except Exception:
                return {}
        last_err = stderr.decode(errors="replace")
        if not any(s in last_err for s in _GH_TRANSIENT_ERRORS):
            break  # non-transient failure — don't waste retries
        await asyncio.sleep(1.5 * (attempt + 1))
    return {"_error": last_err.strip()[:200] or "gh pr view failed"}


@app.get("/api/sync/prs")
async def api_sync_prs():
    """Open PRs across the org with real GitHub CI status (statusCheckRollup).

    Uses ``gh search prs`` for the list, then enriches each PR in parallel
    with ``gh pr view --json statusCheckRollup,headRefName`` so the CI column
    reflects actual GitHub Actions results (not the ops-server Redis state).
    Cached 5 minutes.
    """
    cache_key = "sync:prs"
    cached = await pool.get(cache_key)
    if cached:
        return json.loads(cached)

    data = await _gh_json(
        "_sync:prs:raw",
        "search", "prs",
        "--owner", GH_ORG,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,repository,author,createdAt,updatedAt,url,labels",
        ttl=60,
    )
    if data.get("error") or not isinstance(data.get("rows"), list):
        return data

    rows = data["rows"]

    # Bound concurrency: firing one gh call per PR all at once trips GitHub's
    # secondary rate limit (HTTP 401), which dropped the CI chip for random PRs.
    sem = asyncio.Semaphore(5)

    # Enrich each PR with GitHub CI status (concurrency-capped)
    async def enrich(pr):
        repo_nwo = (pr.get("repository") or {}).get("nameWithOwner", "")
        if not repo_nwo:
            return
        async with sem:
            detail = await _gh_pr_ci(repo_nwo, pr["number"])
        if detail.get("_error"):
            # Fetch failed (transient API error) — surface as 'unknown', not a
            # missing chip, so the UI shows the status is unavailable, not absent.
            pr["headRefName"] = ""
            pr["_ci"] = {"overall": "unknown", "error": detail["_error"]}
            return
        checks = detail.get("statusCheckRollup") or []
        pr["headRefName"] = detail.get("headRefName", "")
        if not checks:
            pr["_ci"] = None
            return
        conclusions = [c.get("conclusion") or c.get("status") or "" for c in checks]
        failed = [c for c in checks if c.get("conclusion") in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE")]
        if all(c in ("SUCCESS", "NEUTRAL", "SKIPPED") for c in conclusions):
            overall = "pass"
        elif any(c in ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE") for c in conclusions):
            overall = "fail"
        elif any(c in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED") for c in conclusions):
            overall = "pending"
        elif any(c == "CANCELLED" for c in conclusions):
            # CANCELLED after IN_PROGRESS is already caught above; reaching here means
            # the run was cancelled with no new run in flight — treat as failed.
            overall = "fail"
        else:
            overall = "pending"
        pr["_ci"] = {
            "overall": overall,
            "passed": overall == "pass",
            "failed_checks": [c.get("name", "?") for c in failed],
            "checks": checks,
        }

    await asyncio.gather(*(enrich(pr) for pr in rows), return_exceptions=True)

    payload = {"rows": rows, "cached_at": datetime.now(timezone.utc).isoformat()}
    await pool.set(cache_key, json.dumps(payload), ex=300)
    return payload


@app.post("/api/sync/prs/invalidate")
async def api_sync_prs_invalidate(request: Request):
    """Bust the PR cache so the next GET returns fresh data."""
    await _require_writer(request)
    await pool.delete("sync:prs")
    return {"status": "cache cleared"}


@app.get("/api/sync/issues")
async def api_sync_issues():
    """Open issues across the org (cached 5 min)."""
    return await _gh_json(
        "sync:issues",
        "search", "issues",
        "--owner", GH_ORG,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,repository,author,createdAt,updatedAt,url,labels",
    )


@app.post("/api/sync/issues/invalidate")
async def api_sync_issues_invalidate(request: Request):
    """Bust the issues cache."""
    await _require_writer(request)
    await pool.delete("sync:issues")
    return {"status": "cache cleared"}


@app.get("/api/sync/audit")
async def api_sync_audit():
    """Return the latest stored sync validate result."""
    raw = await pool.get("sync:audit:latest")
    if not raw:
        return {"output": None, "timestamp": None, "job_id": None, "exit_code": None}
    try:
        return json.loads(raw)
    except Exception:
        return {"output": raw, "timestamp": None, "job_id": None, "exit_code": None}


_AUDIT_SCRIPT    = FRAMEWORK_DIR / "audit" / "generate-html.py"
_AUDIT_FETCH_SH  = FRAMEWORK_DIR / "audit" / "fetch-data.sh"
_AUDIT_DATA_DIR  = FRAMEWORK_DIR / "audit" / "data"
_AUDIT_CACHE_KEY = "audit:html:cache"
_AUDIT_CACHE_TTL = 3600  # 1 hour


async def _fetch_audit_data() -> bool:
    """Run fetch-data.sh to pull fresh repo data from GitHub. Returns True on success."""
    if not _AUDIT_FETCH_SH.exists():
        log.error("fetch-data.sh not found: %s", _AUDIT_FETCH_SH)
        return False
    gh_token = os.environ.get("GH_TOKEN", "")
    env = {**os.environ, "GH_TOKEN": gh_token, "AUDIT_DATA_DIR": str(_AUDIT_DATA_DIR)}
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(_AUDIT_FETCH_SH),
            cwd=str(_AUDIT_FETCH_SH.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode == 0:
            log.info("Audit data fetch complete")
            return True
        log.warning("fetch-data.sh exited %s: %s", proc.returncode, stdout.decode(errors="replace")[-500:])
    except asyncio.TimeoutError:
        log.error("fetch-data.sh timed out after 5m")
    except Exception as e:
        log.error("fetch-data.sh error: %s", e)
    return False


async def _generate_audit_html() -> str | None:
    """Run generate-html.py and return the generated HTML string."""
    import sys
    import tempfile
    if not _AUDIT_SCRIPT.exists():
        log.error("Audit script not found: %s", _AUDIT_SCRIPT)
        return None
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        out_path = Path(f.name)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_AUDIT_SCRIPT), "--output", str(out_path),
            cwd=str(_AUDIT_SCRIPT.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "AUDIT_DATA_DIR": str(_AUDIT_DATA_DIR)},
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0 and out_path.exists():
            return out_path.read_text(encoding="utf-8")
        log.warning("Audit script exited %s: %s", proc.returncode, stdout.decode(errors="replace"))
    except asyncio.TimeoutError:
        log.error("Audit generation timed out")
    except Exception as e:
        log.error("Audit generation error: %s", e)
    finally:
        out_path.unlink(missing_ok=True)
    return None


@app.get("/audit", response_class=HTMLResponse)
async def view_audit():
    """Serve the rich audit HTML page (cached, regenerated from generate-html.py)."""
    cached = await pool.get(_AUDIT_CACHE_KEY)
    if cached:
        return HTMLResponse(content=cached)
    html = await _generate_audit_html()
    if html:
        await pool.set(_AUDIT_CACHE_KEY, html, ex=_AUDIT_CACHE_TTL)
        return HTMLResponse(content=html)
    return HTMLResponse(content="<p style='color:red;font-family:monospace'>Audit generation failed — check server logs.</p>", status_code=500)


@app.post("/api/audit/refresh")
async def api_audit_refresh(request: Request):
    """Fetch fresh data from GitHub, regenerate audit HTML, update cache (writer only)."""
    await _require_writer(request)
    await pool.delete(_AUDIT_CACHE_KEY)
    fetched = await _fetch_audit_data()
    if not fetched:
        log.warning("fetch-data.sh failed — regenerating from existing data")
    html = await _generate_audit_html()
    if html:
        await pool.set(_AUDIT_CACHE_KEY, html, ex=_AUDIT_CACHE_TTL)
        return {"status": "ok", "fetched": fetched, "message": "Audit refreshed from GitHub." if fetched else "Regenerated from cached data (fetch failed)."}
    raise HTTPException(status_code=500, detail="Audit generation failed — check server logs.")


@app.get("/api/repos/{owner}/{repo}/branches")
async def api_repo_branches(owner: str, repo: str):
    """List remote branches for a repo via GitHub API.

    Cached briefly (10 min) in Redis under ``repo:branches:{owner}/{repo}``
    to avoid hammering the GH API on every dashboard click.
    """
    cache_key = f"repo:branches:{owner}/{repo}"
    cached = await pool.get(cache_key)
    if cached:
        return json.loads(cached)

    branches: list[str] = []
    if GH_TOKEN:
        try:
            headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                page, per_page = 1, 100
                while True:
                    r = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/branches",
                        headers=headers,
                        params={"per_page": per_page, "page": page},
                    )
                    if not r.is_success:
                        break
                    batch = [b["name"] for b in r.json()]
                    branches.extend(batch)
                    if len(batch) < per_page:
                        break
                    page += 1
        except Exception:
            pass

    if not branches:
        branches = ["main"]

    # Sort: main first, then alphabetical
    main_first = [b for b in branches if b == "main"]
    others = sorted([b for b in branches if b != "main"])
    branches = main_first + others
    payload = {"branches": branches}
    await pool.set(cache_key, json.dumps(payload), ex=600)
    return payload


def _infer_started_at(job: dict, result: dict) -> str:
    """Return the best available started_at timestamp for a completed job.

    Older job records may lack a 'timestamp' field (queue time). Fall back to
    computing start = finished_at - duration_seconds so the history table
    always shows a useful date instead of a blank.
    """
    ts = job.get("timestamp") or job.get("started_at")
    if ts:
        return ts
    finished = job.get("finished_at")
    dur = result.get("duration_seconds") or job.get("duration_seconds")
    if finished and dur:
        try:
            fin_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            return (fin_dt - timedelta(seconds=float(dur))).isoformat()
        except Exception:
            return finished
    return finished or ""


def _merge_agent_history(completed_raw: list, agent_raw: list) -> list:
    """Append archived agent-job records that aren't already in jobs:completed
    (dedupe by job_id). Pure — unit-tested. Bad JSON entries are passed through
    untouched (the caller's loop skips them)."""
    if not agent_raw:
        return completed_raw
    seen: set = set()
    for r in completed_raw:
        try:
            seen.add(json.loads(r).get("job_id"))
        except Exception:
            pass
    out = list(completed_raw)
    for r in agent_raw:
        try:
            if json.loads(r).get("job_id") not in seen:
                out.append(r)
        except Exception:
            pass
    return out


@app.get("/api/builds/history")
async def api_builds_history(
    repo: str | None = None,
    arch: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    type: str | None = None,
    limit: int = 200,
):
    """Past runs from ``jobs:completed``, filterable.

    No type param (or type=all) returns all job types.
    Pass ``type=integration-test``, ``type=deploy-ghpages``, etc. to filter.
    ``repo`` is a substring match (case-insensitive) so the search bar works.
    """
    completed_raw = await pool.lrange("jobs:completed", -1500, -1)
    # Merge the dedicated agent-job archive (agent jobs are rare and otherwise roll off
    # the 1500-cap jobs:completed list, leaving Agentic History empty). Dedupe by job_id.
    agent_raw = await pool.lrange("agent:jobs:completed", -300, -1)
    completed_raw = _merge_agent_history(completed_raw, agent_raw)
    rows = []
    distinct_repos: set[str] = set()
    distinct_branches: set[str] = set()
    distinct_arches: set[str] = set()
    repo_lower = repo.lower() if repo else ""
    for raw in reversed(completed_raw):  # newest first
        try:
            j = json.loads(raw)
        except Exception:
            continue
        job_type = j.get("type", "integration-test")
        if type and type != "all" and job_type != type:
            continue
        result = j.get("result", {}) or {}
        row_repo = j.get("repo", "")
        row_arch = j.get("arch") or result.get("arch") or j.get("worker_arch", "") or "unknown"
        row_branch = j.get("ref") or j.get("head_branch") or result.get("ref", "") or "main"
        row_status = j.get("status", "completed")
        distinct_repos.add(row_repo)
        if row_branch: distinct_branches.add(row_branch)
        if row_arch: distinct_arches.add(row_arch)
        if repo_lower and repo_lower not in row_repo.lower(): continue
        if arch and row_arch != arch: continue
        if branch and row_branch != branch: continue
        if status == 'failed':
            # FAIL = non-terminated jobs whose tests didn't pass
            if row_status == 'terminated': continue
            if result.get('passed'): continue
        elif status == 'passed':
            # PASS = completed jobs whose tests passed
            if row_status == 'terminated': continue
            if not result.get('passed'): continue
        elif status and row_status != status:
            continue
        # Trigger inference: nightly if id matches, else dashboard/webhook
        nightly_id = j.get("nightly_run_id", "")
        trigger = j.get("trigger") or (
            "nightly" if nightly_id.startswith("nightly-")
            else ("manual" if nightly_id.startswith("manual") else "")
        ) or "webhook"
        rows.append({
            "job_id": j.get("job_id", ""),
            "repo": row_repo,
            "arch": row_arch,
            "branch": row_branch,
            "status": row_status,
            "passed": bool(result.get("passed")),
            "duration": int(result.get("duration_seconds", 0)),
            "exit_code": result.get("exit_code"),
            "started_at": _infer_started_at(j, result),
            "finished_at": j.get("finished_at"),
            "trigger": trigger,
            "nightly_run_id": nightly_id,
            "worker_id": j.get("worker_id", "master"),
            "type": j.get("type", "integration-test"),
            "result": result,
        })
        if len(rows) >= limit: break
    return {
        "rows": rows,
        "total_returned": len(rows),
        "filters": {
            "repos": sorted(distinct_repos),
            "arches": sorted(distinct_arches),
            "branches": sorted(distinct_branches),
        },
    }


# Job types shown on the Nightly tab. `integration-test` runs the devcontainer
# CI (integration.sh); `training-test` (and its in-container predecessor
# `app-layer-test`) replays the Enablement App learner flow — the UI groups the
# latter two under the "training" category.
NIGHTLY_TEST_TYPES = ("integration-test", "app-layer-test", "training-test")


def _nightly_category(job_type: str) -> str:
    return "integration" if job_type == "integration-test" else "training"


def _nightly_history_key(job: dict) -> str:
    """Sparkline grouping key. Category is part of the key so a repo's training
    history never pollutes its integration history (same repo, same arch)."""
    result = job.get("result", {}) or {}
    arch = job.get("arch") or result.get("arch") or job.get("worker_arch") or "arm64"
    return f"{job.get('repo', '')}|{arch}|{_nightly_category(job.get('type', ''))}"


def _nightly_run_payload(runs: dict[str, list], target_id: str) -> dict:
    """Shared shape for /api/nightly/latest and /api/nightly/run/{id}: results
    for one run (each row tagged with its category) + per-key history built
    across ALL runs, plus overall and per-category pass/fail counts."""
    target_jobs = runs[target_id]

    history: dict[str, list] = {}
    for rid in sorted(runs.keys()):
        for job in runs[rid]:
            result = job.get("result", {}) or {}
            history.setdefault(_nightly_history_key(job), []).append({
                "passed": bool(result.get("passed")),
                "status": job.get("status", "completed"),
                "finished_at": job.get("finished_at", ""),
                "job_id": job.get("job_id", ""),
                "run_id": rid,
            })

    results_out = []
    for job in sorted(target_jobs, key=lambda j: j.get("repo", "")):
        # History = previous nightly runs (exclude the current one), last 7
        hist = [h for h in history.get(_nightly_history_key(job), [])
                if h["run_id"] != target_id][-7:]
        results_out.append({**job, "history": hist,
                            "category": _nightly_category(job.get("type", ""))})

    def _counts(jobs: list) -> dict:
        return {
            "total": len(jobs),
            "passed": sum(1 for j in jobs if j.get("result", {}).get("passed")),
            "failed": sum(1 for j in jobs if not j.get("result", {}).get("passed")),
        }

    integration = [j for j in target_jobs if _nightly_category(j.get("type", "")) == "integration"]
    training = [j for j in target_jobs if _nightly_category(j.get("type", "")) == "training"]
    return {
        "run_id": target_id,
        **_counts(target_jobs),
        "integration": _counts(integration),
        "training": _counts(training),
        "results": results_out,
    }


async def _nightly_runs_map() -> dict[str, list]:
    """All nightly jobs from jobs:completed, grouped by nightly_run_id."""
    completed_raw = await pool.lrange("jobs:completed", -1500, -1)
    runs: dict[str, list] = {}
    for j in completed_raw:
        job = json.loads(j)
        if job.get("type") in NIGHTLY_TEST_TYPES and job.get("nightly_run_id", "").startswith("nightly-"):
            runs.setdefault(job["nightly_run_id"], []).append(job)
    return runs


async def _latest_training_tests() -> tuple[list, dict]:
    """Latest training-test per repo (nightly AND manual triggers) + per-repo
    history. The Training tab shows the CURRENT state of every training —
    superseded per-arch app-layer-test records are excluded (training-test is
    always one amd64 session)."""
    completed_raw = await pool.lrange("jobs:completed", -1500, -1)
    latest: dict[str, dict] = {}
    history: dict[str, list] = {}
    for j in completed_raw:  # chronological — last wins
        job = json.loads(j)
        if job.get("type") != "training-test":
            continue
        repo = job.get("repo", "")
        latest[repo] = job
        result = job.get("result", {}) or {}
        history.setdefault(repo, []).append({
            "passed": bool(result.get("passed")),
            "status": job.get("status", "completed"),
            "finished_at": job.get("finished_at", ""),
            "job_id": job.get("job_id", ""),
            "run_id": job.get("nightly_run_id", ""),
        })
    return sorted(latest.values(), key=lambda j: j.get("repo", "")), history


@app.get("/api/nightly/latest")
async def api_nightly_latest():
    """Latest nightly run results with per-repo build history for sparklines.
    Training rows are the latest training-test per repo across nightly AND
    manual triggers, so a manual rerun updates the board immediately."""
    runs = await _nightly_runs_map()
    training_latest, training_history = await _latest_training_tests()
    if not runs and not training_latest:
        return {"run_id": None, "results": []}
    payload = (_nightly_run_payload(runs, sorted(runs.keys())[-1])
               if runs else {"run_id": None, "total": 0, "passed": 0, "failed": 0,
                             "integration": {"total": 0, "passed": 0, "failed": 0},
                             "results": []})
    # Replace category-training rows (which would be the target nightly's
    # app-layer/per-arch records) with the latest real training-tests.
    results = [r for r in payload["results"] if r.get("category") != "training"]
    for job in training_latest:
        hist = [h for h in training_history.get(job.get("repo", ""), [])
                if h["job_id"] != job.get("job_id", "")][-7:]
        results.append({**job, "history": hist, "category": "training"})
    payload["results"] = results
    payload["training"] = {
        "total": len(training_latest),
        "passed": sum(1 for j in training_latest if j.get("result", {}).get("passed")),
        "failed": sum(1 for j in training_latest if not j.get("result", {}).get("passed")),
    }
    return payload


@app.post("/api/builds/trigger")
async def api_trigger_build(request: Request):
    """Push integration-test jobs into the local worker queue.

    For ``arch=both`` (default), pushes one job to ``queue:test:arm64`` AND
    ``queue:test:amd64`` so both architectures run in parallel.
    The local worker-manager (master ARM) and worker-agent (remote AMD)
    pick the jobs up and execute ``.devcontainer/test/integration.sh``.
    """
    role = await _require_writer(request)
    body = await request.json()
    repo = body["repo"]
    arch = body.get("arch", "both")              # arm64 | amd64 | both
    ref  = body.get("ref", "main")
    requested_by = role["user"]

    job_type = body.get("type", "integration-test")
    if job_type not in ("integration-test", "daemon", "app-layer-test", "training-test"):
        raise HTTPException(400, "type must be integration-test, daemon, app-layer-test, or training-test")

    timestamp = datetime.now(timezone.utc).isoformat()

    if job_type == "training-test":
        # Full e2e training test: a master-side orchestrator provisions a real
        # arena session (always amd64 — /api/arena/provision pins the arch) and
        # drives every doc step through the exec API. One job, no per-arch fanout.
        job = {
            "type": "training-test",
            "repo": repo,
            "arch": "amd64",
            "queue": "training",
            "ref": ref,
            "timestamp": timestamp,
            "trigger": "dashboard",
            "nightly_run_id": f"manual-{int(datetime.now(timezone.utc).timestamp())}",
            "requested_by": requested_by,
        }
        await pool.rpush("queue:training", json.dumps(job))
        return {"status": "queued", "repo": repo, "ref": ref, "type": job_type,
                "requested_by": requested_by,
                "jobs": [{"arch": "amd64", "queue": "queue:training"}]}

    arches = ["arm64", "amd64"] if arch == "both" else [arch]
    queued = []
    for a in arches:
        job = {
            "type": job_type,
            "repo": repo,
            "arch": a,
            "queue": f"test:{a}",
            "ref": ref,
            "timestamp": timestamp,
            "trigger": "dashboard",
            "nightly_run_id": f"manual-{int(datetime.now(timezone.utc).timestamp())}",
            "requested_by": requested_by,
        }
        await pool.rpush(f"queue:test:{a}", json.dumps(job))
        queued.append({"arch": a, "queue": f"queue:test:{a}"})

    return {"status": "queued", "repo": repo, "ref": ref, "type": job_type, "requested_by": requested_by, "jobs": queued}


@app.post("/api/jobs/{job_id}/shell-token")
async def api_shell_token(job_id: str, request: Request):
    """Issue a single-use, 60-second shell token for a running job.

    nginx guards this endpoint with auth_request (writer only).  The token
    is then passed as a query param to the WebSocket endpoint, which has no
    auth_request so nginx doesn't strip the Upgrade header.
    """
    await _require_writer(request)
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="job not running")
    token = secrets.token_hex(16)
    await pool.set(f"shell:token:{token}", job_id, ex=60)
    return {"token": token}


@app.websocket("/ws/jobs/{job_id}/shell")
async def job_shell_ws(ws: WebSocket, job_id: str, token: str = "", rows: int = 24, cols: int = 220):
    """PTY bridge: browser xterm.js ↔ docker exec inside the Sysbox container.

    Auth is via a single-use shell token (issued by /api/jobs/{id}/shell-token
    which is nginx-auth-gated).  The WebSocket location in nginx has no
    auth_request because that module is incompatible with WebSocket upgrades.
    """
    await ws.accept()

    # Validate single-use token atomically: delete it on first use.
    pipe = pool.pipeline(transaction=True)
    pipe.get(f"shell:token:{token}")
    pipe.delete(f"shell:token:{token}")
    stored_id, _ = await pipe.execute()
    if not stored_id or stored_id != job_id:
        await ws.send_bytes(b"\r\n\x1b[31mInvalid or expired shell token.\x1b[0m\r\n")
        await ws.close()
        return

    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        await ws.send_bytes(
            f"\r\n\x1b[31mJob {job_id} is not running or has already completed.\x1b[0m\r\n".encode()
        )
        await ws.close()
        return

    worker_id = meta.get("worker_id", "")
    repo = meta.get("repo", "")
    repo_name = repo.split("/")[-1] if "/" in repo else repo or "workspace"
    workspace = f"/workspaces/{repo_name}"

    # Codespace jobs: shell in via `gh codespace ssh` as the learner (their stored
    # token), not docker-exec into a Sysbox container. Same xterm front-end + token flow.
    if meta.get("provider") == "codespace":
        from dashboard.github_oauth import get_user_token
        from dashboard.codespace_service import SSH_READY_MAX_HOLD, _append_creation_log
        user_token = await get_user_token(pool, meta.get("dtUser", ""))
        gh_env = {"GH_TOKEN": user_token} if user_token else {}

        # sshd is installed by the repo's post-create (setUpTerminal), minutes after
        # GitHub reports Available. If the learner opens the terminal before that,
        # `gh codespace ssh` fails with "failed to start SSH server" and the popup
        # dies. Wait for the ssh_ready flag (set by the creation-log fetch, which is
        # itself one SSH round-trip) instead — keep the learner informed meanwhile.
        if not await pool.hget(f"job:running:{job_id}", "ssh_ready"):
            await ws.send_bytes(
                b"\r\n\x1b[33m\xe2\x8f\xb3 The environment is still starting \xe2\x80\x94 "
                b"waiting for its SSH server (this can take a few minutes)...\x1b[0m\r\n"
            )
            deadline = asyncio.get_event_loop().time() + SSH_READY_MAX_HOLD
            while not await pool.hget(f"job:running:{job_id}", "ssh_ready"):
                if await pool.hget(f"job:running:{job_id}", "recovery"):
                    break
                if asyncio.get_event_loop().time() > deadline:
                    await ws.send_bytes(
                        b"\r\n\x1b[31mThe environment did not come up in time \xe2\x80\x94 its container "
                        b"likely failed to start (GitHub-side).\r\nPlease Terminate this session and "
                        b"launch the training again \xe2\x80\x94 a fresh environment normally works.\x1b[0m\r\n")
                    return
                # The fetch is one gh-ssh round-trip; on success it sets ssh_ready.
                asyncio.ensure_future(_append_creation_log(meta.get("dtUser", ""), job_id))
                await asyncio.sleep(10)
                await ws.send_bytes(b"\x1b[90m.\x1b[0m")
            else:
                await ws.send_bytes(b"\r\n\x1b[32mEnvironment ready \xe2\x80\x94 connecting...\x1b[0m\r\n")
            # Devcontainer died → GitHub swapped in an Alpine recovery container; a
            # shell there has none of the training tools. Tell the learner instead.
            if await pool.hget(f"job:running:{job_id}", "recovery"):
                await ws.send_bytes(
                    b"\r\n\x1b[31mThe environment's container failed to start \xe2\x80\x94 GitHub put the "
                    b"Codespace in recovery mode.\r\nPlease Terminate this session and launch the "
                    b"training again \xe2\x80\x94 a fresh environment normally works.\x1b[0m\r\n")
                return
        # `gh codespace ssh` lands in the Codespace BASE (user "vscode"), but the
        # hands-on happens inside the nested Dynatrace enablement container (image
        # shinojosa/dt-enablement) where kubectl, the k3d cluster and the lab tools
        # live. That container is started without a fixed --name, so select it by
        # image and docker-exec into its zsh. `-- -t <cmd>` forces ssh to allocate a
        # PTY so `docker exec -it` gets a real TTY (verified: /dev/pts + zsh present).
        # -w {workspace} is required: without it the shell starts at "/", so
        # source_framework.sh derives REPO_PATH="/" and the framework errors with
        # "//.devcontainer/util/.count: no such file or directory".
        inner_shell = (
            "CID=$(docker ps --format '{{.ID}} {{.Image}}' | "
            "awk '/dt-enablement/{print $1; exit}'); "
            f"WS='{workspace}'; "
            "if [ -n \"$CID\" ]; then "
            "docker exec \"$CID\" test -d \"$WS\" 2>/dev/null || WS=/workspaces; "
            "exec docker exec -it -e TERM=xterm-256color -w \"$WS\" \"$CID\" zsh; "
            "else echo 'dt-enablement container not found; opening base shell'; "
            "exec \"$SHELL\" -l; fi"
        )
        cmd = ["gh", "codespace", "ssh", "-c", job_id, "--", "-t", inner_shell]
        log.info("Codespace shell open: job=%s rows=%s cols=%s", job_id, rows, cols)
        await _pty_bridge(ws, cmd, rows=rows, cols=cols, env=gh_env)
        log.info("Codespace shell closed: job=%s", job_id)
        return

    # sb_name is stored in the running hash by the warm-pool agent (slot-based jobs).
    # Fall back to the legacy naming for non-slotted jobs.
    sb_name = meta.get("sb_name") or f"sb-{job_id[-32:]}"
    inner_exec = [
        "docker", "exec", "-it", sb_name,
        "docker", "exec", "-it",
        "-e", "TERM=xterm-256color",
        "-w", workspace,
        "dt", "zsh",
    ]

    if worker_id != "master":
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        ssh_host = worker_hash.get("ssh_host", "autonomous-enablements-worker")
        cmd = [
            "ssh", "-t",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            ssh_host,
        ] + inner_exec
    else:
        cmd = inner_exec

    log.info("Shell open: job=%s worker=%s sb=%s rows=%s cols=%s", job_id, worker_id or "local", sb_name, rows, cols)
    await _pty_bridge(ws, cmd, rows=rows, cols=cols)
    log.info("Shell closed: job=%s", job_id)


async def _pty_bridge(ws: WebSocket, cmd: list[str], rows: int = 24, cols: int = 220,
                      env: dict | None = None):
    """Create a PTY subprocess and bridge its I/O to the WebSocket.

    Uses loop.add_reader for non-blocking PTY output so the reader task is
    a proper asyncio coroutine that CAN be cancelled when the WebSocket
    disconnects — avoiding the deadlock that run_in_executor causes when
    os.read blocks in a thread that can't be interrupted.
    """
    master_fd, slave_fd = pty.openpty()
    # Set PTY size before starting the subprocess so applications (k9s, kubectl
    # completions, etc.) see the correct dimensions from the very first ioctl.
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env={**os.environ, "TERM": "xterm-256color", **(env or {})},
        )
        os.close(slave_fd)  # parent doesn't need the slave end
    except Exception as exc:
        try:
            os.close(slave_fd)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        await ws.send_bytes(f"\r\n\x1b[31mFailed to start shell: {exc}\x1b[0m\r\n".encode())
        return

    loop = asyncio.get_running_loop()
    pty_out: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_pty_readable():
        try:
            data = os.read(master_fd, 4096)
            pty_out.put_nowait(data)
        except OSError:
            # PTY EOF — subprocess exited or fd was closed
            pty_out.put_nowait(None)
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

    loop.add_reader(master_fd, _on_pty_readable)

    async def _pty_to_ws():
        while True:
            chunk = await pty_out.get()
            if chunk is None:
                break
            try:
                await ws.send_bytes(chunk)
            except Exception:
                break

    async def _ws_to_pty():
        while True:
            try:
                msg = await ws.receive()
            except (WebSocketDisconnect, Exception):
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                text = msg["text"]
                try:
                    ev = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    ev = None
                if isinstance(ev, dict) and ev.get("type") == "resize":
                    try:
                        rows = max(1, int(ev.get("rows", 24)))
                        cols = max(1, int(ev.get("cols", 80)))
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                    except (ValueError, OSError):
                        pass
                else:
                    try:
                        os.write(master_fd, text.encode())
                    except OSError:
                        break
            elif msg.get("bytes"):
                try:
                    os.write(master_fd, msg["bytes"])
                except OSError:
                    break

    t_out = asyncio.create_task(_pty_to_ws())
    t_in = asyncio.create_task(_ws_to_pty())
    try:
        await asyncio.wait({t_out, t_in}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        t_out.cancel()
        t_in.cancel()
        try:
            await asyncio.gather(t_out, t_in, return_exceptions=True)
        except Exception:
            pass
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass


async def _read_codespace_app_registry(job_id: str, meta: dict) -> list[dict]:
    """Read the framework app-registry from a Codespace and attach each app's
    public GitHub-forwarded-port URL.

    The registry is written by the framework's registerApp() into the nested
    dt-enablement container (selected by image, like the shell bridge). Each app's
    forwarded port is set public so the learner can open it, and `url` points at
    `https://{codespace}-{port}.app.github.dev`.
    """
    from dashboard.github_oauth import get_user_token
    token = await get_user_token(pool, meta.get("dtUser", ""))
    if not token:
        return []
    env = {**os.environ, "GH_TOKEN": token}
    registry_path = "/home/vscode/.cache/dt-framework/app-registry"
    inner = (
        "CID=$(docker ps --format '{{.ID}} {{.Image}}' | "
        "awk '/dt-enablement/{print $1; exit}'); "
        f"docker exec \"$CID\" cat {registry_path} 2>/dev/null"
    )
    cmd = ["gh", "codespace", "ssh", "-c", job_id, "--", "-t", inner]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
    except Exception:
        return []

    apps = []
    for line in stdout.decode(errors="replace").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        # parts[3] is the app's SERVICE port (e.g. todoapp 8080) — inside the
        # cluster only. In a Codespace every app is reached through the nginx
        # ingress catch-all on forwarded port 80 (see framework getAppURL), so
        # the public URL is always https://{codespace}-80.app.github.dev.
        port = parts[3].strip()
        apps.append({
            "name": parts[0], "namespace": parts[1], "service": parts[2],
            "port": port, "ingress_host": parts[4],
            "orbital_subdomain": "", "provider": "codespace",
            "url": f"https://{job_id}-80.app.github.dev",
        })
    # Make the ingress port public so the URL is reachable (best-effort).
    if apps:
        try:
            p = await asyncio.create_subprocess_exec(
                "gh", "codespace", "ports", "visibility", "80:public",
                "-c", job_id, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=env,
            )
            await asyncio.wait_for(p.communicate(), timeout=15)
        except Exception:
            pass
    return apps


async def _read_app_registry(job_id: str, meta: dict) -> list[dict]:
    """Read the .app-registry file from inside the running job's dt container.

    Uses the same SSH + docker exec chain as the shell bridge. Results are
    cached in Redis for 60 s to avoid exec overhead on every proxy request.
    """
    cache_key = f"job:apps:{job_id}"
    cached = await pool.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    # Codespace sessions run on GitHub, not a Sysbox container: read the same
    # app-registry via `gh codespace ssh` + docker-exec into the dt-enablement
    # container, and expose each app through its GitHub-forwarded-port URL.
    if meta.get("provider") == "codespace":
        apps = await _read_codespace_app_registry(job_id, meta)
        await pool.set(cache_key, json.dumps(apps), ex=60)
        return apps

    worker_id = meta.get("worker_id", "")
    sb_name = meta.get("sb_name") or f"sb-{job_id[-32:]}"
    # App registry is written by the framework's registerApp() helper to
    # ${HOME}/.cache/dt-framework/app-registry (HOME=/home/vscode inside dt).
    registry_path = "/home/vscode/.cache/dt-framework/app-registry"

    cmd = ["docker", "exec", sb_name, "docker", "exec", "dt", "cat", registry_path]
    if worker_id != "master":
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        ssh_host = worker_hash.get("ssh_host", "")
        if ssh_host:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                "-o", "BatchMode=yes",
                ssh_host,
            ] + cmd

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        return []

    apps = []
    for line in stdout.decode().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 5:
            apps.append({
                "name": parts[0],
                "namespace": parts[1],
                "service": parts[2],
                "port": parts[3],
                "ingress_host": parts[4],
                # field 7: orbital subdomain label e.g. "astroshop--enablement-674b15115240"
                "orbital_subdomain": parts[6].strip() if len(parts) >= 7 else "",
            })

    await pool.set(cache_key, json.dumps(apps), ex=60)
    return apps


def _b36(n: int) -> str:
    """Encode a non-negative int in base36 (0-9a-z)."""
    if n <= 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def _new_job_id() -> str:
    """Canonical job id: base36(epoch_ms)-<4hex>  (e.g. "mk3p9aqz-7f3a").

    Mirrors workers/manager.py and worker-agent/agent.py. Globally unique,
    chronologically sortable, and a valid DNS label — used as the Redis key,
    the log filename, and the app subdomain tail ({app}--{id}).
    """
    return f"{_b36(int(time.time() * 1000))}-{secrets.token_hex(2)}"


def _dt_hostgroup(user: str) -> str:
    """Per-user Grail-isolation id "<user>-<YYYYMMDD>" (DT_HOSTGROUP).

    Derived ONCE at provision time and carried in the job dict + redis meta so
    the worker's .env, the session-status API, and the app's DQL placeholder
    substitution all agree on the same value (no date drift across midnight).
    Email users keep only the local part; result is RFC-1123-safe.
    Mirrors workers/manager.py and worker-agent/executor.py.
    """
    user = (user or "").split("@", 1)[0].lower()
    user = re.sub(r"[^a-z0-9-]+", "-", user).strip("-")
    if not user:
        return ""
    return f"{user}-{datetime.now(timezone.utc):%Y%m%d}"


async def _find_job_by_subdomain(subdomain: str) -> tuple[str, dict, dict]:
    """Find a running job and app info from a wildcard subdomain label.

    subdomain format: {appname}--{job_id}
    The job_id is the canonical short id (base36 ts + hex) used verbatim as the
    Redis key, so a direct job:running:{job_id} lookup always resolves it —
    no prefix scanning or slug-shortening needed.
    Returns (job_id, job_meta, app_info) or ("", {}, {}) if not found.
    """
    if "--" not in subdomain:
        return "", {}, {}

    appname, job_id = subdomain.split("--", 1)

    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        return "", {}, {}
    apps = await _read_app_registry(job_id, meta)
    app_info = next((a for a in apps if a["name"] == appname), None)
    if app_info:
        return job_id, meta, app_info
    return "", {}, {}


@app.get("/api/jobs/{job_id}/apps")
async def api_job_apps(job_id: str):
    """List apps registered in the job's .app-registry with their proxy URLs."""
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="job not running")

    apps = await _read_app_registry(job_id, meta)
    result = []
    for a in apps:
        entry = {**a, "proxy_url": f"/apps/{job_id}/{a['name']}/"}
        if a.get("orbital_subdomain"):
            entry["subdomain_url"] = (
                f"https://{a['orbital_subdomain']}"
                ".autonomous-enablements.whydevslovedynatrace.com/"
            )
        result.append(entry)
    return {"apps": result}


def _rewrite_proxy_body(content: bytes, base_path: str, content_type: str) -> bytes:
    """Rewrite root-relative URLs in HTML/CSS proxy responses.

    For HTML: rewrites src/href/action attributes and injects a JS shim that
    patches fetch() and XMLHttpRequest so dynamic API calls (e.g. $.ajax('/todos'))
    are transparently prefixed with the proxy base path at runtime.

    For CSS: rewrites url(/...) patterns so background images load correctly.
    """
    import re as _re
    ct = content_type.lower()
    is_html = "html" in ct or "xhtml" in ct
    is_css = "css" in ct
    if not (is_html or is_css):
        return content

    charset = "utf-8"
    if "charset=" in ct:
        charset = ct.split("charset=")[-1].strip().split(";")[0].strip()
    try:
        text = content.decode(charset, errors="replace")
    except Exception:
        return content

    # Rewrite root-relative url(...) in CSS and inline HTML styles.
    # Excludes protocol-relative //... and data: URIs.
    def _rewrite_css_url(m: "_re.Match") -> str:
        val = m.group(1).strip("'\"")
        if val.startswith("/") and not val.startswith("//"):
            if not val.startswith(base_path):
                return f"url({base_path}{val})"
        elif _re.match(r'^https?://localhost(:\d+)?/', val):
            path_part = _re.sub(r'^https?://localhost(:\d+)?', '', val)
            path_part = _re.sub(r'^//+', '/', path_part)
            if not path_part.startswith(base_path):
                return f"url({base_path}{path_part})"
        return m.group(0)

    text = _re.sub(r"url\(([^)]*)\)", _rewrite_css_url, text)

    if is_html:
        # Rewrite src="/" href="/" action="/" data-src="/" attributes.
        # Also handles absolute http://localhost:PORT/... URLs that Next.js / some
        # apps emit (e.g. <img src="http://localhost:8080/icons/foo.svg">).
        def _rewrite_attr_value(val: str) -> str:
            # Root-relative /path
            if val.startswith("/") and not val.startswith("//"):
                if not val.startswith(base_path):
                    return base_path + val
            # Absolute localhost URL — strip the origin
            elif _re.match(r'^https?://localhost(:\d+)?/', val):
                path_part = _re.sub(r'^https?://localhost(:\d+)?', '', val)
                # Normalise accidental double-slash after stripping origin
                path_part = _re.sub(r'^//+', '/', path_part)
                if not path_part.startswith(base_path):
                    return base_path + path_part
            return val

        # Rewrite resource-loading attributes on all tags.
        # Deliberately excludes href — <a href> must NOT be rewritten because
        # Next.js/React reads those values to determine client-side routes; if we
        # prefix them the router navigates to the wrong path and renders a blank
        # page.  href on <link> tags (stylesheets, icons, preloads) is handled
        # separately below.
        for attr in ("src", "action", "data-src"):
            text = _re.sub(
                rf'{attr}="([^"]*)"',
                lambda m, a=attr: f'{a}="{_rewrite_attr_value(m.group(1))}"',
                text,
            )
            text = _re.sub(
                rf"{attr}='([^']*)'",
                lambda m, a=attr: f"{a}='{_rewrite_attr_value(m.group(1))}'",
                text,
            )

        # Rewrite href only on <link> tags (CSS, icons, preloads, canonical).
        # Single-line tag assumption holds for all known SSR frameworks.
        text = _re.sub(
            r'(<link\b[^>]*?\bhref=")([^"]*?)(")',
            lambda m: m.group(1) + _rewrite_attr_value(m.group(2)) + m.group(3),
            text,
            flags=_re.IGNORECASE,
        )
        text = _re.sub(
            r"(<link\b[^>]*?\bhref=')([^']*?)(')",
            lambda m: m.group(1) + _rewrite_attr_value(m.group(2)) + m.group(3),
            text,
            flags=_re.IGNORECASE,
        )

        # srcset has comma-separated "URL [descriptor]" pairs — rewrite each URL.
        def _rewrite_srcset(m: "_re.Match") -> str:
            quote = m.group(1)
            parts = []
            for entry in m.group(2).split(","):
                entry = entry.strip()
                if not entry:
                    continue
                tokens = entry.split(None, 1)
                rewritten = _rewrite_attr_value(tokens[0])
                parts.append(rewritten + (" " + tokens[1] if len(tokens) > 1 else ""))
            return f'srcset={quote}{", ".join(parts)}{quote}'

        text = _re.sub(r'srcset=(["\'])([^"\']*)\1', _rewrite_srcset, text, flags=_re.IGNORECASE)

        # Also rewrite Location: root-relative in meta refresh tags.
        text = _re.sub(
            r'(content="\d+;\s*url=)(/(?!/)[^"]*)',
            lambda m: f"{m.group(1)}{base_path}{m.group(2)}",
            text,
        )

        # Inject a JS shim that:
        # - Rewrites root-relative and absolute localhost URLs in fetch() / XHR
        #   so dynamic API calls go through the proxy base path.
        # - Patches history.pushState / history.replaceState so Next.js-style
        #   client-side navigation stays inside the proxy path (prevents iframe
        #   URL from escaping to the ops dashboard root).
        shim = (
            f"<script>"
            f"(function(){{"
            f"var B='{base_path}';"
            f"function r(u){{"
            f"if(typeof u!=='string')return u;"
            f"if(u.charAt(0)==='/'&&u.charAt(1)!=='/'&&u.indexOf(B)!==0)return B+u;"
            f"if(/^https?:\\/\\/localhost(:\\d+)?\\//.test(u)){{"
            f"try{{var p=new URL(u);var q=p.pathname.replace(/^\\/\\//,'/');if(q.indexOf(B)!==0)return B+q+(p.search||'')+(p.hash||'');}}catch(e){{}}"
            f"}}"
            f"return u;"
            f"}}"
            f"var _f=window.fetch;"
            f"window.fetch=function(i,o){{return _f.call(this,typeof i==='string'?r(i):i,o);}};"
            f"var _x=XMLHttpRequest.prototype.open;"
            f"XMLHttpRequest.prototype.open=function(m,u){{arguments[1]=r(String(u));return _x.apply(this,arguments);}};"
            f"var _ps=history.pushState.bind(history);"
            f"history.pushState=function(s,t,u){{return _ps(s,t,u!=null?r(String(u)):u);}};"
            f"var _rs=history.replaceState.bind(history);"
            f"history.replaceState=function(s,t,u){{return _rs(s,t,u!=null?r(String(u)):u);}};"
            f"}})();"
            f"</script>"
        )
        if "</head>" in text:
            text = text.replace("</head>", shim + "</head>", 1)
        elif "<head>" in text:
            text = text.replace("<head>", "<head>" + shim, 1)
        else:
            text = shim + text

    try:
        return text.encode(charset, errors="replace")
    except Exception:
        return content


@app.api_route(
    "/apps/{job_id}/{app_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@app.api_route(
    "/apps/{job_id}/{app_name}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_job_app(job_id: str, app_name: str, request: Request, path: str = ""):
    """Reverse-proxy to an app running inside a job's k3d cluster.

    Connects directly to the Sysbox's published port (allocated at job start)
    on the worker host, setting the Host header so nginx ingress can route
    to the right service.  No SSH tunnel required — the master's private IP
    is allowed inbound on the port range via the worker security group.
    """
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="job not running")

    # Codespace apps aren't proxied through Orbital (the port lives in the learner's
    # Codespace on GitHub) — redirect to the public GitHub-forwarded-port URL.
    if meta.get("provider") == "codespace":
        from fastapi.responses import RedirectResponse
        cs_apps = await _read_app_registry(job_id, meta)
        target = next((a.get("url") for a in cs_apps if a["name"] == app_name and a.get("url")), None)
        if target:
            return RedirectResponse(target)
        raise HTTPException(status_code=404, detail=f"app '{app_name}' not exposed in this Codespace yet")

    app_proxy_port = meta.get("app_proxy_port")
    if not app_proxy_port:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:32px;background:#050810;color:#d0d7de'>"
            "<h3 style='color:#f0b429'>App proxy not available</h3>"
            "<p>This job was started before app port forwarding was added, or the worker agent "
            "needs to be updated.</p>"
            "<p>Terminate this job and start a new Training session to enable app preview.</p>"
            "</body></html>",
            status_code=503,
        )

    apps = await _read_app_registry(job_id, meta)
    app_info = next((a for a in apps if a["name"] == app_name), None)
    if not app_info:
        raise HTTPException(status_code=404, detail=f"app '{app_name}' not found in registry")

    worker_id = meta.get("worker_id", "")
    if worker_id != "master":
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        target_ip = worker_hash.get("host", "127.0.0.1")
    else:
        target_ip = "127.0.0.1"

    target_url = f"http://{target_ip}:{app_proxy_port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Forward a minimal set of headers; always override Host for ingress routing.
    forward_headers = {
        "Host": app_info["ingress_host"],
    }
    for h in ("accept", "accept-language", "cookie",
               "content-type", "cache-control", "x-requested-with"):
        if h in request.headers:
            forward_headers[h] = request.headers[h]
    # Don't forward accept-encoding: we decode the response body for URL
    # rewriting, so the upstream should send uncompressed content.

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
        raise HTTPException(status_code=502, detail="could not connect to app — is the cluster ready?")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="upstream app timed out")

    # Strip hop-by-hop headers and security headers that must not be forwarded.
    # x-frame-options is stripped here so nginx's SAMEORIGIN (set in the
    # /apps/ location block) is the only value the browser sees.
    skip = {"transfer-encoding", "connection", "keep-alive", "upgrade",
            "proxy-authenticate", "proxy-authorization", "te", "trailers",
            "x-frame-options", "content-security-policy",
            "content-security-policy-report-only"}
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in skip
    }

    # Rewrite Location redirects so the browser stays within the proxy.
    location = resp_headers.get("location", "")
    if location:
        if location.startswith("http://") or location.startswith("https://"):
            # Absolute redirect from upstream — keep it inside the proxy.
            resp_headers["location"] = f"/apps/{job_id}/{app_name}/"
        elif location.startswith("/") and not location.startswith("//"):
            # Root-relative redirect — prefix with proxy base.
            resp_headers["location"] = f"/apps/{job_id}/{app_name}{location}"

    # Rewrite root-relative URLs in HTML/CSS so assets and API calls resolve
    # through the proxy instead of hitting the ops dashboard root.
    content_type = upstream.headers.get("content-type", "")
    body = _rewrite_proxy_body(upstream.content, f"/apps/{job_id}/{app_name}", content_type)

    # Remove content-encoding now that we've decoded/re-encoded the body.
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)

    from fastapi.responses import Response as PlainResponse
    return PlainResponse(
        content=body,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


# ---------------------------------------------------------------------------
# Wildcard-subdomain app proxy
# ---------------------------------------------------------------------------
# Handles requests routed via nginx from wildcard subdomains:
#   {appname}--{job_slug}.autonomous-enablements.whydevslovedynatrace.com
#
# nginx rewrites the path to /proxy-subdomain/<original-path> and sets
# the X-App-Subdomain header to the subdomain label before forwarding here.
# Unlike /apps/{job_id}/{app_name}/ there is NO HTML rewriting — the app
# runs at root so all asset paths resolve correctly without patching.
# ---------------------------------------------------------------------------

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
    for h in ("accept", "accept-language", "cookie",
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
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in skip
    }

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

    return PlainResponse(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


# ---------------------------------------------------------------------------
# Arena API — training catalog, provisioning, session management
# ---------------------------------------------------------------------------

# Repos in dynatrace-wwse that are available as Arena trainings.
# Each entry maps repo name → Arena metadata overrides.
# site_name and tags are scraped from mkdocs.yaml; these values fill gaps.
_ARENA_REPOS = {
    "enablement-dynatrace-log-ingest-101": {
        "id": "log-ingest-101",
        "type": "lab",
        "difficulty": "beginner",
        "estimatedTime": 45,
        "tags": ["logs", "log-ingest", "opentelemetry"],
    },
    "enablement-dtwiz-101": {
        "id": "dtwiz-101",
        "type": "lab",
        "difficulty": "beginner",
        "estimatedTime": 60,
        "tags": ["kubernetes", "operator", "problems", "dtwiz"],
    },
    "enablement-live-debugger-bug-hunting": {
        "id": "live-debugger",
        "type": "lab",
        "difficulty": "beginner",
        "estimatedTime": 30,
        "tags": ["live-debugger", "debugging", "code"],
    },
    "enablement-gen-ai-llm-observability": {
        "id": "gen-ai-llm",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 60,
        "tags": ["ai", "llm", "opentelemetry", "genai"],
    },
    "enablement-dql-fundamentals": {
        "id": "dql-fundamentals",
        "type": "lab-assessment",
        "difficulty": "beginner",
        "estimatedTime": 45,
        "tags": ["dql", "logs", "metrics", "traces"],
    },
    "enablement-business-observability": {
        "id": "business-observability",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 60,
        "tags": ["bizevents", "business-observability", "dql"],
    },
    "enablement-kubernetes-101": {
        "id": "kubernetes-101",
        "type": "lab-assessment",
        "difficulty": "beginner",
        "estimatedTime": 60,
        "tags": ["kubernetes", "observability"],
    },
    "enablement-kubernetes-opentelemetry": {
        "id": "kubernetes-opentelemetry",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 180,
        "tags": ["kubernetes", "opentelemetry", "traces"],
    },
    "enablement-dynatrace-ai-mcp": {
        "id": "ai-mcp",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 120,
        "tags": ["gen-ai", "opentelemetry", "mcp"],
    },
    "enablement-dql-301": {
        "id": "dql-301",
        "type": "lab",
        "difficulty": "advanced",
        "estimatedTime": 60,
        "tags": ["dql", "logs", "business-observability"],
    },
    "enablement-browser-dem-biz-observability": {
        "id": "browser-dem-biz",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 120,
        "tags": ["rum", "business-observability", "dql"],
    },
    "enablement-workflow-essentials": {
        "id": "workflow-essentials",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 150,
        "tags": ["workflows", "automation"],
    },
    "enablement-azure-webapp-otel": {
        "id": "azure-webapp-otel",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 120,
        "tags": ["opentelemetry", "azure"],
    },
    "enablement-kubernetes-opentelemetry-openpipeline": {
        "id": "kubernetes-otel-openpipeline",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 180,
        "tags": ["kubernetes", "opentelemetry", "logs"],
    },
    "workshop-dynatrace-log-analytics": {
        "id": "log-analytics",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 180,
        "tags": ["logs", "kubernetes", "dql"],
    },
    "workshop-destination-automation": {
        "id": "destination-automation",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 120,
        "tags": ["workflows", "automation"],
    },
    "demo-agentic-ai-with-nvidia": {
        "id": "agentic-ai-nvidia",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 90,
        "tags": ["gen-ai", "opentelemetry"],
    },
    "demo-mcp-unguard": {
        "id": "mcp-unguard",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 60,
        "tags": ["application-security", "gen-ai"],
    },
    "demo-opentelemetry": {
        "id": "opentelemetry-demo",
        "type": "lab",
        "difficulty": "beginner",
        "estimatedTime": 60,
        "tags": ["opentelemetry", "traces"],
    },
    "demo-astroshop-runtime-optimization": {
        "id": "astroshop-runtime",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 90,
        "tags": ["kubernetes", "devops"],
    },
    "demo-astroshop-problems": {
        "id": "astroshop-problems",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 90,
        "tags": ["kubernetes", "live-debugger"],
    },
    "bug-busters": {
        "id": "bug-busters",
        "type": "lab",
        "difficulty": "intermediate",
        "estimatedTime": 90,
        "tags": ["live-debugger", "devops"],
    },
}

_ARENA_CATALOG_CACHE_KEY = "arena:catalog"
_ARENA_BUILD_LOCK = asyncio.Lock()
# Titles/descriptions change only when a repo's mkdocs site_name changes — cache long.
# The app calls this on EVERY boot of EVERY tenant; a cold rebuild used to be ~21
# serial GitHub calls (~5.4s) on the boot critical path every 5 minutes.
_ARENA_CATALOG_TTL = 3600  # 1 hour


async def _fetch_arena_catalog() -> list[dict]:
    """Scrape site_name from each repo's mkdocs.yaml via GitHub API.

    Falls back to repo name if mkdocs.yaml is unavailable.
    Results cached in Redis for 5 minutes to avoid repeated API calls.
    """
    import base64 as _b64
    import re as _re

    async def _get_mkdocs_title(repo: str) -> tuple[str, str]:
        """Return (site_name, description) from mkdocs.yaml, or sensible defaults."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                url = f"{GH_API}/repos/dynatrace-wwse/{repo}/contents/mkdocs.yaml"
                headers = {"Authorization": f"Bearer {GH_TOKEN}"} if GH_TOKEN else {}
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return repo, ""
                content = _b64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
                name_match = _re.search(r'^site_name:\s*["\']?(.+?)["\']?\s*$', content, _re.MULTILINE)
                desc_match = _re.search(r'^site_description:\s*["\']?(.+?)["\']?\s*$', content, _re.MULTILINE)
                title = name_match.group(1).strip() if name_match else repo
                # Strip "Dynatrace Enablement Lab: " prefix for brevity
                title = _re.sub(r'^Dynatrace (?:Enablement|Observability) Lab:\s*', '', title)
                desc = desc_match.group(1).strip() if desc_match else ""
                return title, desc
        except Exception:
            return repo, ""

    # All repos concurrently — serial fetching put ~21 × ~250ms GitHub round-trips
    # on the app-boot critical path whenever the cache was cold.
    titles = await asyncio.gather(*[_get_mkdocs_title(repo) for repo in _ARENA_REPOS])
    trainings = []
    for (repo, meta), (title, desc) in zip(_ARENA_REPOS.items(), titles):
        trainings.append({
            "id": meta["id"],
            "title": title,
            "description": desc or f"Hands-on {title} lab in a live Kubernetes environment.",
            "type": meta["type"],
            "difficulty": meta["difficulty"],
            "estimatedTime": meta["estimatedTime"],
            "tags": meta["tags"],
            "repoUrl": f"https://github.com/dynatrace-wwse/{repo}",
            "branch": "main",
            "source": "orbital",
        })
    return trainings


def _filter_trainings_by_profile(trainings: list[dict], tenant: str) -> list[dict]:
    """Keep only the hands-on trainings whose repo is in the tenant's content profile.
    The Arena catalog is the full set of hands-on repos; without this, every tenant
    sees all of them regardless of its profile. Unknown/invalid tenant → no filter."""
    try:
        tenant_id, domain = classify_tenant(tenant)
    except Exception:
        return trainings
    profile = _load_profile(resolve_profile(tenant_id, domain))
    allowed = {(s.get("repo") or "").lower() for s in profile.get("sources", [])}
    if not allowed:
        return trainings

    def repo_of(t: dict) -> str:
        return "/".join((t.get("repoUrl") or "").rstrip("/").split("/")[-2:]).lower()

    return [t for t in trainings if repo_of(t) in allowed]


@app.get("/api/arena/trainings")
async def api_arena_trainings(tenant: str = ""):
    """Return available Arena trainings scraped from real repos.

    Titles come from mkdocs.yaml site_name. Cached in Redis for 5 minutes.
    When `tenant` is given, the list is filtered to that tenant's content profile
    (so e.g. a 'core' tenant only sees the hands-on trainings in 'core')."""
    cached = await pool.get(_ARENA_CATALOG_CACHE_KEY)
    if not cached:
        # Single-flight: without this, N concurrent cache-misses (e.g. an install
        # storm right after the TTL lapses) each ran the full GitHub scrape in
        # parallel — observed 5× concurrent 3s rebuilds in load testing.
        async with _ARENA_BUILD_LOCK:
            cached = await pool.get(_ARENA_CATALOG_CACHE_KEY)
            if not cached:
                trainings = await _fetch_arena_catalog()
                await pool.set(_ARENA_CATALOG_CACHE_KEY, json.dumps(trainings), ex=_ARENA_CATALOG_TTL)
                cached = json.dumps(trainings)
    trainings = json.loads(cached)
    if tenant:
        trainings = _filter_trainings_by_profile(trainings, tenant)
    return trainings


class ArenaProvisionRequest(BaseModel):
    trainingId: str
    userId: str
    tenantId: str = ""
    # DT tenant for this session — if omitted, falls back to worker static creds.
    tenantUrl: str = ""
    # Auth for token provisioning: OAuth2 (preferred, app-installed flow) OR existing API token.
    oauthClientId: str = ""
    oauthClientSecret: str = ""
    apiToken: str = ""          # existing token with apiTokens.write scope
    # Preferred (multi-tenancy): the app self-mints per-tenant tokens and passes the
    # VALUES here, so Orbital never holds a tenant minting credential. The app owns
    # the lifecycle and revokes via its own identity on terminate.
    dtEnv: dict[str, str] = {}   # env_var -> token value (DT_OPERATOR_TOKEN, ...)
    dtTokenIds: list[str] = []   # ids the app will revoke (Orbital does not)
    # Branch to check the training repo out from (the app passes the branch its
    # lab content was imported from). Empty → the catalog's branch (main).
    ref: str = ""


def _gen3_platform_provisioner(tenant_url: str):
    """Build a PlatformTokenProvisioner for a tenant whose classic apiToken creation is
    disabled (gen3 — sprint/dev, and prod as it migrates). Reads the per-account account
    OAuth client from env: MINT_{CLIENT_ID,CLIENT_SECRET,RESOURCE,SSO,API_HOST}_<DOMAIN>.
    Returns None when the tenant isn't gen3 or no creds are configured for its account."""
    try:
        tenant_id, domain = classify_tenant(tenant_url)
    except Exception:
        return None
    sfx = domain.upper()  # SPRINT / DEV / PROD
    cid = os.environ.get(f"MINT_CLIENT_ID_{sfx}")
    csec = os.environ.get(f"MINT_CLIENT_SECRET_{sfx}")
    res = os.environ.get(f"MINT_RESOURCE_{sfx}", "")
    sso = os.environ.get(f"MINT_SSO_{sfx}")
    api = os.environ.get(f"MINT_API_HOST_{sfx}")
    if not (cid and csec and res and sso and api):
        return None
    from provisioning import PlatformTokenProvisioner
    return PlatformTokenProvisioner(
        tenant_url=tenant_url, env_id=tenant_id, account_uuid=res.split(":")[-1],
        sso_token_url=sso, account_api_host=api, oauth_client_id=cid, oauth_client_secret=csec)


# NOTE: Orbital deliberately holds NO per-tenant OAuth client. Self-managed tenants mint
# their own platform tokens INSIDE the app (the OAuth client lives in the app's app-state on
# that tenant) and pass only token VALUES here via ArenaProvisionRequest.dtEnv. The env-based
# _gen3_platform_provisioner remains only for tenants we own (COE/SRO/sprint, MINT_*_<DOMAIN>).


@app.post("/api/arena/provision")
async def api_arena_provision(body: ArenaProvisionRequest, request: Request):
    """Provision a training environment — queues a real daemon job on the amd64 worker.

    If tenantUrl + auth credentials are provided, auto-creates scoped DT API tokens
    for this session (named enbl-{repo}-{user}-{suffix}, expiry = session TTL).
    Token IDs are stored in Redis so they can be revoked on session termination.

    Returns: { jobId, wsUrl, expiresAt, status: "provisioning", tokenProvisioned: bool }
    """
    await _require_arena_auth(request)
    import uuid as _uuid
    from provisioning import DTTokenProvisioner, load_token_specs

    cached = await pool.get(_ARENA_CATALOG_CACHE_KEY)
    catalog = json.loads(cached) if cached else await _fetch_arena_catalog()
    training = next((t for t in catalog if t["id"] == body.trainingId), None)
    if training is None:
        raise HTTPException(status_code=404, detail=f"Training '{body.trainingId}' not found")

    repo_nwo = "/".join(training["repoUrl"].rstrip("/").split("/")[-2:])

    # Idempotency guard: one live session per (user, tenant, training). The app's
    # UI checks this client-side before launching, but a direct call, a double
    # click, or a network retry can still hit provision twice — which used to
    # double-provision (two daemon jobs, two clusters, wasted slots). Return the
    # EXISTING session instead of queuing a duplicate.
    guard_tenant = (body.tenantUrl or body.tenantId or "").rstrip("/")
    if body.userId:
        cursor = 0
        while True:
            cursor, keys = await pool.scan(cursor, match="job:running:enablement-*", count=200)
            for key in keys:
                m = await pool.hgetall(key)
                if not m or m.get("terminating"):
                    continue
                # Skip ghosts: a job whose owning worker deregistered (spot
                # scale-down / crash) is a dead session — don't route a student
                # to it. The terminate reconciler reaps these, but guard here too.
                wid = m.get("worker_id", "")
                if wid and wid != "master" and wid not in ("queued", "") \
                        and not await pool.exists(f"worker:{wid}"):
                    continue
                if (m.get("arena_user") == body.userId
                        and m.get("training_id") == body.trainingId
                        and ((not guard_tenant)
                             or guard_tenant in (m.get("dt_tenant_url", ""), m.get("arena_tenant", "")))):
                    ex_id = m.get("job_id", key.split(":")[-1])
                    log.info("Provision deduped: %s already has session %s for %s",
                             body.userId, ex_id, body.trainingId)
                    livelog = await pool.get(f"job:livelog:{ex_id}")
                    status = ("ready" if livelog and "Daemon ready" in livelog
                              else "queued" if m.get("worker_id") in ("queued", "") else "provisioning")
                    return {
                        "jobId": ex_id,
                        "wsUrl": f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{ex_id}/shell",
                        "expiresAt": m.get("expires_at", ""),
                        "status": status,
                        "tokenProvisioned": m.get("token_provisioned") == "1",
                        "dtSessionId": m.get("dt_hostgroup", ""),
                        "deduped": True,
                    }
            if cursor == 0:
                break

    job_id = f"enablement-{_uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    # Cap Orbital-hosted training sessions at 2h so no daemon runs unbounded; the
    # expiry reaper force-kills it at expires_at. Overridable via env.
    session_hours = int(os.environ.get("ORBITAL_SESSION_HOURS", "2"))
    expires_at = (now.replace(microsecond=0) + timedelta(hours=session_hours)).isoformat()

    # Environment follows the content: a lab imported from a branch runs its
    # session from that branch (test-before-merge). Reject refs that could
    # smuggle flags/paths into the git checkout.
    session_ref = (body.ref or "").strip()
    if session_ref and (session_ref.startswith("-") or ".." in session_ref):
        raise HTTPException(status_code=400, detail=f"Invalid ref '{session_ref}'")
    session_ref = session_ref or training["branch"]

    # --- Token provisioning ---
    dt_env: dict[str, str] = {}
    provisioned_token_ids: list[str] = []
    token_provisioned = False
    mint_kind = ""  # "platform" when Orbital minted via the Account Management API (gen3)
    mint_error = ""  # surfaced in the response so callers (training-test) can log WHY

    tenant_url = body.tenantUrl.rstrip("/") if body.tenantUrl else ""
    if body.dtEnv:
        # Preferred: the app already minted per-tenant tokens with its own identity.
        # Use the values directly; the app owns revocation (Orbital holds no tenant cred).
        dt_env = dict(body.dtEnv)
        provisioned_token_ids = list(body.dtTokenIds)
        token_provisioned = True
        if not tenant_url:
            tenant_url = (dt_env.get("DT_ENVIRONMENT") or "").rstrip("/")
    elif tenant_url and (_gen3 := _gen3_platform_provisioner(tenant_url)) is not None:
        # gen3 tenant (classic apiToken creation disabled, e.g. sprint): Orbital mints
        # platform tokens via the account OAuth client. Orbital owns revocation (mint_kind).
        try:
            specs = await load_token_specs(repo_nwo, ref=session_ref)
            result = await _gen3.create_tokens(repo=repo_nwo, user_id=body.userId,
                                               specs=specs, expires_in_hours=session_hours)
            dt_env = result.env
            provisioned_token_ids = result.token_ids
            token_provisioned = True
            mint_kind = "platform"
        except Exception as exc:
            import logging as _log
            mint_error = str(exc)[:300]
            _log.getLogger("ops-dashboard").warning(
                "Platform-token provisioning failed for %s / %s: %s — falling back to worker creds",
                repo_nwo, body.userId, exc)
    elif tenant_url and (body.apiToken or (body.oauthClientId and body.oauthClientSecret)):
        try:
            provisioner = DTTokenProvisioner(
                tenant_url=tenant_url,
                api_token=body.apiToken,
                oauth_client_id=body.oauthClientId,
                oauth_client_secret=body.oauthClientSecret,
            )
            specs = await load_token_specs(repo_nwo, ref=session_ref)
            result = await provisioner.create_tokens(
                repo=repo_nwo,
                user_id=body.userId,
                specs=specs,
                expires_in_hours=session_hours,
            )
            dt_env = result.env
            provisioned_token_ids = result.token_ids
            token_provisioned = True
            mint_kind = "classic"
        except Exception as exc:
            # Non-fatal: log and fall back to worker static creds
            import logging as _log
            mint_error = str(exc)[:300]
            _log.getLogger("ops-dashboard").warning(
                "Token provisioning failed for %s / %s: %s — falling back to worker creds",
                repo_nwo, body.userId, exc,
            )

    # Per-user Grail-isolation id — derived once here so the worker's .env
    # (DT_HOSTGROUP → generateDynakube) and the app's {{DT_SESSION_ID}} DQL
    # placeholder always agree.
    dt_hostgroup = _dt_hostgroup(body.userId)

    job = {
        "job_id":        job_id,
        "type":          "daemon",
        "repo":          repo_nwo,
        "arch":          "amd64",
        "ref":           session_ref,
        "timestamp":     now.isoformat(),
        "trigger":       "enablement-app",
        "nightly_run_id": f"enablement-{body.trainingId}",
        "requested_by":  body.userId,
        "dt_hostgroup":  dt_hostgroup,
    }
    if dt_env:
        job["dt_env"] = dt_env
    # Tenant the worker should bind to — drives the multi-tenancy guard in
    # _write_env_file (CoE → static creds; non-CoE → minted only, never CoE).
    if tenant_url:
        job["tenant"] = tenant_url

    redis_meta = {
        "job_id":       job_id,
        "repo":         repo_nwo,
        "branch":       session_ref,
        "arch":         "amd64",
        "started_at":   now.isoformat(),
        "worker_id":    "queued",
        "type":         "daemon",
        "arena_user":   body.userId,
        "arena_tenant": body.tenantId or tenant_url,
        # Stage badge shown next to the tenant id in History (production / sprint / dev),
        # derived from the tenant domain (*.apps.dynatrace.com=production,
        # *.sprint.apps.dynatracelabs.com=sprint, *.dev…=dev).
        "stage":        {"prod": "production", "sprint": "sprint", "dev": "dev"}.get(
            classify_tenant(tenant_url or body.tenantId or "")[1], "production"),
        "training_id":  body.trainingId,
        "expires_at":   expires_at,
        "token_provisioned": "1" if token_provisioned else "0",
        "dt_hostgroup": dt_hostgroup,
    }
    if provisioned_token_ids:
        redis_meta["dt_token_ids"] = json.dumps(provisioned_token_ids)
    if tenant_url:
        redis_meta["dt_tenant_url"] = tenant_url
    if mint_kind:
        # Orbital minted platform tokens (gen3) → terminate revokes via the same account
        # OAuth client (no per-job creds stored; rebuilt from env on revoke).
        redis_meta["mint_kind"] = mint_kind
    # Store auth so terminate can revoke tokens. Token has apiTokens.read+write only.
    if token_provisioned:
        if body.apiToken:
            redis_meta["dt_auth_token"] = body.apiToken
        elif body.oauthClientId:
            redis_meta["dt_oauth_client_id"] = body.oauthClientId
            redis_meta["dt_oauth_client_secret"] = body.oauthClientSecret

    await pool.hset(f"job:running:{job_id}", mapping=redis_meta)
    await pool.expire(f"job:running:{job_id}", int(timedelta(hours=session_hours).total_seconds()))
    await pool.rpush("queue:test:amd64", json.dumps(job))

    return {
        "jobId":            job_id,
        "wsUrl":            f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{job_id}/shell",
        "expiresAt":        expires_at,
        "status":           "provisioning",
        "tokenProvisioned": token_provisioned,
        "dtSessionId":      dt_hostgroup,
        # Mint story for callers that show the full flow (training-test log):
        # token IDs + env-var names only — NEVER token values.
        "mintKind": mint_kind or ("app" if body.dtEnv else "none"),
        "mintDetail": [
            {"envVar": k, "tokenId": tid}
            for k, tid in zip([k for k in dt_env if k != "DT_ENVIRONMENT"], provisioned_token_ids)
        ],
        **({"mintError": mint_error} if mint_error else {}),
    }


@app.get("/api/arena/sessions/{job_id}")
async def api_arena_session_status(job_id: str, request: Request):
    """Return current session status.

    Status transitions:
      queued      → worker hasn't picked up the job yet
      provisioning → worker is running postCreate/postStart (cluster setup, ~5-15 min)
      ready       → "Daemon ready" appeared in livelog — shell is available
      failed      → creation failed; the retained log explains why (logAvailable)
      terminated  → explicitly terminated
      expired     → job:running key missing and no terminal record (TTL elapsed)

    Gated by _require_arena_auth: every known caller (the app's orbitalFetch,
    training_test_runner, bootcamp_loadtest, agentic_validator) can carry the
    service bearer, and the learner-facing /shell + /terminal pages poll
    /api/jobs/{id}/livelog — NOT this endpoint — so nothing anonymous needs it.
    The compat window inventories any caller this audit missed.
    """
    await _require_arena_auth(request)
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        # job:running is gone — distinguish a FAILED creation (so the student can
        # read the retained log) from a plain TTL-expiry. job:final outlives the
        # running key (written by the worker's finally block, 7-day TTL).
        final = await pool.hgetall(f"job:final:{job_id}")
        fstatus = final.get("status")
        if fstatus in ("failed", "terminated", "completed"):
            return {
                "jobId":        job_id,
                "status":       fstatus,
                "error":        final.get("error", ""),
                "finishedAt":   final.get("finished_at", ""),
                "logAvailable": bool(await pool.exists(f"job:log:{job_id}")),
            }
        return {"jobId": job_id, "status": "expired"}

    # Termination requested — report gone immediately even while the worker is still
    # tearing the container down (slow). Stops the user from clicking Terminate twice.
    if meta.get("terminating"):
        return {"jobId": job_id, "status": "terminated"}

    # Check livelog for readiness signal written by execute_daemon
    livelog = await pool.get(f"job:livelog:{job_id}")
    if livelog and "Daemon ready" in livelog:
        status = "ready"
    elif meta.get("worker_id") in ("queued", ""):
        status = "queued"
    else:
        status = "provisioning"

    # Anonymous callers get the learner identity masked (the app's bearer /
    # signed-in members get full values — the app needs them for the UI + DQL
    # session-id substitution).
    full = _has_full_access(request)
    return {
        "jobId":      job_id,
        "status":     status,
        "wsUrl":      f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{job_id}/shell",
        "trainingId": meta.get("training_id", ""),
        "userId":     meta.get("arena_user", "") if full
                      else masking.mask_email(meta.get("arena_user", "")),
        "expiresAt":  meta.get("expires_at", ""),
        "dtSessionId": meta.get("dt_hostgroup", "") if full
                       else masking.mask_email(meta.get("dt_hostgroup", "")),
    }


@app.get("/api/arena/user-session")
async def api_arena_user_session(userId: str, trainingId: str, request: Request, tenant: str = ""):
    """Find an existing running session for a given user + training + tenant.

    Session uniqueness is (user, tenant, training): the SAME user may have a
    separate environment per tenant, so a session in a DIFFERENT tenant must NOT
    match. `tenant` is the caller's own environment URL (getEnvironmentUrl()),
    compared against the job's stored tenant URL/id. Returns 404 if none found.
    """
    await _require_arena_auth(request)
    cursor = 0
    while True:
        cursor, keys = await pool.scan(cursor, match="job:running:enablement-*", count=100)
        for key in keys:
            meta = await pool.hgetall(key)
            if not meta:
                continue
            if meta.get("terminating"):
                continue  # being torn down — treat as gone
            tenant_match = (not tenant) or tenant in (
                meta.get("dt_tenant_url", ""), meta.get("arena_tenant", ""))
            if (meta.get("arena_user") == userId
                    and meta.get("training_id") == trainingId and tenant_match):
                job_id = meta.get("job_id", key.split(":")[-1])
                livelog = await pool.get(f"job:livelog:{job_id}")
                if livelog and "Daemon ready" in livelog:
                    status = "ready"
                elif meta.get("worker_id") in ("queued", ""):
                    status = "queued"
                else:
                    status = "provisioning"
                full = _has_full_access(request)
                return {
                    "jobId":      job_id,
                    "status":     status,
                    "wsUrl":      f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{job_id}/shell",
                    "trainingId": meta.get("training_id", ""),
                    "userId":     meta.get("arena_user", "") if full
                                  else masking.mask_email(meta.get("arena_user", "")),
                    "expiresAt":  meta.get("expires_at", ""),
                    "dtSessionId": meta.get("dt_hostgroup", "") if full
                                   else masking.mask_email(meta.get("dt_hostgroup", "")),
                }
        if cursor == 0:
            break
    raise HTTPException(status_code=404, detail="No active session found")


@app.get("/api/arena/active-sessions")
async def api_arena_active_sessions(userId: str, request: Request, tenant: str = ""):
    """Every running session for a user+tenant, ACROSS all trainings.

    Server-side resource guard: only one live environment per user+tenant is
    allowed. The app calls this before launching so a second training can't be
    provisioned while ANY session is still running here — authoritative even when
    the learner switches browser/device (localStorage can't see those). Mirrors
    user-session but drops the training filter and returns a list.

    Compat window: anonymous callers still get the (PII-masked) list; under
    ARENA_AUTH_ENFORCE=1 they get 401 instead.
    """
    await _require_arena_auth(request)
    full = _has_full_access(request)
    sessions = []
    cursor = 0
    while True:
        cursor, keys = await pool.scan(cursor, match="job:running:enablement-*", count=100)
        for key in keys:
            meta = await pool.hgetall(key)
            if not meta or meta.get("terminating"):
                continue
            tenant_match = (not tenant) or tenant in (
                meta.get("dt_tenant_url", ""), meta.get("arena_tenant", ""))
            if meta.get("arena_user") == userId and tenant_match:
                job_id = meta.get("job_id", key.split(":")[-1])
                livelog = await pool.get(f"job:livelog:{job_id}")
                if livelog and "Daemon ready" in livelog:
                    status = "ready"
                elif meta.get("worker_id") in ("queued", ""):
                    status = "queued"
                else:
                    status = "provisioning"
                sessions.append({
                    "jobId":      job_id,
                    "status":     status,
                    "trainingId": meta.get("training_id", ""),
                    "userId":     meta.get("arena_user", "") if full
                                  else masking.mask_email(meta.get("arena_user", "")),
                    "expiresAt":  meta.get("expires_at", ""),
                })
        if cursor == 0:
            break
    return {"sessions": sessions, "count": len(sessions)}


@app.post("/api/arena/sessions/{job_id}/shell-token")
async def api_arena_shell_token(job_id: str, request: Request):
    """Issue a single-use 60-second shell token for an arena session.

    Arena orbital proxy function calls this server-side (no browser OAuth needed).
    The returned token is passed to the /terminal/{job_id}?token=... page.

    NOTE (enforcement blocker): the /shell/{job_id} and /terminal/{job_id}
    pages ALSO call this anonymously from the learner's browser (their inline
    JS refreshes the 60 s token right before connecting). Browser JS cannot
    hold the service bearer, so those flows will show up as
    ARENA-LEGACY-CALLER during the compat window and would break under
    ARENA_AUTH_ENFORCE=1 — they need a page-scoped token scheme before
    enforcement flips.
    """
    await _require_arena_auth(request)
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if meta.get("worker_id") in ("queued", "stub"):
        raise HTTPException(status_code=409, detail="Session not ready yet")

    token = secrets.token_hex(16)
    await pool.set(f"shell:token:{token}", job_id, ex=60)
    return {"token": token}


@app.get("/shell/{job_id}", response_class=HTMLResponse)
async def arena_shell_page(job_id: str):
    """Standalone xterm.js shell page for Arena training sessions.

    Same self-contained HTML as the ops-dashboard popup (shellPopupHtml in app.js)
    but uses /api/arena/sessions/{id}/shell-token so no ops-portal auth is needed.
    Intended to be window.open()'d from the DT App.
    """
    base_url = "https://autonomous-enablements.whydevslovedynatrace.com"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Shell · {job_id}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
@font-face{{font-family:'MesloLGS NF';src:url('https://cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/MesloLGS%20NF%20Regular.ttf') format('truetype');font-weight:normal;font-style:normal}}
@font-face{{font-family:'MesloLGS NF';src:url('https://cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/MesloLGS%20NF%20Bold.ttf') format('truetype');font-weight:bold;font-style:normal}}
html,body{{margin:0;padding:0;background:#000;width:100%;height:100vh;overflow:hidden}}
#t{{width:100%;height:100vh;padding:4px;box-sizing:border-box}}
</style>
</head>
<body>
<div id="t"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
(async()=>{{
  const jobId={json.dumps(job_id)};
  const BASE='{base_url}';
  const term=new Terminal({{cursorBlink:true,fontFamily:'"MesloLGS NF","Cascadia Code NF",ui-monospace,monospace',fontSize:13,theme:{{background:'#000000',foreground:'#e2e8f2',cursor:'#00b4de'}}}});
  const fit=new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById('t'));
  await document.fonts.load('13px "MesloLGS NF"').catch(()=>{{}});
  // Fit robustly: a single fit() before layout/fonts settle leaves the bottom
  // prompt row clipped. Re-fit on rAF, after a short delay, and on load so the
  // last (command-input) row is always fully visible.
  const doFit=()=>{{try{{fit.fit();}}catch(e){{}}}};
  doFit();
  requestAnimationFrame(doFit);
  setTimeout(doFit,150);
  window.addEventListener('load',doFit);
  term.write('\\x1b[36m◈  Connecting to isolation container…\\x1b[0m\\r\\n');
  let token='';
  try{{
    const r=await fetch(BASE+'/api/arena/sessions/'+jobId+'/shell-token',{{method:'POST'}});
    if(!r.ok){{term.write('\\r\\n\\x1b[31mFailed to get shell token ('+r.status+')\\x1b[0m\\r\\n');return;}}
    ({{token}}=await r.json());
  }}catch(err){{term.write('\\r\\n\\x1b[31mError: '+err+'\\x1b[0m\\r\\n');return;}}
  const ws=new WebSocket('wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/'+jobId+'/shell?token='+encodeURIComponent(token)+'&rows='+term.rows+'&cols='+term.cols);
  ws.binaryType='arraybuffer';
  ws.onopen=()=>{{doFit();term.write('\\x1b[32m◈  Tunnel established — spawning shell\\x1b[0m\\r\\n\\r\\n');ws.send(JSON.stringify({{type:'resize',rows:term.rows,cols:term.cols}}));}};
  ws.onmessage=e=>{{term.write(e.data instanceof ArrayBuffer?new Uint8Array(e.data):e.data);}};
  ws.onclose=()=>term.write('\\r\\n\\x1b[90m[connection closed]\\x1b[0m\\r\\n');
  ws.onerror=()=>term.write('\\r\\n\\x1b[31m[WebSocket error]\\x1b[0m\\r\\n');
  term.onData(d=>{{if(ws.readyState===WebSocket.OPEN)ws.send(new TextEncoder().encode(d));}});
  term.onResize(({{rows,cols}})=>{{if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({{type:'resize',rows,cols}}));}});
  window.addEventListener('resize',doFit);
}})();
</script>
</body>
</html>""")


@app.get("/terminal/{job_id}", response_class=HTMLResponse)
async def arena_terminal_page(job_id: str, token: str = ""):
    """Standalone xterm.js terminal page for Arena training sessions.

    Tabs: Log (livelog stream, plain text) | Shell (xterm PTY, lazy-opened) | Apps (proxy + docs)

    Shell is opened lazily when the user first clicks the Shell tab and "Daemon ready"
    has been seen in the log — this avoids xterm measuring 0x0 when hidden.

    Auth: single-use shell token passed as ?token=.
    """
    ws_url   = f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{job_id}/shell"
    base_url = "https://autonomous-enablements.whydevslovedynatrace.com"

    meta = await pool.hgetall(f"job:running:{job_id}") or {}
    repo_full = meta.get("repo", "")
    repo_name = repo_full.split("/")[-1] if repo_full else ""
    docs_url  = f"https://dynatrace-wwse.github.io/{repo_name}/" if repo_name else ""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Training Environment</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<style>
@font-face {{
  font-family: 'MesloLGS NF';
  src: url('https://cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/MesloLGS%20NF%20Regular.ttf') format('truetype');
  font-weight: normal; font-style: normal;
}}
@font-face {{
  font-family: 'MesloLGS NF';
  src: url('https://cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/MesloLGS%20NF%20Bold.ttf') format('truetype');
  font-weight: bold; font-style: normal;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ height: 100%; overflow: hidden; background: #1a1a2e; color: #d4d4d4; }}
body {{ display: flex; flex-direction: column; font-family: -apple-system, sans-serif; }}

#topbar {{
  background: #16213e; color: #a0aec0; font-size: 12px; line-height: 38px;
  padding: 0 16px; display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0; border-bottom: 1px solid #0d3460; gap: 16px;
}}
#brand {{ display: flex; align-items: center; gap: 8px; }}
#brand-logo {{ color: #00b4d8; font-size: 18px; }}
#brand-name {{ color: #e2e8f0; font-weight: 600; font-size: 13px; letter-spacing: 0.3px; }}
#status {{ font-size: 11px; color: #718096; white-space: nowrap; }}
#status.ok   {{ color: #48bb78; }}
#status.err  {{ color: #fc8181; }}
#status.busy {{ color: #ed8936; }}

#tabbar {{
  background: #16213e; border-bottom: 1px solid #0d3460;
  display: flex; flex-shrink: 0; padding: 0 8px;
}}
.tab {{
  padding: 0 20px; line-height: 36px; cursor: pointer;
  border-bottom: 2px solid transparent; color: #718096;
  font-size: 12px; user-select: none; transition: color .15s; position: relative;
}}
.tab:hover  {{ color: #a0aec0; }}
.tab.active {{ color: #00b4d8; border-bottom-color: #00b4d8; }}
.tab.disabled {{ opacity: 0.4; cursor: not-allowed; }}
.tab .badge {{
  display: inline-block; margin-left: 6px; padding: 1px 5px; border-radius: 8px;
  font-size: 10px; background: #48bb78; color: #1a1a2e; font-weight: 700;
  vertical-align: middle;
}}

#panels {{ flex: 1; display: flex; flex-direction: column; min-height: 0; }}

/* ── Log panel ── */
#panel-log {{ flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }}
#log-output {{
  flex: 1; min-height: 0; margin: 0; padding: 14px;
  overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  font: 12px/1.5 ui-monospace, Menlo, monospace; color: #c9d1d9; background: #0d1117;
}}
#log-status {{
  padding: 6px 16px; font-size: 11px; color: #718096; background: #16213e;
  border-top: 1px solid #0d3460; flex-shrink: 0;
}}
.ansi-bold {{ font-weight: bold; }}
.ansi-red {{ color: #f85149; }} .ansi-green {{ color: #3fb950; }}
.ansi-yellow {{ color: #d29922; }} .ansi-blue {{ color: #58a6ff; }}
.ansi-magenta {{ color: #bc8cff; }} .ansi-cyan {{ color: #39c5cf; }}
.ansi-white {{ color: #c9d1d9; }} .ansi-gray {{ color: #8b949e; }}

/* ── Shell panel ── */
#panel-shell {{ flex: 1; padding: 4px; min-height: 0; display: none; flex-direction: column; }}
#terminal {{ flex: 1; min-height: 0; }}

/* ── Apps panel ── */
#panel-apps {{
  flex: 1; padding: 20px; overflow-y: auto; display: none; background: #1a1a2e;
}}
#panel-apps h3 {{ color: #e2e8f0; font-size: 13px; margin-bottom: 16px; }}
.app-card {{
  background: #16213e; border: 1px solid #0d3460; border-radius: 6px;
  padding: 14px 16px; margin-bottom: 10px;
  display: flex; align-items: center; justify-content: space-between;
}}
.app-card.docs-card {{ border-color: #1a4a6b; }}
.app-name {{ color: #00b4d8; font-size: 13px; font-weight: 600; }}
.app-sub  {{ color: #718096; font-size: 11px; margin-top: 3px; }}
.app-btn {{
  background: #0e639c; color: #fff; padding: 5px 14px; border-radius: 4px;
  font-size: 12px; text-decoration: none; transition: background .15s; white-space: nowrap;
}}
.app-btn:hover {{ background: #1177bb; }}
.app-btn.docs-btn {{ background: #2d6a4f; }}
.app-btn.docs-btn:hover {{ background: #40916c; }}
</style>
</head>
<body>
<div id="topbar">
  <div id="brand">
    <span id="brand-logo">⬡</span>
    <span id="brand-name">Dynatrace Enablements</span>
  </div>
  <span id="status" class="busy">Setting up environment…</span>
</div>
<div id="tabbar">
  <div class="tab active" id="tab-log"   onclick="switchTab('log')">📋 Log</div>
  <div class="tab disabled" id="tab-shell" onclick="switchTab('shell')">⌨ Shell</div>
  <div class="tab" id="tab-apps" onclick="switchTab('apps')">🚀 Apps</div>
</div>
<div id="panels">
  <div id="panel-log">
    <pre id="log-output">Loading…</pre>
    <div id="log-status">Waiting for provisioning log…</div>
  </div>
  <div id="panel-shell">
    <div id="terminal"></div>
  </div>
  <div id="panel-apps">
    <h3>Apps &amp; Resources</h3>
    <div id="apps-list">
      {'<div class="app-card docs-card"><div><div class="app-name">📖 Training Documentation</div><div class="app-sub">GitHub Pages</div></div><a class="app-btn docs-btn" href="' + docs_url + '" target="_blank" rel="noopener">Open ↗</a></div>' if docs_url else ''}
      <div id="dynamic-apps"></div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
<script>
const JOB_ID   = {json.dumps(job_id)};
const TOKEN    = {json.dumps(token)};
const WS_URL   = {json.dumps(ws_url)};
const BASE_URL = {json.dumps(base_url)};

const statusEl  = document.getElementById('status');
const logOutput = document.getElementById('log-output');
const logStatus = document.getElementById('log-status');

// ── Tab switching ─────────────────────────────────────────────────────────────
let termOpened = false;
let shellReady = false; // true once "Daemon ready" seen

function switchTab(name) {{
  if (name === 'shell' && !shellReady) return; // locked until env ready

  ['log','shell','apps'].forEach(n => {{
    const panel = document.getElementById('panel-' + n);
    const tab   = document.getElementById('tab-' + n);
    const show  = n === name;
    panel.style.display = show ? (n === 'shell' ? 'flex' : (n === 'apps' ? 'block' : 'flex')) : 'none';
    tab.className = 'tab' + (show ? ' active' : '') +
                    (n === 'shell' && !shellReady ? ' disabled' : '');
  }});

  if (name === 'shell') {{
    if (!termOpened) {{
      openTerminal();
    }} else {{
      setTimeout(() => fitAddon && fitAddon.fit(), 50);
    }}
  }}
}}

// ── Log panel (pre + ANSI colors) ────────────────────────────────────────────
const logEl = document.getElementById('log-output');
const ANSI_COLORS = {{30:'gray',31:'red',32:'green',33:'yellow',34:'blue',35:'magenta',36:'cyan',37:'white',
                     90:'gray',91:'red',92:'green',93:'yellow',94:'blue',95:'magenta',96:'cyan',97:'white'}};
function escHtml(s){{return s.replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]))}}
function ansiToHtml(text){{
  let out='',open=0,last=0;
  text.replace(/\x1b\[([0-9;]*)m/g,(m,codes,i)=>{{
    out+=escHtml(text.slice(last,i)); last=i+m.length;
    const parts=codes?codes.split(';').map(Number):[0];
    for(const c of parts){{
      if(c===0){{while(open-->0)out+='</span>';open=0;}}
      else if(c===1){{out+='<span class="ansi-bold">';open++;}}
      else if(ANSI_COLORS[c]){{out+='<span class="ansi-'+ANSI_COLORS[c]+'">';open++;}}
    }}
    return m;
  }});
  out+=escHtml(text.slice(last));
  while(open-->0)out+='</span>';
  return out;
}}

let lastLogLen = 0;
let logTimer   = setInterval(pollLivelog, 2000);

async function pollLivelog() {{
  try {{
    const r = await fetch(`${{BASE_URL}}/api/jobs/${{JOB_ID}}/livelog`);
    if (r.status === 404) {{
      logStatus.textContent = 'Waiting for container to start…';
      return;
    }}
    const text = await r.text();
    if (text.length > lastLogLen) {{
      if (lastLogLen === 0) logEl.textContent = '';
      logEl.innerHTML += ansiToHtml(text.slice(lastLogLen));
      logEl.scrollTop = logEl.scrollHeight;
      lastLogLen = text.length;
    }}
    const ready = text.includes('Daemon ready');
    logStatus.textContent = ready
      ? '✓ Environment ready — click Shell to connect'
      : `Streaming log… (${{Math.round(lastLogLen / 1024)}} KB)`;

    if (ready && !shellReady) {{
      clearInterval(logTimer); logTimer = null;
      shellReady = true;
      const shellTab = document.getElementById('tab-shell');
      shellTab.className = 'tab';
      shellTab.innerHTML = '⌨ Shell <span class="badge">Ready</span>';
      statusEl.textContent = 'Environment ready';
      statusEl.className   = 'ok';
    }}
  }} catch {{}}
}}

// ── xterm — opened lazily when Shell tab first selected ───────────────────────
let term, fitAddon, ws;

async function openTerminal() {{
  termOpened = true;
  statusEl.textContent = 'Fetching shell token…';
  statusEl.className   = 'busy';

  // Always fetch a fresh token right before connecting — the URL token (if any)
  // has a 60s TTL and will have expired if the user waited for provisioning.
  let token = TOKEN; // fallback to URL param
  try {{
    const tr = await fetch(`${{BASE_URL}}/api/arena/sessions/${{JOB_ID}}/shell-token`, {{ method: 'POST' }});
    if (tr.ok) {{ const j = await tr.json(); token = j.token; }}
  }} catch {{ /* use URL token as fallback */ }}

  statusEl.textContent = 'Connecting shell…';

  document.fonts.load('13px "MesloLGS NF"').then(() => {{
    term = new Terminal({{
      cursorBlink: true,
      fontSize: 13,
      fontFamily: '"MesloLGS NF", "Cascadia Code NF", "Hack Nerd Font", ui-monospace, Menlo, monospace',
      theme: {{
        background: '#1a1a2e', foreground: '#d4d4d4', cursor: '#00b4d8',
        selectionBackground: '#264f78',
      }},
      scrollback: 5000,
      convertEol: false,
    }});
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon.WebLinksAddon());

    term.open(document.getElementById('terminal'));
    fitAddon.fit(); // panel-shell is VISIBLE at this point — correct dimensions

    const rows = term.rows, cols = term.cols;
    ws = new WebSocket(`${{WS_URL}}?token=${{encodeURIComponent(token)}}&rows=${{rows}}&cols=${{cols}}`);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {{
      statusEl.textContent = 'Connected';
      statusEl.className   = 'ok';
      ws.send(JSON.stringify({{ type: 'resize', rows, cols }}));
    }};
    ws.onmessage = (e) => {{
      if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data));
      else term.write(e.data);
    }};
    ws.onclose = (e) => {{
      statusEl.textContent = `Disconnected (${{e.code}})`;
      statusEl.className   = 'err';
      term.writeln('\r\n\x1b[31mSession closed.\x1b[0m');
    }};
    ws.onerror = () => {{
      statusEl.textContent = 'Connection error';
      statusEl.className   = 'err';
    }};

    const encoder = new TextEncoder();
    term.onData(d => {{ if (ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(d)); }});
    term.onResize(sz => {{
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({{ type: 'resize', rows: sz.rows, cols: sz.cols }}));
    }});
  }});
}}

window.addEventListener('resize', () => {{
  if (termOpened && fitAddon) fitAddon.fit();
}});

// ── Apps polling — starts on page load, not just when shell connects ──────────
let appsInterval = null;
function startAppsPolling() {{
  if (appsInterval) return;
  pollApps();
  appsInterval = setInterval(pollApps, 10000);
}}
startAppsPolling(); // show docs card immediately
async function pollApps() {{
  try {{
    const r = await fetch(`${{BASE_URL}}/api/jobs/${{JOB_ID}}/apps`);
    if (!r.ok) return;
    const {{apps}} = await r.json();
    const el = document.getElementById('dynamic-apps');
    el.innerHTML = (apps || []).map(a => `
      <div class="app-card">
        <div>
          <div class="app-name">${{a.name}}</div>
          <div class="app-sub">port ${{a.port}}</div>
        </div>
        <a class="app-btn" href="${{a.subdomain_url || (BASE_URL + a.proxy_url)}}" target="_blank" rel="noopener">Open ↗</a>
      </div>`).join('');
  }} catch {{}}
}}
</script>
</body>
</html>""")


class ArenaExecRequest(BaseModel):
    command: str
    # When True, run inside an interactive zsh (-i) so .zshrc / my_functions.sh are sourced.
    # Use for STEP_SETUP commands that call shell functions. Default False (faster, no profile).
    interactive: bool = False
    # Per-call timeout (seconds), clamped to [10, 900]. Solution runs
    # (deployApplicationMonitoring & co.) legitimately outlive the old fixed
    # 120 s cap — the app passes a higher value for those.
    timeoutSeconds: int = 120


@app.post("/api/arena/sessions/{job_id}/exec-start")
async def api_arena_session_exec_start(job_id: str, body: ArenaExecRequest, request: Request):
    """Start a command in the background and return an execId immediately.

    For long-running commands (solution runs: operator deploy, ActiveGate
    rollout) that outlive the Dynatrace app-function's ~120 s cap: the app
    starts the exec here and polls /exec-status/{execId} until done. The
    result is kept in Redis for 1 h.
    """
    await _require_arena_auth(request)
    import uuid as _uuid

    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if meta.get("status") == "provisioning" or meta.get("worker_id") == "stub":
        raise HTTPException(status_code=409, detail="Session not ready yet")

    exec_id = _uuid.uuid4().hex[:12]
    key = f"exec:result:{job_id}:{exec_id}"
    await pool.set(key, json.dumps({"done": False}), ex=3600)

    async def _run():
        try:
            result = await _arena_exec_run(job_id, meta, body)
        except Exception as exc:  # pragma: no cover — defensive
            result = {"stdout": "", "stderr": str(exc), "exitCode": -1}
        await pool.set(key, json.dumps({"done": True, **result}), ex=3600)

    asyncio.ensure_future(_run())
    return {"execId": exec_id, "done": False}


@app.get("/api/arena/sessions/{job_id}/exec-status/{exec_id}")
async def api_arena_session_exec_status(job_id: str, exec_id: str, request: Request):
    """Poll a background exec started via /exec-start. Returns {done, stdout, stderr, exitCode}."""
    await _require_arena_auth(request)
    raw = await pool.get(f"exec:result:{job_id}:{exec_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Unknown or expired execId")
    return json.loads(raw)


@app.post("/api/arena/sessions/{job_id}/exec")
async def api_arena_session_exec(job_id: str, body: ArenaExecRequest, request: Request):
    """Run a command inside the training container.

    Uses the same SSH→docker-exec chain as the PTY bridge but without a TTY.
    Set interactive=True to load .zshrc (required for shell functions defined
    in my_functions.sh, e.g. dynatraceEvalReadSaveCredentials, generateDynakube).

    All executions are appended to job:exec-log:{job_id} in Redis for auditing.

    Returns: { stdout, stderr, exitCode }
    """
    await _require_arena_auth(request)
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if meta.get("status") == "provisioning" or meta.get("worker_id") == "stub":
        raise HTTPException(status_code=409, detail="Session not ready yet")

    return await _arena_exec_run(job_id, meta, body)


async def _arena_exec_run(job_id: str, meta: dict, body: ArenaExecRequest) -> dict:
    """Build the exec chain (Sysbox or Codespace), run it, audit-log the result."""
    import asyncio as _asyncio
    import shlex as _shlex

    worker_id = meta.get("worker_id", "")
    repo = meta.get("repo", "")
    repo_name = repo.split("/")[-1] if "/" in repo else repo
    container = meta.get("sb_name") or f"sb-{job_id[-32:]}"

    # interactive=True: zsh -i loads .zshrc so my_functions.sh functions are available
    zsh_flag = "-ic" if body.interactive else "-c"

    # Codespace sessions have no Sysbox container — exec through the learner's
    # Codespace (`gh codespace ssh`) into the nested dt-enablement container,
    # selected by image like the shell bridge. Same audit-log flow below.
    if meta.get("provider") == "codespace":
        from dashboard.github_oauth import get_user_token
        token = await get_user_token(pool, meta.get("dtUser", ""))
        if not token:
            raise HTTPException(status_code=502, detail="GitHub credential for this Codespace is gone")
        cs_env = {**os.environ, "GH_TOKEN": token}
        cs_env.pop("GITHUB_TOKEN", None)
        # docker-exec sessions do NOT get the Codespace's secret env (GitHub only
        # injects DT_ENVIRONMENT & co. into login shells) — load the canonical
        # secrets file (KEY=<base64(value)> lines) first so lab commands
        # referencing $DT_* work. Never overrides an already-set variable.
        secrets_src = (
            "if [ -r /workspaces/.codespaces/shared/.env-secrets ]; then "
            "while IFS='=' read -r k v; do "
            'case "$k" in ""|\\#*) continue;; esac; '
            '[ -n "$(printenv "$k" 2>/dev/null)" ] && continue; '
            'export "$k=$(printf %s "$v" | base64 -d 2>/dev/null)"; '
            "done < /workspaces/.codespaces/shared/.env-secrets; fi; "
        )
        inner = (
            "CID=$(docker ps --format '{{.ID}} {{.Image}}' | "
            "awk '/dt-enablement/{print $1; exit}'); "
            f"WS='/workspaces/{repo_name}'; "
            "if [ -z \"$CID\" ]; then echo 'dt-enablement container not found' >&2; exit 1; fi; "
            "docker exec \"$CID\" test -d \"$WS\" 2>/dev/null || WS=/workspaces; "
            f"docker exec -w \"$WS\" \"$CID\" zsh {zsh_flag} {_shlex.quote(secrets_src + body.command)}"
        )
        full_cmd = ["gh", "codespace", "ssh", "-c", job_id, "--", inner]
        exec_env = cs_env
    else:
        cmd_args = ["docker", "exec", container,
                    "docker", "exec", "-w", f"/workspaces/{repo_name}", "dt",
                    "zsh", zsh_flag, body.command]

        worker_rec = await pool.hgetall(f"worker:{worker_id}") if worker_id != "master" else {}
        ssh_host = worker_rec.get("ssh_host", "")
        if ssh_host:
            full_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                        ssh_host, _shlex.join(cmd_args)]
        else:
            full_cmd = cmd_args
        exec_env = None

    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    timeout_s = max(10, min(int(body.timeoutSeconds or 120), 900))
    try:
        proc = await _asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            env=exec_env,
        )
        stdout_b, stderr_b = await _asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        result = {
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
            "exitCode": proc.returncode,
        }
    except _asyncio.TimeoutError:
        # Don't leak the subprocess — without kill() a timed-out ssh/docker exec
        # keeps running server-side while the UI already reported failure.
        try:
            proc.kill()
        except Exception:
            pass
        result = {"stdout": "", "stderr": f"Command timed out after {timeout_s} seconds", "exitCode": -1}
    except Exception as exc:
        result = {"stdout": "", "stderr": str(exc), "exitCode": -1}

    # Append to per-job exec audit log (capped at 500 entries, 24h TTL)
    log_key = f"job:exec-log:{job_id}"
    entry = json.dumps({
        "ts": ts,
        "command": body.command,
        "interactive": body.interactive,
        "exitCode": result["exitCode"],
        "stdout": result["stdout"][:2000],
        "stderr": result["stderr"][:500],
    })
    try:
        await pool.rpush(log_key, entry)
        await pool.ltrim(log_key, -500, -1)
        await pool.expire(log_key, 86400)
    except Exception as log_err:
        log.warning("Could not write exec-log for %s: %s", job_id, log_err)

    return result


@app.post("/api/arena/sessions/{job_id}/terminate")
async def api_arena_terminate(job_id: str, request: Request):
    """Terminate a training session from the Arena app (no oauth2-proxy auth required).

    Publishes on ``ops:terminate`` — the worker kills the Sysbox container and
    cleans up Redis state. Mirrors /api/jobs/{job_id}/terminate but skips the
    writer-role check so the Dynatrace app function can call it with just the
    ORBITAL_TOKEN Bearer header.

    Returns 404 if the job is not currently running.
    """
    await _require_arena_auth(request)
    if not await pool.exists(f"job:running:{job_id}"):
        in_completed = await pool.exists(f"job:log:{job_id}")
        detail = (
            f"Job {job_id} has already completed."
            if in_completed else
            f"Job {job_id} is not running."
        )
        raise HTTPException(404, detail)

    # Best-effort: revoke provisioned DT tokens
    meta = await pool.hgetall(f"job:running:{job_id}")
    # Only Orbital-minted tokens carry a stored auth cred here. App-minted tokens
    # (multi-tenancy) are revoked by the app itself — Orbital holds no tenant cred.
    if meta.get("mint_kind") == "platform" and meta.get("dt_token_ids") and meta.get("dt_tenant_url"):
        try:
            token_ids = json.loads(meta["dt_token_ids"])
            prov = _gen3_platform_provisioner(meta["dt_tenant_url"])
            if prov:
                asyncio.create_task(prov.revoke_tokens(token_ids))
        except Exception as exc:
            log.warning("Could not initiate platform-token revocation for %s: %s", job_id, exc)
    elif (meta.get("dt_token_ids") and meta.get("dt_tenant_url")
            and (meta.get("dt_auth_token") or meta.get("dt_oauth_client_id"))):
        from provisioning import DTTokenProvisioner
        try:
            token_ids = json.loads(meta["dt_token_ids"])
            provisioner = DTTokenProvisioner(
                tenant_url=meta["dt_tenant_url"],
                api_token=meta.get("dt_auth_token", ""),
                oauth_client_id=meta.get("dt_oauth_client_id", ""),
                oauth_client_secret=meta.get("dt_oauth_client_secret", ""),
            )
            asyncio.create_task(provisioner.revoke_tokens(token_ids))
        except Exception as exc:
            log.warning("Could not initiate token revocation for %s: %s", job_id, exc)

    # Mark terminating IMMEDIATELY so the UI sees the session gone right away — the
    # worker's container teardown is slow, but session-status/find-session treat a
    # terminating job as gone so the user doesn't have to click Terminate repeatedly.
    await pool.hset(f"job:running:{job_id}", "terminating", "1")
    await pool.publish("ops:terminate", job_id)
    log.info("Arena termination requested for %s", job_id)
    return {"status": "termination_requested", "job_id": job_id}


# ── Live training sessions (bootcamp cohorts) ─────────────────────────────────
# Registry for instructor-led cohorts: a trainer creates a session with a
# roster of emails, learners join from any tenant, the trainer starts/ends for
# everyone at once. Called via the app's `orbital` app function, which sends
# NO X-Auth headers — trainer actions are gated by matching the caller-supplied
# trainerEmail against the stored one (same openness as /api/arena/*). All
# decision logic is in dashboard/live_sessions.py (pure, unit-tested).


def _live_keys(session_id: str) -> tuple[str, str, str]:
    """The three Redis keys of a live session: (hash, roster set, joined hash)."""
    base = f"live:session:{session_id}"
    return base, f"{base}:roster", f"{base}:joined"


class LiveSessionCreate(BaseModel):
    title: str = ""
    trainingId: str = ""
    ref: str = ""                 # optional content branch
    trainerEmail: str = ""
    roster: list[str] = []
    # Workshop scheduling (EPIC-002) — all optional; absent fields keep the
    # pre-workshop behavior (immediate state=open, no seat cap).
    scheduledAt: str = ""         # ISO8601 UTC → initial state "scheduled"
    timezone: str = ""            # IANA zone name
    durationMinutes: int = 0
    maxSeats: int = 0             # 0 = unlimited


class LiveSessionJoin(BaseModel):
    email: str = ""


class LiveSessionTrainerAction(BaseModel):
    trainerEmail: str = ""


class LiveSessionJoinByCode(BaseModel):
    code: str = ""
    email: str = ""
    name: str = ""


class LiveSessionProvisionAll(BaseModel):
    trainerEmail: str = ""
    tenant: str = ""              # trainer's arena tenant URL
    # Subset of roster emails to provision this call (empty = whole roster) —
    # lets the app chunk large rosters under its 120s function cap.
    emails: list[str] = []
    # Per-learner app-minted token env (email -> {dtEnv, dtTokenIds}). Orbital
    # stores no tenant credential, so the app mints and passes values through,
    # exactly as the single-user /api/arena/provision path does.
    perUser: dict[str, dict] = {}


async def _reserve_join_code(session_id: str) -> str:
    """Generate a join code and claim it via SET NX on live:joincode:{code}
    so two sessions can never share one. 31^6 ≈ 887M codes — collisions are
    vanishingly rare, but retry a few times anyway."""
    for _ in range(20):
        code = live_sessions.generate_join_code()
        if await pool.set(f"live:joincode:{code}", session_id, nx=True):
            return code
    raise HTTPException(status_code=500, detail="could not allocate a unique join code")


@app.post("/api/live/sessions")
async def api_live_session_create(body: LiveSessionCreate, request: Request):
    """Create a live session (state=open) with a roster of invited emails.

    Emails are trimmed + lowercased; entries without an '@' are dropped.
    400 when title/trainingId/trainerEmail is missing or the roster is empty
    after normalization. Returns the full session JSON (trainer view).

    Workshops (EPIC-002): scheduledAt/timezone/durationMinutes/maxSeats are
    optional; creating WITH scheduledAt yields state "scheduled" (open the
    room later via /open-registration or /start), without it state "open"
    exactly as before. Every session gets a unique join code (trainer-only
    in payloads) resolvable via POST /api/live/sessions/join-by-code.

    Auth: service bearer (the app's orbital function) or a signed-in writer.
    """
    await _require_service_or_writer(request)
    try:
        fields = live_sessions.validate_create(
            body.title, body.trainingId, body.trainerEmail, body.roster)
        schedule = live_sessions.validate_schedule(
            body.scheduledAt, body.timezone, body.durationMinutes, body.maxSeats)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session_id = _new_job_id()
    join_code = await _reserve_join_code(session_id)
    now = datetime.now(timezone.utc)
    session = {
        "title":        fields["title"],
        "trainingId":   fields["trainingId"],
        "ref":          (body.ref or "").strip(),
        "trainerEmail": fields["trainerEmail"],
        "state":        live_sessions.initial_state(schedule["scheduledAt"]),
        "createdAt":    now.isoformat(),
        "startedAt":    "",
        "endedAt":      "",
        "joinCode":     join_code,
    }
    # Optional workshop fields — only written when present so pre-workshop
    # sessions (and their payloads) stay byte-identical.
    session.update({k: v for k, v in schedule.items() if v})
    sess_key, roster_key, _ = _live_keys(session_id)
    await pool.hset(sess_key, mapping=session)
    await pool.sadd(roster_key, *fields["roster"])
    await pool.zadd("live:sessions:index", {session_id: now.timestamp()})
    log.info("Live session %s created by %s (%s, %d invited)", session_id,
             fields["trainerEmail"], fields["trainingId"], len(fields["roster"]))
    return live_sessions.shape_detail(
        session_id, session, fields["roster"], {}, fields["trainerEmail"])


@app.get("/api/live/sessions")
async def api_live_sessions_list(request: Request, email: str = ""):
    """Active (state != ended) sessions visible to an email — as trainer or
    roster member. Index entries whose keys have TTL-expired are skipped.

    The email is caller-supplied (the app proxy authenticates with the
    service bearer, not per-user) — anonymous callers therefore get masked
    trainer emails and never a joinCode."""
    email = live_sessions.normalize_email(email)
    if not live_sessions.is_valid_email(email):
        raise HTTPException(status_code=400, detail="a valid email query parameter is required")
    full = _has_full_access(request)
    sessions = []
    for session_id in await pool.zrevrange("live:sessions:index", 0, -1):
        sess_key, roster_key, joined_key = _live_keys(session_id)
        session = await pool.hgetall(sess_key)
        if not session:
            continue  # expired after `ended` — tolerate the stale index member
        roster = await pool.smembers(roster_key)
        if not live_sessions.is_listed(session, roster, email):
            continue
        joined = await pool.hgetall(joined_key)
        item = live_sessions.shape_summary(
            session_id, session, roster, joined, email)
        sessions.append(item if full else masking.mask_live_summary(item))
    return {"sessions": sessions, "count": len(sessions)}


@app.get("/api/live/sessions/{session_id}")
async def api_live_session_detail(session_id: str, request: Request, email: str = ""):
    """Full session state. The roster and per-learner joined list are only
    included when the caller email matches trainerEmail; learners get counts.

    The trainerEmail match is caller-supplied — anonymous callers get the
    masked view (no roster/joined/joinCode, masked trainer email) even when
    they present the trainer's email."""
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    roster = await pool.smembers(roster_key)
    joined = await pool.hgetall(joined_key)
    detail = live_sessions.shape_detail(session_id, session, roster, joined, email)
    return detail if _has_full_access(request) else masking.mask_live_detail(detail)


@app.post("/api/live/sessions/{session_id}/join")
async def api_live_session_join(session_id: str, body: LiveSessionJoin):
    """Learner joins a session they are invited to. Idempotent — re-joining
    keeps the original joinedAt. 403 when not on the roster, 409 when ended."""
    email = live_sessions.normalize_email(body.email)
    if not live_sessions.is_valid_email(email):
        raise HTTPException(status_code=400, detail="a valid email is required")
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    roster = await pool.smembers(roster_key)
    err = live_sessions.join_error(session.get("state", ""), email, roster)
    if err:
        raise HTTPException(status_code=err[0], detail=err[1])
    await pool.hsetnx(joined_key, email, datetime.now(timezone.utc).isoformat())
    return {"state": session.get("state", ""),
            "joinedCount": await pool.hlen(joined_key)}


@app.post("/api/live/sessions/{session_id}/start")
async def api_live_session_start(session_id: str, body: LiveSessionTrainerAction, request: Request):
    """Trainer starts the session: scheduled|open → running (learners'
    waiting screens flip). Idempotent when already running; 403 on
    trainerEmail mismatch. Auth: service bearer or signed-in writer."""
    await _require_service_or_writer(request)
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not live_sessions.is_trainer(body.trainerEmail, session):
        raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    try:
        new_state, changed = live_sessions.apply_transition(session.get("state", ""), "start")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if changed:
        session["state"] = new_state
        session["startedAt"] = datetime.now(timezone.utc).isoformat()
        await pool.hset(sess_key, mapping={
            "state": new_state, "startedAt": session["startedAt"]})
        log.info("Live session %s started by %s", session_id, body.trainerEmail)
    roster = await pool.smembers(roster_key)
    joined = await pool.hgetall(joined_key)
    return live_sessions.shape_detail(session_id, session, roster, joined, body.trainerEmail)


@app.post("/api/live/sessions/{session_id}/end")
async def api_live_session_end(session_id: str, body: LiveSessionTrainerAction, request: Request):
    """Trainer ends the session: state → ended, freezes the board, and sets a
    7-day TTL on the session keys (index entry kept; listing tolerates it).
    Auth: service bearer or signed-in writer."""
    await _require_service_or_writer(request)
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not live_sessions.is_trainer(body.trainerEmail, session):
        raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    try:
        new_state, changed = live_sessions.apply_transition(session.get("state", ""), "end")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if changed:
        session["state"] = new_state
        session["endedAt"] = datetime.now(timezone.utc).isoformat()
        await pool.hset(sess_key, mapping={
            "state": new_state, "endedAt": session["endedAt"]})
        await _store_pad_export(session_id, session)
        log.info("Live session %s ended by %s", session_id, body.trainerEmail)
    await _expire_live_session_keys(session_id, session)
    roster = await pool.smembers(roster_key)
    joined = await pool.hgetall(joined_key)
    return live_sessions.shape_detail(session_id, session, roster, joined, body.trainerEmail)


@app.post("/api/live/sessions/{session_id}/cancel")
async def api_live_session_cancel(session_id: str, body: LiveSessionTrainerAction, request: Request):
    """Trainer cancels a scheduled/open workshop: state → cancelled, sets
    cancelledAt, freezes the pad into the export snapshot, and applies the
    same 7-day TTL as ended (entity + index kept so learners see it was
    cancelled rather than a vanished session). Idempotent; 409 once running
    or ended; 403 on trainerEmail mismatch. Auth: service bearer or
    signed-in writer."""
    await _require_service_or_writer(request)
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not live_sessions.is_trainer(body.trainerEmail, session):
        raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    try:
        new_state, changed = live_sessions.apply_transition(session.get("state", ""), "cancel")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if changed:
        session["state"] = new_state
        session["cancelledAt"] = datetime.now(timezone.utc).isoformat()
        await pool.hset(sess_key, mapping={
            "state": new_state, "cancelledAt": session["cancelledAt"]})
        await _store_pad_export(session_id, session)
        log.info("Live session %s cancelled by %s", session_id, body.trainerEmail)
    await _expire_live_session_keys(session_id, session)
    roster = await pool.smembers(roster_key)
    joined = await pool.hgetall(joined_key)
    return live_sessions.shape_detail(session_id, session, roster, joined, body.trainerEmail)


@app.post("/api/live/sessions/{session_id}/open-registration")
async def api_live_session_open_registration(session_id: str, body: LiveSessionTrainerAction, request: Request):
    """Trainer opens registration on a scheduled workshop: scheduled → open.
    Idempotent when already open; 409 once running/ended/cancelled; 403 on
    trainerEmail mismatch. Auth: service bearer or signed-in writer."""
    await _require_service_or_writer(request)
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not live_sessions.is_trainer(body.trainerEmail, session):
        raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    try:
        new_state, changed = live_sessions.apply_transition(session.get("state", ""), "open-registration")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if changed:
        session["state"] = new_state
        await pool.hset(sess_key, "state", new_state)
        log.info("Live session %s registration opened by %s", session_id, body.trainerEmail)
    roster = await pool.smembers(roster_key)
    joined = await pool.hgetall(joined_key)
    return live_sessions.shape_detail(session_id, session, roster, joined, body.trainerEmail)


@app.post("/api/live/sessions/join-by-code")
async def api_live_session_join_by_code(body: LiveSessionJoinByCode, request: Request):
    """Join a workshop by its 6-char code (case-insensitive) — appends the
    email to the roster (self-registration), unlike /join which requires an
    invite. 404 unknown code, 409 when full (maxSeats) or ended/cancelled.
    Returns the session summary (learner view — no joinCode echo).

    Auth: service bearer or signed-in writer — learners always join through
    the app's authed proxy, so an anonymous caller has no business here
    (prevents code-guessing roster injection + email harvesting)."""
    await _require_service_or_writer(request)
    code = live_sessions.normalize_join_code(body.code)
    if not code:
        raise HTTPException(status_code=400, detail="a join code is required")
    email = live_sessions.normalize_email(body.email)
    if not live_sessions.is_valid_email(email):
        raise HTTPException(status_code=400, detail="a valid email is required")
    session_id = await pool.get(f"live:joincode:{code}")
    if not session_id:
        raise HTTPException(status_code=404, detail="Unknown join code")
    sess_key, roster_key, joined_key = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    roster = await pool.smembers(roster_key)
    err = live_sessions.join_by_code_error(
        session.get("state", ""), email, roster,
        int(session.get("maxSeats") or 0))
    if err:
        raise HTTPException(status_code=err[0], detail=err[1])
    await pool.sadd(roster_key, email)
    await pool.hsetnx(joined_key, email, datetime.now(timezone.utc).isoformat())
    roster = await pool.smembers(roster_key)
    joined = await pool.hgetall(joined_key)
    return {"sessionId": session_id,
            **live_sessions.shape_summary(session_id, session, roster, joined, email)}


@app.post("/api/live/sessions/{session_id}/provision-all")
async def api_live_session_provision_all(session_id: str, body: LiveSessionProvisionAll, request: Request):
    """Trainer pre-provisions a training environment for every roster email.

    Reuses the arena provision path verbatim (including its one-active-
    session-per-user+tenant+training dedupe): already-active learners are
    reported as "already-active", everyone else gets a daemon job queued
    exactly as POST /api/arena/provision would. tenant = the trainer's arena
    tenant URL; the session's trainingId/ref drive repo + branch.
    Auth: service bearer or signed-in writer."""
    await _require_service_or_writer(request)
    sess_key, roster_key, _ = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not live_sessions.is_trainer(body.trainerEmail, session):
        raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    roster = await pool.smembers(roster_key)
    wanted = {e.strip().lower() for e in body.emails if e.strip()} if body.emails else None
    results = []
    for email in sorted(roster):
        if wanted is not None and email.lower() not in wanted:
            continue
        per = body.perUser.get(email) or body.perUser.get(email.lower()) or {}
        try:
            # Pass the trainer's (already-authenticated) request through the
            # arena gate — provision-all itself is service/writer-gated above.
            provisioned = await api_arena_provision(ArenaProvisionRequest(
                trainingId=session.get("trainingId", ""),
                userId=email,
                tenantUrl=(body.tenant or "").rstrip("/"),
                ref=session.get("ref", ""),
                dtEnv=per.get("dtEnv") or {},
                dtTokenIds=per.get("dtTokenIds") or [],
            ), request)
            results.append({
                "email":  email,
                "status": "already-active" if provisioned.get("deduped") else "queued",
                "jobId":  provisioned.get("jobId", ""),
            })
        except HTTPException as exc:
            results.append({"email": email, "status": "error",
                            "error": str(exc.detail)[:200]})
        except Exception as exc:
            results.append({"email": email, "status": "error",
                            "error": str(exc)[:200]})
    log.info("Live session %s provision-all by %s: %d queued, %d active, %d errors",
             session_id, body.trainerEmail,
             sum(1 for r in results if r["status"] == "queued"),
             sum(1 for r in results if r["status"] == "already-active"),
             sum(1 for r in results if r["status"] == "error"))
    return {"results": results}


@app.get("/api/live/sessions/{session_id}/readiness")
async def api_live_session_readiness(session_id: str, request: Request, trainerEmail: str = ""):
    """Trainer's per-learner provisioning board: for each roster email the
    state of their environment for THIS training — none | queued |
    provisioning | ready | failed. "ready" is the same "Daemon ready"
    livelog contract as the arena session-status endpoint; "failed" only
    when a failed terminal record newer than the session exists (be honest
    — no invented states)."""
    sess_key, roster_key, _ = _live_keys(session_id)
    session = await pool.hgetall(sess_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if not live_sessions.is_trainer(trainerEmail, session):
        raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    roster = await pool.smembers(roster_key)
    training_id = session.get("trainingId", "")

    # One scan of the running arena jobs → email -> meta for this training.
    running_by_email: dict[str, dict] = {}
    cursor = 0
    while True:
        cursor, keys = await pool.scan(cursor, match="job:running:enablement-*", count=200)
        for key in keys:
            meta = await pool.hgetall(key)
            if not meta or meta.get("terminating"):
                continue
            user = live_sessions.normalize_email(meta.get("arena_user"))
            if meta.get("training_id") == training_id and user in roster:
                running_by_email[user] = meta
        if cursor == 0:
            break

    # Failed records: recent jobs:completed entries matched by (email,
    # training, finished after the session was created).
    failed_by_email: dict[str, str] = {}
    for raw in await pool.lrange("jobs:completed", -500, -1):
        try:
            record = json.loads(raw)
        except Exception:
            continue
        user = live_sessions.failed_job_email(
            record, roster, training_id, since=session.get("createdAt", ""))
        if user and user not in running_by_email:
            failed_by_email[user] = record.get("job_id", "")

    results = []
    for email in sorted(roster):
        meta = running_by_email.get(email)
        if meta:
            job_id = meta.get("job_id", "")
            livelog = await pool.get(f"job:livelog:{job_id}") if job_id else ""
            results.append({"email": email,
                            "state": live_sessions.readiness_state(meta, livelog),
                            "jobId": job_id})
        elif email in failed_by_email:
            results.append({"email": email, "state": "failed",
                            "jobId": failed_by_email[email]})
        else:
            results.append({"email": email, "state": "none"})
    payload = {"results": results}
    # trainerEmail is caller-supplied — anonymous callers who know it must
    # not harvest the roster; they get masked emails (states stay visible).
    return payload if _has_full_access(request) else masking.mask_readiness(payload)


@app.get("/api/live/capacity")
async def api_live_capacity(needed: int = 0):
    """Read-only fleet headroom check for a workshop of `needed` learners —
    worker heartbeat capacities minus the running-job count per worker. An
    approximation (heartbeat hashes, no cluster-wide slot ledger) but honest
    enough for a fail-loud pre-flight."""
    workers = await _fleet_workers()
    active_counts: dict[str, int] = {}
    cursor = 0
    while True:
        cursor, keys = await pool.scan(cursor, match="job:running:*", count=200)
        for key in keys:
            try:
                meta = await pool.hgetall(key)
            except Exception:
                continue
            wid = meta.get("worker_id", "")
            if wid:
                active_counts[wid] = active_counts.get(wid, 0) + 1
        if cursor == 0:
            break
    return live_sessions.capacity_summary(workers, active_counts, needed)


# ── Structured workshop pad (EPIC-002) ────────────────────────────────────────
# Shared notes (welcome/solutions markdown) + Q&A for a live session, stored
# in Redis streams (NOT pub/sub) so late joiners and the export replay the
# full history. Browser access is via /pad/{id} — a self-contained page that
# claims a single-use pad token (shell-token pattern) for an 8h pad session;
# live updates are SSE (nginx: proxy_buffering off on /api/live/*stream).
# All decision logic is in dashboard/live_pad.py (pure, unit-tested).


def _pad_keys(session_id: str) -> tuple[str, str, str]:
    """The three Redis keys of a session pad: (sections hash, qa stream,
    export string)."""
    base = f"live:pad:{session_id}"
    return f"{base}:sections", f"{base}:qa", f"{base}:export"


async def _expire_live_session_keys(session_id: str, session: dict):
    """Apply the ended/cancelled 7-day TTL to every key of a session — the
    session hash/roster/joined, the join-code pointer, and the raw pad keys
    (the pad export carries its own 30-day TTL). expire on a missing key is
    a no-op, so this is safe for sessions without a pad or join code."""
    sections_key, qa_key, _ = _pad_keys(session_id)
    keys = [*_live_keys(session_id), sections_key, qa_key]
    if session.get("joinCode"):
        keys.append(f"live:joincode:{session['joinCode']}")
    for key in keys:
        await pool.expire(key, live_sessions.SESSION_TTL_SECONDS)


async def _store_pad_export(session_id: str, session: dict):
    """Freeze the pad into a standalone HTML snapshot (live:pad:{id}:export,
    30-day TTL) — called on the end/cancel transition."""
    sections_key, qa_key, export_key = _pad_keys(session_id)
    sections = await pool.hgetall(sections_key)
    entries = await pool.xrange(qa_key)
    html_doc = live_pad.render_export(
        session, sections, live_pad.assemble_qa(entries))
    await pool.set(export_key, html_doc, ex=live_pad.EXPORT_TTL_SECONDS)


async def _require_live_session(session_id: str) -> dict:
    session = await pool.hgetall(f"live:session:{session_id}")
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return session


def _pad_frozen_guard(session: dict):
    """Pad writes stop once the session is ended/cancelled — the export
    snapshot has already been frozen and would silently drop them."""
    if session.get("state") in ("ended", "cancelled"):
        raise HTTPException(status_code=409,
                            detail=f"session is {session.get('state')} — the pad is read-only")


async def _pad_add_question(session_id: str, session: dict, email: str,
                            name: str, text: str) -> dict:
    _pad_frozen_guard(session)
    try:
        text = live_pad.clean_text(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _, qa_key, _ = _pad_keys(session_id)
    ts = datetime.now(timezone.utc).isoformat()
    entry_id = await pool.xadd(qa_key, {
        "type": "question", "qid": "", "email": email, "name": name,
        "text": text, "ts": ts})
    return {"qid": entry_id, "ts": ts}


async def _pad_add_answer(session_id: str, session: dict, email: str,
                          name: str, qid: str, text: str) -> dict:
    _pad_frozen_guard(session)
    try:
        text = live_pad.clean_text(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _, qa_key, _ = _pad_keys(session_id)
    question = await pool.xrange(qa_key, min=qid, max=qid) if qid else []
    if not question or question[0][1].get("type") != "question":
        raise HTTPException(status_code=404, detail="question not found")
    ts = datetime.now(timezone.utc).isoformat()
    await pool.xadd(qa_key, {
        "type": "answer", "qid": qid, "email": email, "name": name,
        "text": text, "ts": ts})
    return {"qid": qid, "ts": ts}


async def _pad_set_section(session_id: str, session: dict, email: str,
                           key: str, markdown: str) -> dict:
    _pad_frozen_guard(session)
    err = live_pad.section_error(key, email, session)
    if err:
        raise HTTPException(status_code=err[0], detail=err[1])
    sections_key, _, _ = _pad_keys(session_id)
    await pool.hset(sections_key, key, markdown or "")
    return {"key": key, "saved": True}


async def _pad_event_stream(session_id: str):
    """SSE generator: one full snapshot event, then incremental qa events via
    blocking XREAD (15 s block, keepalive comment between). Section edits
    have no stream — change detection piggybacks on each wake (≤15 s lag)."""
    sections_key, qa_key, _ = _pad_keys(session_id)
    sections = await pool.hgetall(sections_key)
    entries = await pool.xrange(qa_key)
    last_id = entries[-1][0] if entries else "0-0"
    yield ("event: snapshot\ndata: "
           + json.dumps(live_pad.shape_pad(sections, entries)) + "\n\n")
    known = {k: sections.get(k, "") for k in live_pad.SECTION_KEYS}
    while True:
        result = await pool.xread({qa_key: last_id}, block=15000, count=100)
        if result:
            for _stream, new_entries in result:
                for entry_id, fields in new_entries:
                    last_id = entry_id
                    event = dict(fields)
                    if event.get("type") == "question":
                        event["qid"] = entry_id
                    yield "event: qa\ndata: " + json.dumps(event) + "\n\n"
        else:
            yield ": keepalive\n\n"
        sections = await pool.hgetall(sections_key)
        current = {k: sections.get(k, "") for k in live_pad.SECTION_KEYS}
        if current != known:
            known = current
            yield "event: sections\ndata: " + json.dumps(current) + "\n\n"


def _sse_response(generator) -> StreamingResponse:
    return StreamingResponse(generator, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class LivePadSection(BaseModel):
    trainerEmail: str = ""
    key: str = ""
    markdown: str = ""


class LivePadQuestion(BaseModel):
    email: str = ""
    name: str = ""
    text: str = ""


class LivePadAnswer(BaseModel):
    trainerEmail: str = ""
    name: str = ""
    qid: str = ""
    text: str = ""


class LivePadTokenRequest(BaseModel):
    email: str = ""
    name: str = ""
    role: str = ""                # "trainer" | "learner"


class LivePadClaim(BaseModel):
    token: str = ""


class LivePadSessionBody(BaseModel):
    """padSession-authed writes from the /pad/{id} page — identity comes from
    the claimed pad-session record server-side, never from client fields."""
    padSession: str = ""
    key: str = ""
    markdown: str = ""
    qid: str = ""
    text: str = ""


async def _resolve_pad_session(pad_session: str) -> dict:
    raw = await pool.get(f"live:padsession:{pad_session}") if pad_session else None
    if not raw:
        raise HTTPException(status_code=401, detail="invalid or expired pad session")
    return json.loads(raw)


@app.get("/api/live/sessions/{session_id}/pad")
async def api_live_pad_get(session_id: str, request: Request):
    """Full pad state: the two structured sections + Q&A with answers
    assembled under their questions. Poll-friendly (the in-app tab reads
    this via the orbital proxy); the popup page uses the SSE stream.
    Anonymous callers get question authors' emails masked."""
    await _require_live_session(session_id)
    sections_key, qa_key, _ = _pad_keys(session_id)
    sections = await pool.hgetall(sections_key)
    entries = await pool.xrange(qa_key)
    pad = live_pad.shape_pad(sections, entries)
    return pad if _has_full_access(request) else masking.mask_pad(pad)


@app.post("/api/live/sessions/{session_id}/pad/section")
async def api_live_pad_section(session_id: str, body: LivePadSection):
    """Trainer sets a structured section (welcome|solutions markdown)."""
    session = await _require_live_session(session_id)
    return await _pad_set_section(session_id, session,
                                  body.trainerEmail, body.key, body.markdown)


@app.post("/api/live/sessions/{session_id}/pad/question")
async def api_live_pad_question(session_id: str, body: LivePadQuestion):
    """Anyone in the session asks a question (max 2000 chars, HTML
    stripped). The entry's stream id becomes its qid."""
    session = await _require_live_session(session_id)
    email = live_sessions.normalize_email(body.email)
    if not live_sessions.is_valid_email(email):
        raise HTTPException(status_code=400, detail="a valid email is required")
    return await _pad_add_question(session_id, session, email,
                                   (body.name or "").strip(), body.text)


@app.post("/api/live/sessions/{session_id}/pad/answer")
async def api_live_pad_answer(session_id: str, body: LivePadAnswer):
    """Trainer answers a question (qid = the question's stream id)."""
    session = await _require_live_session(session_id)
    if not live_sessions.is_trainer(body.trainerEmail, session):
        raise HTTPException(status_code=403, detail="only the trainer can answer questions")
    return await _pad_add_answer(session_id, session,
                                 live_sessions.normalize_email(body.trainerEmail),
                                 (body.name or "").strip(), body.qid, body.text)


@app.get("/api/live/sessions/{session_id}/pad/stream")
async def api_live_pad_stream(session_id: str, request: Request):
    """SSE pad feed (snapshot + incremental) — session-scoped variant.

    Auth: service bearer or signed-in writer. The popup page uses the
    padSession-authed /api/live/pad/stream instead; this variant has no
    per-user credential, so anonymous access would leak Q&A emails."""
    await _require_service_or_writer(request)
    await _require_live_session(session_id)
    return _sse_response(_pad_event_stream(session_id))


@app.get("/api/live/sessions/{session_id}/pad/export")
async def api_live_pad_export(session_id: str, email: str = ""):
    """The frozen pad snapshot (standalone HTML) — available to the trainer
    and any roster member once the session ended or was cancelled."""
    session = await _require_live_session(session_id)
    roster = await pool.smembers(f"live:session:{session_id}:roster")
    if not (live_sessions.is_trainer(email, session)
            or live_sessions.on_roster(email, roster)):
        raise HTTPException(status_code=403, detail="email is not on the session roster")
    _, _, export_key = _pad_keys(session_id)
    html_doc = await pool.get(export_key)
    if not html_doc:
        raise HTTPException(status_code=404,
                            detail="no export snapshot yet (session has not ended or been cancelled)")
    return HTMLResponse(html_doc)


@app.post("/api/live/sessions/{session_id}/pad-token")
async def api_live_pad_token(session_id: str, body: LivePadTokenRequest, request: Request):
    """Issue a single-use 60-second pad handoff token (shell-token pattern).

    Called via the app's authed orbital proxy; the token rides the
    /pad/{id}?token=… URL and is claimed exactly once by the page, which
    mints the 8h pad session. Trainer role requires the trainerEmail match;
    learners must be on the roster. Auth: service bearer or signed-in
    writer (an anonymous caller supplying a rostered email must not be able
    to mint pad identities)."""
    await _require_service_or_writer(request)
    session = await _require_live_session(session_id)
    try:
        role = live_pad.validate_role(body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    email = live_sessions.normalize_email(body.email)
    if not live_sessions.is_valid_email(email):
        raise HTTPException(status_code=400, detail="a valid email is required")
    if role == "trainer":
        if not live_sessions.is_trainer(email, session):
            raise HTTPException(status_code=403, detail="trainerEmail does not match this session's trainer")
    else:
        roster = await pool.smembers(f"live:session:{session_id}:roster")
        if not (live_sessions.on_roster(email, roster)
                or live_sessions.is_trainer(email, session)):
            raise HTTPException(status_code=403, detail="email is not on the session roster")
    token = secrets.token_hex(16)
    await pool.set(f"live:padtoken:{token}", json.dumps({
        "sessionId": session_id, "email": email,
        "name": (body.name or "").strip(), "role": role,
    }), ex=live_pad.PAD_TOKEN_TTL_SECONDS)
    return {"token": token,
            "padUrl": f"https://autonomous-enablements.whydevslovedynatrace.com/pad/{session_id}?token={token}"}


@app.post("/api/live/pad-claim")
async def api_live_pad_claim(body: LivePadClaim):
    """Claim a single-use pad token → mint the multi-use pad session (8h).

    Same atomic GET+DEL as the shell-token WebSocket validation."""
    pipe = pool.pipeline(transaction=True)
    pipe.get(f"live:padtoken:{body.token}")
    pipe.delete(f"live:padtoken:{body.token}")
    raw, _ = await pipe.execute()
    if not raw:
        raise HTTPException(status_code=401, detail="invalid or expired pad token")
    identity = json.loads(raw)
    pad_session = secrets.token_hex(16)
    await pool.set(f"live:padsession:{pad_session}", raw,
                   ex=live_pad.PAD_SESSION_TTL_SECONDS)
    session = await pool.hgetall(f"live:session:{identity['sessionId']}") or {}
    return {"padSession": pad_session, **identity,
            "title": session.get("title", ""), "state": session.get("state", "")}


@app.post("/api/live/pad/section")
async def api_live_pad_section_ps(body: LivePadSessionBody):
    """padSession-authed section write (identity from the pad session)."""
    identity = await _resolve_pad_session(body.padSession)
    if identity.get("role") != "trainer":
        raise HTTPException(status_code=403, detail="only the trainer can edit pad sections")
    session = await _require_live_session(identity["sessionId"])
    return await _pad_set_section(identity["sessionId"], session,
                                  identity.get("email", ""), body.key, body.markdown)


@app.post("/api/live/pad/question")
async def api_live_pad_question_ps(body: LivePadSessionBody):
    """padSession-authed question (identity from the pad session)."""
    identity = await _resolve_pad_session(body.padSession)
    session = await _require_live_session(identity["sessionId"])
    return await _pad_add_question(identity["sessionId"], session,
                                   identity.get("email", ""),
                                   identity.get("name", ""), body.text)


@app.post("/api/live/pad/answer")
async def api_live_pad_answer_ps(body: LivePadSessionBody):
    """padSession-authed answer (trainer pad sessions only)."""
    identity = await _resolve_pad_session(body.padSession)
    if identity.get("role") != "trainer":
        raise HTTPException(status_code=403, detail="only the trainer can answer questions")
    session = await _require_live_session(identity["sessionId"])
    return await _pad_add_answer(identity["sessionId"], session,
                                 identity.get("email", ""),
                                 identity.get("name", ""), body.qid, body.text)


@app.get("/api/live/pad/stream")
async def api_live_pad_stream_ps(padSession: str = ""):
    """padSession-authed SSE pad feed (EventSource can't set headers, so the
    pad session rides the query string)."""
    identity = await _resolve_pad_session(padSession)
    await _require_live_session(identity["sessionId"])
    return _sse_response(_pad_event_stream(identity["sessionId"]))


# Self-contained pad page (the /shell/{job_id} pattern: inline CSS/JS, no
# external deps). Plain string + placeholder substitution — NOT an f-string —
# so the CSS/JS braces stay readable. __SESSION_ID__/__BASE__ are JSON-encoded
# on the way in.
_PAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workshop Pad</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; background: #1a1a2e; color: #d4d4d4;
  font-family: -apple-system, 'Segoe UI', sans-serif; }
body { display: flex; flex-direction: column; }
#topbar { background: #16213e; color: #a0aec0; font-size: 12px; line-height: 38px;
  padding: 0 16px; display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0; border-bottom: 1px solid #0d3460; gap: 16px; }
#brand { display: flex; align-items: center; gap: 8px; }
#brand-logo { color: #00b4d8; font-size: 18px; }
#brand-name { color: #e2e8f0; font-weight: 600; font-size: 13px; letter-spacing: .3px; }
#who { font-size: 11px; color: #718096; white-space: nowrap; }
#main { flex: 1; overflow-y: auto; padding: 20px; max-width: 860px; width: 100%;
  margin: 0 auto; }
.card { background: #16213e; border: 1px solid #0d3460; border-radius: 6px;
  padding: 14px 16px; margin-bottom: 14px; }
.card h3 { color: #00b4d8; font-size: 13px; margin-bottom: 10px;
  text-transform: uppercase; letter-spacing: .5px; }
.md { font-size: 13px; line-height: 1.55; color: #c9d1d9; word-break: break-word; }
.md h1, .md h2, .md h3 { color: #e2e8f0; margin: 10px 0 6px; }
.md h1 { font-size: 16px; } .md h2 { font-size: 14px; } .md h3 { font-size: 13px; }
.md code { background: #0d1117; border: 1px solid #0d3460; border-radius: 3px;
  padding: 1px 5px; font: 12px ui-monospace, Menlo, monospace; color: #79c0ff; }
.md pre { background: #0d1117; border: 1px solid #0d3460; border-radius: 4px;
  padding: 10px; overflow-x: auto; margin: 8px 0; }
.md pre code { background: none; border: 0; padding: 0; }
.md a { color: #58a6ff; }
.empty { color: #718096; font-size: 12px; font-style: italic; }
textarea { width: 100%; background: #0d1117; color: #e6edf3;
  border: 1px solid #0d3460; border-radius: 4px; padding: 8px;
  font: 12px ui-monospace, Menlo, monospace; resize: vertical; min-height: 70px; }
textarea:focus, input:focus { outline: 1px solid #00b4d8; }
button { background: #0e639c; color: #fff; border: 0; border-radius: 4px;
  padding: 6px 14px; font-size: 12px; cursor: pointer; }
button:hover { background: #1177bb; }
button:disabled { opacity: .5; cursor: default; }
.row { display: flex; gap: 8px; margin-top: 8px; align-items: flex-start; }
.q { border-top: 1px solid #0d3460; padding: 10px 0; }
.q:first-child { border-top: 0; }
.q .who { color: #e2e8f0; font-size: 12px; font-weight: 600; }
.q .ts { color: #718096; font-size: 10px; font-weight: 400; margin-left: 6px; }
.q .text { font-size: 13px; margin-top: 4px; white-space: pre-wrap;
  word-break: break-word; }
.a { margin: 8px 0 0 18px; padding-left: 10px; border-left: 2px solid #00b4d8; }
.a .who { color: #00b4d8; }
#err { display: none; margin: 40px auto; max-width: 480px; text-align: center;
  color: #fc8181; font-size: 13px; line-height: 1.6; }
.saved { color: #48bb78; font-size: 11px; margin-left: 8px; }
</style>
</head>
<body>
<div id="topbar">
  <div id="brand">
    <span id="brand-logo">⬡</span>
    <span id="brand-name">Workshop Pad</span>
    <span id="title" style="color:#718096"></span>
  </div>
  <span id="who">Connecting…</span>
</div>
<div id="err"></div>
<div id="main" style="display:none">
  <div class="card" id="card-welcome">
    <h3>Welcome</h3>
    <div class="md" id="md-welcome"><span class="empty">Nothing here yet.</span></div>
    <div id="edit-welcome" style="display:none">
      <div class="row"><textarea id="ta-welcome" placeholder="Markdown…"></textarea></div>
      <div class="row"><button onclick="saveSection('welcome')">Save</button><span class="saved" id="ok-welcome"></span></div>
    </div>
  </div>
  <div class="card" id="card-solutions">
    <h3>Solutions</h3>
    <div class="md" id="md-solutions"><span class="empty">Nothing here yet.</span></div>
    <div id="edit-solutions" style="display:none">
      <div class="row"><textarea id="ta-solutions" placeholder="Markdown…"></textarea></div>
      <div class="row"><button onclick="saveSection('solutions')">Save</button><span class="saved" id="ok-solutions"></span></div>
    </div>
  </div>
  <div class="card">
    <h3>Questions &amp; Answers</h3>
    <div id="qa"><span class="empty">No questions yet — ask the first one below.</span></div>
    <div class="row">
      <textarea id="compose" maxlength="2000" placeholder="Ask a question…"></textarea>
      <button onclick="ask()">Ask</button>
    </div>
  </div>
</div>
<script>
(async () => {
  const SESSION_ID = __SESSION_ID__;
  const BASE = __BASE__;
  const store = 'padsession-' + SESSION_ID;
  const esc = s => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  // Tiny markdown renderer: escape first, then fences, headings, bold,
  // inline code, links, line breaks. Deliberately minimal.
  function md(src) {
    if (!src) return '<span class="empty">Nothing here yet.</span>';
    let out = esc(src);
    out = out.replace(/```([\\s\\S]*?)```/g, (m, c) => '<pre><code>' + c.replace(/^\\n/, '') + '</code></pre>');
    out = out.replace(/^### (.*)$/gm, '<h3>$1</h3>');
    out = out.replace(/^## (.*)$/gm, '<h2>$1</h2>');
    out = out.replace(/^# (.*)$/gm, '<h1>$1</h1>');
    out = out.replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>');
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return out.replace(/\\n/g, '<br>');
  }
  function fail(msg) {
    document.getElementById('err').style.display = 'block';
    document.getElementById('err').textContent = msg;
    document.getElementById('who').textContent = '';
  }

  // ── Claim the single-use token → 8h pad session (survives reloads via
  // sessionStorage; the URL token is dead after first use). ──
  let me = null;
  try { me = JSON.parse(sessionStorage.getItem(store) || 'null'); } catch (e) {}
  const urlToken = new URLSearchParams(location.search).get('token') || '';
  if (!me && urlToken) {
    try {
      const r = await fetch(BASE + '/api/live/pad-claim', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: urlToken})});
      if (r.ok) { me = await r.json(); sessionStorage.setItem(store, JSON.stringify(me)); }
    } catch (e) {}
  }
  if (!me) return fail('This pad link has expired — reopen the pad from the app to get a fresh one.');

  document.getElementById('main').style.display = 'block';
  document.getElementById('title').textContent = me.title ? '· ' + me.title : '';
  document.getElementById('who').textContent = (me.name || me.email) + ' (' + me.role + ')';
  const isTrainer = me.role === 'trainer';
  if (isTrainer) ['welcome', 'solutions'].forEach(k =>
    document.getElementById('edit-' + k).style.display = 'block');

  // ── State + rendering (SSE is the single source of truth — own posts
  // come back through the stream, no local echo). ──
  let S = {sections: {}, qa: []};
  function renderSections() {
    for (const k of ['welcome', 'solutions']) {
      document.getElementById('md-' + k).innerHTML = md(S.sections[k] || '');
      const ta = document.getElementById('ta-' + k);
      if (isTrainer && document.activeElement !== ta) ta.value = S.sections[k] || '';
    }
  }
  function renderQa() {
    const host = document.getElementById('qa');
    if (!S.qa.length) { host.innerHTML = '<span class="empty">No questions yet — ask the first one below.</span>'; return; }
    host.innerHTML = S.qa.map((q, i) => {
      let h = '<div class="q"><div class="who">' + esc(q.name || q.email) +
        '<span class="ts">' + esc((q.ts || '').replace('T', ' ').slice(0, 16)) + '</span></div>' +
        '<div class="text">' + esc(q.text) + '</div>';
      for (const a of q.answers) h += '<div class="a"><div class="who">' + esc(a.name || 'Trainer') +
        '<span class="ts">' + esc((a.ts || '').replace('T', ' ').slice(0, 16)) + '</span></div>' +
        '<div class="text">' + esc(a.text) + '</div></div>';
      if (isTrainer) h += '<div class="row"><textarea id="ans-' + i + '" style="min-height:40px" placeholder="Answer…"></textarea>' +
        '<button onclick="answer(' + i + ')">Reply</button></div>';
      return h + '</div>';
    }).join('');
  }

  async function post(path, body) {
    const r = await fetch(BASE + path, {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({padSession: me.padSession}, body))});
    if (!r.ok) alert('Request failed (' + r.status + '): ' + await r.text());
    return r.ok;
  }
  window.ask = async () => {
    const ta = document.getElementById('compose');
    if (ta.value.trim() && await post('/api/live/pad/question', {text: ta.value})) ta.value = '';
  };
  window.answer = async (i) => {
    const ta = document.getElementById('ans-' + i);
    if (ta.value.trim()) await post('/api/live/pad/answer', {qid: S.qa[i].qid, text: ta.value});
  };
  window.saveSection = async (k) => {
    if (await post('/api/live/pad/section', {key: k, markdown: document.getElementById('ta-' + k).value})) {
      const ok = document.getElementById('ok-' + k);
      ok.textContent = 'saved'; setTimeout(() => ok.textContent = '', 1500);
    }
  };

  // ── Live updates (SSE; EventSource reconnects on its own) ──
  const es = new EventSource(BASE + '/api/live/pad/stream?padSession=' + encodeURIComponent(me.padSession));
  es.addEventListener('snapshot', e => {
    S = JSON.parse(e.data); renderSections(); renderQa();
  });
  es.addEventListener('sections', e => {
    S.sections = JSON.parse(e.data); renderSections();
  });
  es.addEventListener('qa', e => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'question') {
      if (!S.qa.some(q => q.qid === ev.qid))
        S.qa.push({qid: ev.qid, name: ev.name, email: ev.email, text: ev.text, ts: ev.ts, answers: []});
    } else if (ev.type === 'answer') {
      const q = S.qa.find(q => q.qid === ev.qid);
      if (q) q.answers.push({name: ev.name, text: ev.text, ts: ev.ts});
    }
    renderQa();
  });
  es.onerror = () => { document.getElementById('who').textContent = (me.name || me.email) + ' (reconnecting…)'; };
  es.onopen = () => { document.getElementById('who').textContent = (me.name || me.email) + ' (' + me.role + ')'; };
})();
</script>
</body>
</html>"""


@app.get("/pad/{session_id}", response_class=HTMLResponse)
async def live_pad_page(session_id: str, token: str = ""):
    """Standalone workshop pad page (Welcome/Solutions + Q&A, live via SSE).

    Same handoff as /shell/{job_id}: opened from the app with a single-use
    ?token= (minted by the authed pad-token endpoint), which the page claims
    for an 8h pad session. Self-contained — inline CSS/JS, no external deps.
    """
    base_url = "https://autonomous-enablements.whydevslovedynatrace.com"
    return HTMLResponse(_PAD_PAGE_HTML
                        .replace("__SESSION_ID__", json.dumps(session_id))
                        .replace("__BASE__", json.dumps(base_url)))


# ── Framework test suites ─────────────────────────────────────────────────────

FRAMEWORK_SUITES = [
    {"id": "bats",      "name": "Unit Tests (bats)",         "description": "Shell unit tests — static, no cluster needed", "arch": "arm64", "needs_creds": False, "test_script": "cd .devcontainer && bats test/unit/"},
    {"id": "engines",   "name": "Engine Tests",              "description": "k3d + Kind (AMD64; Kind skipped on Orbital)",   "arch": "amd64", "needs_creds": False, "test_script": "bash .devcontainer/test/integration_engines.sh"},
    {"id": "k3d-apps",  "name": "K3d App Exposure",          "description": "All demo apps deployed + exposed via ingress",  "arch": "amd64", "needs_creds": False, "test_script": "bash .devcontainer/test/integration_k3d_apps.sh"},
    {"id": "dt-apponly","name": "DT Application Monitoring", "description": "Operator + ActiveGate + CSI code injection + todo-app (K3d, AMD64+ARM64)",          "arch": "both", "needs_creds": True,  "test_script": "bash .devcontainer/test/integration_appmon_k3d_todoapp.sh"},
    {"id": "dt-cnfs",   "name": "DT CloudNative FullStack",  "description": "CNFS dynakube + K3d — OneAgent crash expected on container nodes (AMD64+ARM64)", "arch": "both", "needs_creds": True,  "requires_native": True, "test_script": "bash .devcontainer/test/integration_cnfs_k3d_todoapp.sh"},
    {"id": "k3d-aitraveladvisor", "name": "AI Travel Advisor", "description": "Ollama + Weaviate + app on K3d — verifies local-path PVC and ingress (AMD64, needs DT_LLM_TOKEN)", "arch": "amd64", "needs_creds": True, "test_script": "bash .devcontainer/test/integration_k3d_aitraveladvisor.sh"},
    {"id": "dtwiz",     "name": "dtwiz (platform-token)",     "description": "dtwiz CLI install → status → analyze → install kubernetes on K3d (operator via platform token; the dtwiz-101 bootcamp path). Needs DT_PLATFORM_TOKEN.", "arch": "amd64", "needs_creds": True, "requires_native": True, "test_script": "bash .devcontainer/test/integration_dtwiz_k3d.sh"},
]

@app.get("/api/framework/suites")
async def api_framework_suites():
    """Return framework test suite catalog with last run result from Redis."""
    results = []
    for suite in FRAMEWORK_SUITES:
        last = await pool.hgetall(f"framework:suite:{suite['id']}:last")
        results.append({**suite, "last": last or None})
    return {"suites": results}

@app.post("/api/framework/trigger")
async def api_framework_trigger(request: Request):
    """Trigger one or all framework test suites. Writer-only."""
    role = await _require_writer(request)
    body = await request.json()
    suite_id = body.get("suite", "all")   # suite id or "all"
    ref      = body.get("ref", "main")
    arch     = body.get("arch", "amd64")

    suites_to_run = FRAMEWORK_SUITES if suite_id == "all" else [s for s in FRAMEWORK_SUITES if s["id"] == suite_id]
    suites_to_run = [s for s in suites_to_run if s.get("status") != "coming_soon"]

    queued = []
    timestamp = datetime.now(timezone.utc).isoformat()
    for s in suites_to_run:
        target_arch = s["arch"] if suite_id == "all" else arch
        job = {
            "type": "framework-test",
            "suite": s["id"],
            "test_script": s["test_script"],   # executor uses this instead of integration.sh
            "framework_suite": s["id"],         # signals executor to save suite result
            "repo": "dynatrace-wwse/codespaces-framework",
            "arch": target_arch,
            "queue": f"test:{target_arch}",
            "ref": ref,
            "timestamp": timestamp,
            "trigger": "framework",
            "nightly_run_id": f"framework-{int(datetime.now(timezone.utc).timestamp())}",
            "requested_by": role["user"],
        }
        await pool.rpush(f"queue:test:{target_arch}", json.dumps(job))
        queued.append({"suite": s["id"], "arch": target_arch})

    return {"status": "queued", "ref": ref, "jobs": queued}

@app.get("/api/framework/runs")
async def api_framework_runs(limit: int = 20):
    """Recent framework test run results."""
    raw = await pool.lrange("framework:runs", 0, limit - 1)
    runs = []
    for r in raw:
        try:
            runs.append(json.loads(r))
        except Exception:
            pass
    return {"runs": runs}

# ── Nightly runs list ─────────────────────────────────────────────────────────

@app.get("/api/nightly/runs")
async def api_nightly_runs():
    """List all nightly run IDs with pass/fail summaries (newest first)."""
    all_runs = await _nightly_runs_map()
    runs: list[dict] = []
    for rid in sorted(all_runs.keys(), reverse=True):
        jobs = all_runs[rid]
        runs.append({
            "run_id": rid,
            "total": len(jobs),
            "passed": sum(1 for j in jobs if (j.get("result") or {}).get("passed")),
            "failed": sum(1 for j in jobs if not (j.get("result") or {}).get("passed")),
            "timestamp": jobs[0].get("timestamp", ""),
        })
    return {"runs": runs}

@app.get("/api/nightly/run/{run_id}")
async def api_nightly_run(run_id: str):
    """Get nightly results for a specific run_id (or 'latest')."""
    all_runs = await _nightly_runs_map()
    if not all_runs:
        return {"run_id": None, "results": []}

    target_id = sorted(all_runs.keys())[-1] if run_id == "latest" else run_id
    if not all_runs.get(target_id):
        raise HTTPException(404, f"No jobs found for nightly run {run_id}")

    return _nightly_run_payload(all_runs, target_id)


@app.get("/api/nightly/run/{run_id}/summary")
async def api_nightly_run_summary(run_id: str):
    """Extract common error patterns from failed jobs in a nightly run."""
    import re as _re

    all_runs = await _nightly_runs_map()

    if run_id == "latest":
        if not all_runs:
            return {"run_id": None, "patterns": []}
        target_id = sorted(all_runs.keys())[-1]
    else:
        target_id = run_id

    target_jobs = all_runs.get(target_id, [])
    failed_jobs = [j for j in target_jobs if not (j.get("result") or {}).get("passed")]

    if not failed_jobs:
        return {"run_id": target_id, "patterns": []}

    # Patterns that signal real failures (not noise)
    _ERROR_RE = _re.compile(
        r"(error|fail(ed)?|timeout|oomkill|exit code [1-9]|panic|exception|"
        r"cannot|could not|no such|permission denied|connection refused)",
        _re.IGNORECASE,
    )
    # Strip timestamps (e.g. "2026-05-25T10:01:23Z") and hex job IDs
    _STRIP_RE = _re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*|[a-f0-9]{12,}")

    counts: dict[str, int] = {}
    for job in failed_jobs:
        log_raw = await pool.get(f"job:log:{job.get('job_id', '')}")
        if not log_raw:
            continue
        for line in log_raw.splitlines():
            line = line.strip()
            if not line or len(line) < 10:
                continue
            if not _ERROR_RE.search(line):
                continue
            normalized = _STRIP_RE.sub("…", line)[:120].strip()
            if normalized:
                counts[normalized] = counts.get(normalized, 0) + 1

    top = sorted(counts.items(), key=lambda x: -x[1])[:12]
    return {
        "run_id": target_id,
        "failed_jobs": len(failed_jobs),
        "patterns": [{"line": k, "count": v} for k, v in top],
    }


@app.get("/api/health")
async def api_health():
    """Platform health overview. CORS open so external sites can poll status."""
    from fastapi.responses import JSONResponse as _JSONResponse
    _cors = {"Access-Control-Allow-Origin": "*"}
    try:
        await pool.ping()

        # Worker count (skip port-pool list keys)
        worker_count = 0
        async for key in pool.scan_iter("worker:*"):
            if not key.endswith(":app_ports_free"):
                worker_count += 1

        # Queue depths
        queues = {
            "test:arm64": await pool.llen("queue:test:arm64"),
            "test:amd64": await pool.llen("queue:test:amd64"),
            "agent": await pool.llen("queue:agent"),
            "sync": await pool.llen("queue:sync"),
        }

        return _JSONResponse(content={
            "status": "healthy",
            "redis": "connected",
            "workers": worker_count,
            "queues": queues,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, headers=_cors)
    except Exception as e:
        return _JSONResponse(content={"status": "unhealthy", "error": str(e)}, headers=_cors)


# ── Fleet autoscaler (EC2 spot workers) ──────────────────────────────────────
# AWS access via the aws CLI in dashboard/fleet.py — safety rules (4-instance
# cap, terminate-only-spot-workers, creds-expiry classification) live there
# as pure functions with tests in dashboard/test_fleet.py.

async def _fleet_workers() -> list[dict]:
    """Registered worker:{id} hashes (same scan as /api/workers)."""
    workers = []
    async for key in pool.scan_iter("worker:*"):
        # Skip port-pool lists (worker:<id>:app_ports_free) — Redis lists,
        # not hashes; hgetall would raise WRONGTYPE.
        if key.endswith(":app_ports_free"):
            continue
        try:
            data = await pool.hgetall(key)
        except Exception:
            continue
        if data:
            data["worker_id"] = key.replace("worker:", "")
            workers.append(data)
    return workers


@app.get("/api/fleet")
async def api_fleet():
    """EC2 fleet instances joined with registered worker agents.

    Returns ``{instances, workers}``. Each instance carries ``worker_id`` /
    ``agent_online`` when a registered worker's ``host`` matches the
    instance's private IP, so the UI can see which boxes have a live agent.
    """
    try:
        instances = await fleet.list_fleet()
    except fleet.FleetError as e:
        raise HTTPException(502, str(e))

    workers = await _fleet_workers()
    by_ip = {w.get("host"): w for w in workers if w.get("host")}
    for inst in instances:
        w = by_ip.get(inst.get("private_ip"))
        inst["worker_id"] = w["worker_id"] if w else None
        inst["agent_online"] = bool(w)
    return {"instances": instances, "workers": workers}


@app.post("/api/fleet/scale-up")
async def api_fleet_scale_up(request: Request):
    """Launch spot workers from the golden AMI.

    Body: ``{count: int, instanceType?: str}`` — hard cap of 4 per call.
    """
    role = await _require_writer(request)
    body = await request.json()
    try:
        count = int(body.get("count") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "count must be an integer")
    instance_type = (body.get("instanceType")
                     or fleet.DEFAULT_INSTANCE_TYPE).strip()

    try:
        launched = await fleet.scale_up(count, instance_type)
    except ValueError as e:          # bad count / over the safety cap
        raise HTTPException(400, str(e))
    except fleet.FleetError as e:    # AWS failure (incl. expired creds)
        raise HTTPException(502, str(e))

    log.info("fleet scale-up by %s: %d × %s → %s",
             role["user"], count, instance_type,
             [i["instance_id"] for i in launched])
    return {
        "status": "launched",
        "count": count,
        "instance_type": instance_type,
        "instances": launched,
        "requested_by": role["user"],
    }


@app.post("/api/fleet/scale-down")
async def api_fleet_scale_down(request: Request):
    """Terminate spot workers (tag-verified in fleet.scale_down).

    Body: ``{instanceIds: [...], force?: bool}``. Each matched registered
    worker (by private_ip == worker ``host``) is marked ``draining=1`` on
    its ``worker:{id}`` hash first; instances whose matched worker still has
    ``active_jobs > 0`` are refused unless ``force`` is true.
    """
    role = await _require_writer(request)
    body = await request.json()
    instance_ids = body.get("instanceIds") or []
    force = bool(body.get("force"))
    if not instance_ids or not isinstance(instance_ids, list):
        raise HTTPException(400, "instanceIds (non-empty list) is required")

    # Map instance → registered worker (best-effort, by private IP).
    try:
        instances = await fleet.list_fleet()
    except fleet.FleetError as e:
        raise HTTPException(502, str(e))
    ip_by_id = {i["instance_id"]: i.get("private_ip") for i in instances}
    workers_by_ip = {w.get("host"): w for w in await _fleet_workers()
                     if w.get("host")}

    busy, draining = [], []
    for iid in instance_ids:
        worker = workers_by_ip.get(ip_by_id.get(iid))
        if not worker:
            continue
        active = int(worker.get("active_jobs") or 0)
        if active > 0 and not force:
            busy.append({"instance_id": iid,
                         "worker_id": worker["worker_id"],
                         "active_jobs": active})
            continue
        await pool.hset(f"worker:{worker['worker_id']}", "draining", "1")
        draining.append(worker["worker_id"])

    if busy:
        raise HTTPException(409, detail={
            "error": "workers_busy",
            "busy": busy,
            "hint": "retry with {\"force\": true} to terminate anyway",
        })

    try:
        terminated = await fleet.scale_down(instance_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except fleet.FleetError as e:    # refused ids or AWS failure
        raise HTTPException(502, str(e))

    log.info("fleet scale-down by %s: %s (draining: %s)",
             role["user"], instance_ids, draining)
    return {
        "status": "terminating",
        "instances": terminated,
        "draining_workers": draining,
        "requested_by": role["user"],
    }


@app.post("/api/fleet/worker/{instance_id}/start")
async def api_fleet_worker_start(instance_id: str, request: Request):
    """Start a stopped pet worker (autonomous-enablements-worker*)."""
    role = await _require_writer(request)
    try:
        result = await fleet.start_worker(instance_id)
    except fleet.FleetError as e:
        raise HTTPException(502, str(e))
    log.info("fleet worker start by %s: %s", role["user"], instance_id)
    return {"status": "starting", **result, "requested_by": role["user"]}


@app.post("/api/fleet/worker/{instance_id}/stop")
async def api_fleet_worker_stop(instance_id: str, request: Request):
    """Stop a running pet worker (autonomous-enablements-worker*)."""
    role = await _require_writer(request)
    try:
        result = await fleet.stop_worker(instance_id)
    except fleet.FleetError as e:
        raise HTTPException(502, str(e))
    log.info("fleet worker stop by %s: %s", role["user"], instance_id)
    return {"status": "stopping", **result, "requested_by": role["user"]}


def start():
    """Entry point for systemd service."""
    import uvicorn
    uvicorn.run(
        "dashboard.app:app",
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )


if __name__ == "__main__":
    start()
