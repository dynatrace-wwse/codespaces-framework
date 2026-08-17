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

    async def hset(self, key, field=None, value=None, mapping=None):
        target = self.h.setdefault(key, {})
        if mapping:
            target.update(mapping)
        else:
            target[field] = value

    async def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)

    async def hlen(self, key):
        return len(self.h.get(key, {}))

    async def smembers(self, key):
        return set(self.s.get(key, set()))

    async def sadd(self, key, member):
        target = self.s.setdefault(key, set())
        before = len(target)
        target.add(member)
        return len(target) - before

    async def get(self, key):
        return self.k.get(key)

    async def incr(self, key):
        self.k[key] = str(int(self.k.get(key, 0)) + 1)
        return int(self.k[key])

    async def expire(self, key, ttl):
        return True

    async def xadd(self, key, mapping, maxlen=None, approximate=None):
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
    """One clean room per test: a running session whose room the trainer has
    OPENED, with one joined learner and no chat yet.

    roomOpen matters — a learner cannot write to a room the trainer has not
    opened (EPIC-007), so a fixture without it makes every chat test a test of
    the gate. The gate has its own tests below."""
    _fn._saved_pool = a.pool
    fake = FakeRedis()
    a.pool = fake
    fake.h[f"live:session:{SID}"] = {
        "title": "Kubernetes 101", "trainingId": "kubernetes-101",
        "trainers": json.dumps([TRAINER]), "state": "running",
        "roomOpen": "1",
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
    """A learner may claim any name; the role comes from the stored trainer
    team, so a message can never impersonate the trainer."""
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


# ── The room gate (EPIC-007) ─────────────────────────────────────────────────
#
# A learner can always ENTER a workshop, and everything in it is locked until
# the trainer opens the room. That lets the trainer write the welcome note and
# the pad first, so the cohort never walks into an empty room — and it is a
# separate decision from starting the workshop, which is what unlocks the
# environment and the lab steps.

def _close_room():
    a.pool.h[f"live:session:{SID}"]["roomOpen"] = "0"


def _hand(email, raised=True, note="I need help"):
    return client.post(f"/api/live/sessions/{SID}/hand", headers=BEARER,
                       json={"email": email, "name": "", "step": "2",
                             "note": note, "raised": raised})


def _room(email, open_=True):
    return client.post(f"/api/live/sessions/{SID}/room", headers=BEARER,
                       json={"trainerEmail": email, "open": open_})


def test_a_closed_room_refuses_a_learner():
    _close_room()
    r = _chat(LEARNER)
    assert r.status_code == 409
    assert "not opened the room" in r.json()["detail"]


def test_a_closed_room_still_lets_the_TRAINER_prepare_it():
    """The whole reason the gate exists: the welcome note and the pad get
    written before anyone is let in."""
    _close_room()
    assert _chat(TRAINER, "setting up").status_code == 200
    assert client.post(f"/api/live/sessions/{SID}/pad/section", headers=BEARER,
                       json={"trainerEmail": TRAINER, "key": "welcome",
                             "markdown": "# Welcome"}).status_code == 200


def test_a_learner_cannot_raise_a_hand_before_the_room_opens():
    _close_room()
    assert _hand(LEARNER).status_code == 409


def test_raising_a_hand_needs_the_room_not_a_started_workshop():
    """Someone who cannot get their environment going is exactly who needs to
    raise a hand, and that happens before the workshop formally starts."""
    a.pool.h[f"live:session:{SID}"]["state"] = "open"
    assert _hand(LEARNER).status_code == 200


def test_trainer_opens_and_closes_the_room():
    _close_room()
    assert _room(TRAINER, True).json()["roomOpen"] is True
    assert _chat(LEARNER).status_code == 200
    assert _room(TRAINER, False).json()["roomOpen"] is False
    assert _chat(LEARNER).status_code == 409


def test_opening_the_room_is_idempotent():
    assert _room(TRAINER, True).status_code == 200
    assert _room(TRAINER, True).json()["roomOpen"] is True


def test_only_a_trainer_may_open_the_room():
    _close_room()
    assert _room(LEARNER, True).status_code == 403
    assert a.pool.h[f"live:session:{SID}"]["roomOpen"] == "0"


def test_a_co_trainer_may_open_the_room():
    """Every trainer on the team holds the same authority."""
    co = "co@dynatrace.com"
    a.pool.h[f"live:session:{SID}"]["trainers"] = json.dumps([TRAINER, co])
    _close_room()
    assert _room(co, True).json()["roomOpen"] is True


def test_an_ended_room_cannot_be_reopened():
    """Its pad is already exported and frozen — a late write would vanish."""
    a.pool.h[f"live:session:{SID}"]["state"] = "ended"
    assert _room(TRAINER, True).status_code == 409
    assert _chat(TRAINER).status_code == 409


def test_room_flag_is_reported_even_when_false():
    """Absent must never have to be read as false by the client."""
    _close_room()
    body = _room(TRAINER, False).json()
    assert body["roomOpen"] is False and body["gateAhead"] is False


# ── Pacing across a trainer team (EPIC-007) ──────────────────────────────────

def _pacing(email, step, unlock=None, gate=None):
    body = {"trainerEmail": email, "step": step}
    if unlock is not None:
        body["unlockPath"] = unlock
    if gate is not None:
        body["gateAhead"] = gate
    return client.post(f"/api/live/sessions/{SID}/pacing", headers=BEARER,
                       json=body)


def test_any_trainer_may_move_the_class_pointer():
    co = "co@dynatrace.com"
    a.pool.h[f"live:session:{SID}"]["trainers"] = json.dumps([TRAINER, co])
    assert _pacing(TRAINER, 2).json()["trainerStep"] == 2
    assert _pacing(co, 5).json()["trainerStep"] == 5


def test_the_pointer_records_who_moved_it_and_when():
    """Last write wins, so co-trainers need to see the move happen rather than
    wonder why the step jumped."""
    co = "co@dynatrace.com"
    a.pool.h[f"live:session:{SID}"]["trainers"] = json.dumps([TRAINER, co])
    body = _pacing(co, 4).json()
    assert body["pacingBy"] == co
    assert body["pacingAt"]


def test_a_learner_cannot_move_the_pointer():
    _pacing(TRAINER, 3)
    assert _pacing(LEARNER, 9).status_code == 403
    assert a.pool.h[f"live:session:{SID}"]["trainerStep"] == "3"


def test_both_toggles_round_trip_independently():
    body = _pacing(TRAINER, 3, unlock=True).json()
    assert body["unlockPath"] is True and body["gateAhead"] is False
    body = _pacing(TRAINER, 3, gate=True).json()
    assert body["unlockPath"] is True and body["gateAhead"] is True
    body = _pacing(TRAINER, 3, unlock=False).json()
    assert body["unlockPath"] is False and body["gateAhead"] is True


def test_omitting_a_toggle_leaves_it_alone():
    """Moving the pointer must not silently reset what the trainer set."""
    _pacing(TRAINER, 2, unlock=True, gate=True)
    body = _pacing(TRAINER, 3).json()
    assert body["unlockPath"] is True and body["gateAhead"] is True


# ── Access to one workshop (EPIC-007) ────────────────────────────────────────
#
# The workshop route resolves itself from this ONE read, so the status code has
# to distinguish "no such workshop" from "not yours" — otherwise the app can
# only render an empty room and hope.

def _detail(email=""):
    q = f"?email={email}" if email else ""
    return client.get(f"/api/live/sessions/{SID}{q}", headers=BEARER)


def test_unknown_workshop_is_404():
    assert client.get("/api/live/sessions/ws_nope", headers=BEARER).status_code == 404


def test_a_stranger_is_403_not_an_empty_room():
    r = _detail("stranger@elsewhere.com")
    assert r.status_code == 403
    assert "not a participant" in r.json()["detail"]


def test_a_rostered_learner_gets_the_workshop():
    a.pool.s[f"live:session:{SID}:roster"] = {"rostered@x.com"}
    body = _detail("rostered@x.com").json()
    assert body["sessionId"] == SID
    assert body["isTrainer"] is False


def test_a_joined_learner_gets_the_workshop_even_off_roster():
    """join-by-code appends to the roster, but a learner who joined before the
    roster was retyped must not lose access to the room they are sitting in."""
    assert _detail(LEARNER).status_code == 200


def test_a_trainer_gets_the_workshop_and_the_roster():
    body = _detail(TRAINER).json()
    assert body["isTrainer"] is True
    assert "joinCode" in body or body["rosterCount"] == 0


def test_a_co_trainer_is_not_a_stranger():
    co = "co@dynatrace.com"
    a.pool.h[f"live:session:{SID}"]["trainers"] = json.dumps([TRAINER, co])
    assert _detail(co).status_code == 200
    assert _detail(co).json()["isTrainer"] is True


def test_an_anonymous_read_stays_a_masked_200():
    """The public read path. 403-ing it would break every surface that reads a
    workshop without claiming an identity."""
    r = _detail()
    assert r.status_code == 200
    assert "***" in r.json()["trainerEmail"]


def test_the_detail_payload_answers_role_and_both_gates_in_one_call():
    """What kills the reload bug: no second request to learn who you are or
    what is unlocked."""
    body = _detail(TRAINER).json()
    for field in ("isTrainer", "hasJoined", "roomOpen", "gateAhead", "state"):
        assert field in body, f"{field} missing — the route would need a 2nd call"


# ── myTenant: the caller's own binding, and nobody else's ────────────────────
#
# The workshop lobby asks one question the detail payload could not answer:
# "am I already provisioning in the tenant I am looking at?" Without it the app
# had to offer "Provision here instead" to every checked-in learner, including
# the ones who were already in the right place.

def test_a_learner_gets_their_own_bound_tenant():
    a.pool.h[f"live:session:{SID}:tenants"] = {
        LEARNER: "https://abc123.apps.dynatrace.com",
        "someone.else@x.com": "https://zzz999.apps.dynatrace.com",
    }
    body = _detail(LEARNER).json()
    assert body["myTenant"] == "https://abc123.apps.dynatrace.com"
    # Scoped to the caller: a learner learns nothing about where anyone else is.
    assert "zzz999" not in json.dumps(body)


def test_a_learner_with_no_binding_gets_an_empty_my_tenant():
    body = _detail(LEARNER).json()
    assert body["myTenant"] == ""


def test_a_trainer_still_gets_every_binding_plus_their_own():
    a.pool.h[f"live:session:{SID}:tenants"] = {
        LEARNER: "https://abc123.apps.dynatrace.com"}
    body = _detail(TRAINER).json()
    assert body["joined"] == [
        {"email": LEARNER, "joinedAt": body["joined"][0]["joinedAt"],
         "tenant": "https://abc123.apps.dynatrace.com"}]
    assert body["myTenant"] == ""       # the trainer never checked in here


# ── join-by-code binds the tenant it was typed in ────────────────────────────

def _join_by_code(code="ABC123", email="newbie@x.com", tenant=""):
    return client.post("/api/live/sessions/join-by-code", headers=BEARER,
                       json={"code": code, "email": email, "tenant": tenant})


def test_join_by_code_registers_and_records_where_the_learner_is():
    """Registration, not check-in: the roster gains the email, `joined` does
    not — but the tenant IS bound, so the trainer's registrant table shows
    where each self-registered learner will run instead of a dash."""
    a.pool.k["live:joincode:ABC123"] = SID
    r = _join_by_code(tenant="https://abc123.apps.dynatrace.com/")
    assert r.status_code == 200
    assert "newbie@x.com" in a.pool.s[f"live:session:{SID}:roster"]
    # Bound as the canonical environment id — what every comparison uses.
    assert a.pool.h[f"live:session:{SID}:tenants"]["newbie@x.com"] == "abc123"
    # Registered, not present — the gates must not move.
    assert "newbie@x.com" not in a.pool.h[f"live:session:{SID}:joined"]


def test_join_by_code_without_a_tenant_binds_nothing():
    """An older app that sends no tenant still registers — the field is
    additive, not required."""
    a.pool.k["live:joincode:ABC123"] = SID
    assert _join_by_code().status_code == 200
    assert "newbie@x.com" in a.pool.s[f"live:session:{SID}:roster"]
    assert a.pool.h.get(f"live:session:{SID}:tenants", {}) == {}


def test_join_by_code_by_a_trainer_neither_rosters_nor_binds():
    """A trainer using the code is just their way IN. Rostering them would
    demote them to a learner row and inflate the cohort count."""
    a.pool.k["live:joincode:ABC123"] = SID
    r = _join_by_code(email=TRAINER, tenant="https://abc123.apps.dynatrace.com")
    assert r.status_code == 200
    assert TRAINER not in a.pool.s.get(f"live:session:{SID}:roster", set())
    assert a.pool.h.get(f"live:session:{SID}:tenants", {}) == {}
