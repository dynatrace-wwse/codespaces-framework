"""In-app Virtual Room surface (RFE-C) — the poll snapshot and the chat POST.

Why this file exists: chat and the attendee rail used to live only on the SSE
stream, but the Virtual Room tab *inside* the Dynatrace app cannot open an
EventSource to Orbital (CSP blocks in-frame fetch, so every call is proxied by
the app's orbital function). Inside the app the room therefore rendered with no
people and no chat in it. These tests pin the fix:

  * GET  /api/live/sessions/{id}/pad   carries chat + attendees
  * polling that route counts as being in the room (presence heartbeat)
  * a learner sees the room masked; the trainer sees it raw
  * POST /api/live/sessions/{id}/pad/chat decides the role SERVER-side, so a
    learner cannot post as the trainer

Redis is replaced by an in-process fake (below) — like every other test file
here, this one touches no real Redis and no network.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_live_room.py
"""

import json
from datetime import datetime, timezone

import dashboard.app as a
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}
SID = "test-room-1"
TRAINER = "trainer@dynatrace.com"
LEARNER = "learner@customer.com"


class FakeRedis:
    """The handful of commands the room helpers use, over plain dicts.

    Streams are lists of (id, fields); ids are monotonic so assemble_chat's
    watermark comparison behaves exactly as it does against real Redis."""

    def __init__(self):
        self.h: dict = {}       # hashes
        self.s: dict = {}       # sets
        self.k: dict = {}       # strings/counters
        self.x: dict = {}       # streams
        self.seq = 0

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def smembers(self, key):
        return set(self.s.get(key, set()))

    async def get(self, key):
        return self.k.get(key)

    async def incr(self, key):
        self.k[key] = str(int(self.k.get(key, 0)) + 1)
        return int(self.k[key])

    async def expire(self, key, ttl):
        return True

    async def xadd(self, key, mapping):
        self.seq += 1
        entry_id = f"{1000 + self.seq}-0"
        self.x.setdefault(key, []).append((entry_id, dict(mapping)))
        return entry_id

    async def xrange(self, key, min="-", max="+"):
        entries = self.x.get(key, [])
        if min != "-" or max != "+":
            return [e for e in entries if e[0] == min]
        return list(entries)


def setup_module(_module):
    """Accept a known bearer regardless of the environment — the snapshot's
    masking decision hangs off it (service caller = raw view)."""
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.ORBITAL_TOKENS = ("test-service-token",)


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens


def setup_function(_fn):
    """One clean room per test: an open session with the trainer + one joined
    learner, and no chat yet."""
    _fn._saved_pool = a.pool
    fake = FakeRedis()
    a.pool = fake
    fake.h[f"live:session:{SID}"] = {
        "title": "Kubernetes 101", "trainingId": "kubernetes-101",
        "trainerEmail": TRAINER, "state": "running",
        "ownerTenant": "https://geu80787.apps.dynatrace.com",
    }
    fake.h[f"live:session:{SID}:joined"] = {
        LEARNER: datetime.now(timezone.utc).isoformat()}


def teardown_function(_fn):
    a.pool = _fn._saved_pool


def _pad(email=""):
    q = f"?email={email}" if email else ""
    return client.get(f"/api/live/sessions/{SID}/pad{q}", headers=BEARER)


def _chat(email, text="hello room", name=""):
    return client.post(f"/api/live/sessions/{SID}/pad/chat", headers=BEARER,
                       json={"email": email, "text": text, "name": name})


def test_pool_is_faked():
    """Guard: if the app's startup handler ever rebinds pool under TestClient,
    every assertion below would silently run against production Redis."""
    _pad(TRAINER)
    assert isinstance(a.pool, FakeRedis)


def test_snapshot_carries_chat_and_attendees():
    _chat(TRAINER, "welcome everyone")
    body = _pad(TRAINER).json()
    assert [m["text"] for m in body["chat"]] == ["welcome everyone"]
    assert TRAINER in [x["email"] for x in body["attendees"]]


def test_polling_the_snapshot_puts_you_in_the_room():
    """The tab has no SSE connection — the poll IS the heartbeat. Without this
    the rail only ever showed people who opened the popup."""
    def present():
        return [x["email"] for x in _pad(TRAINER).json()["attendees"] if x["present"]]

    # The learner has joined the session but never opened the room.
    assert LEARNER not in present()
    _pad(LEARNER)
    assert LEARNER in present()


def test_learner_sees_the_room_masked_except_their_own_row():
    _chat(TRAINER, "welcome everyone")
    body = _pad(LEARNER).json()
    assert [m["email"] for m in body["chat"]] == ["tr***@d***"]
    own = [x for x in body["attendees"] if x["email"] == LEARNER]
    assert own, "a learner must still recognise their own row"


def test_anonymous_snapshot_is_fully_masked():
    """No bearer, no email: the public read path, masked like every other one."""
    _chat(TRAINER, "welcome everyone")
    body = client.get(f"/api/live/sessions/{SID}/pad").json()
    assert all("***" in m["email"] for m in body["chat"])


def test_chat_role_is_decided_server_side():
    """A learner may claim any name; the role comes from the stored
    trainerEmail, so a message can never impersonate the trainer."""
    _chat(LEARNER, "I am the trainer", name="Trainer")
    roles = {m["email"]: m["role"] for m in _pad(TRAINER).json()["chat"]}
    assert roles[LEARNER] == "learner"
    _chat(TRAINER, "no, I am")
    roles = {m["email"]: m["role"] for m in _pad(TRAINER).json()["chat"]}
    assert roles[TRAINER] == "trainer"


def test_chat_rejects_an_invalid_email():
    assert _chat("not-an-email").status_code == 400


def test_chat_rejects_empty_text():
    assert _chat(LEARNER, "   ").status_code == 400


def test_chat_is_rate_limited():
    codes = [_chat(LEARNER, f"msg {i}").status_code for i in range(8)]
    assert 429 in codes, f"no rate limit hit: {codes}"


def test_chat_is_refused_once_the_session_ended():
    """The export snapshot is frozen on end — a late message would vanish."""
    a.pool.h[f"live:session:{SID}"]["state"] = "ended"
    assert _chat(LEARNER).status_code == 409


def test_ended_session_does_not_heartbeat():
    """Nobody is 'in the room' after it closes, however long the tab is left
    open on a stale page."""
    a.pool.h[f"live:session:{SID}"]["state"] = "ended"
    _pad(LEARNER)
    assert not [x for x in _pad(TRAINER).json()["attendees"] if x["present"]]


def test_heartbeat_keeps_a_name_the_popup_established():
    """The in-app tab heartbeats without a display name (no pad-token claim).
    That must not blank the name the popup already stored."""
    presence_key = a._room_keys(SID)[3]
    a.pool.h[presence_key] = {LEARNER: json.dumps(
        {"name": "Ada L", "role": "learner",
         "ts": datetime.now(timezone.utc).isoformat()})}
    _pad(LEARNER)
    names = {x["email"]: x["name"] for x in _pad(TRAINER).json()["attendees"]}
    assert names[LEARNER] == "Ada L"


def test_unknown_session_is_404_on_both_routes():
    assert client.get("/api/live/sessions/nope/pad").status_code == 404
    assert client.post("/api/live/sessions/nope/pad/chat", headers=BEARER,
                       json={"email": LEARNER, "text": "hi"}).status_code == 404
