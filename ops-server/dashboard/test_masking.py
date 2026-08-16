"""Pure-logic tests for PII masking (dashboard/masking.py).

No Redis, no FastAPI — same approach as test_live_sessions.py: exercises the
masking transforms applied to anonymous (public) API reads.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_masking.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_masking
"""

from dashboard import masking as m


# ── mask_email ───────────────────────────────────────────────────────────────

def test_mask_email_keeps_two_local_chars_and_domain_initial():
    assert m.mask_email("maria.gonzalez@dynatrace.com") == "ma***@d***"
    assert m.mask_email("bob@x.com") == "bo***@x***"


def test_mask_email_short_local_part():
    assert m.mask_email("a@b.com") == "a***@b***"


def test_mask_email_without_at_is_treated_as_bare_id():
    assert m.mask_email("sergio_hinojosa_2026aug02") == "se***"
    assert m.mask_email("sergiohinojosa") == "se***"


def test_mask_email_falsy_passthrough():
    assert m.mask_email("") == ""
    assert m.mask_email(None) is None


# ── mask_tenant ──────────────────────────────────────────────────────────────

def test_mask_tenant_url_keeps_scheme_and_three_chars():
    assert m.mask_tenant("https://sro97894.apps.dynatrace.com") == "https://sro***"
    assert m.mask_tenant("https://geu80787.live.dynatrace.com/") == "https://geu***"


def test_mask_tenant_bare_id():
    assert m.mask_tenant("sro97894") == "sro***"
    assert m.mask_tenant("ydi9582h.sprint.apps.dynatracelabs.com") == "ydi***"


def test_mask_tenant_falsy_passthrough():
    assert m.mask_tenant("") == ""
    assert m.mask_tenant(None) is None


# ── live-session payload masking ─────────────────────────────────────────────

def _summary(**extra):
    item = {
        "sessionId": "msc46p55-7f00", "title": "K8s 101",
        "trainingId": "kubernetes-101", "state": "open",
        "trainerEmail": "trainer@dynatrace.com",
        "joinedCount": 3, "rosterCount": 10,
        "isTrainer": False, "hasJoined": False,
    }
    item.update(extra)
    return item


def test_mask_live_summary_masks_trainer_and_drops_joincode():
    masked = m.mask_live_summary(_summary(joinCode="ABC234"))
    assert masked["trainerEmail"] == "tr***@d***"
    assert "joinCode" not in masked
    # Non-PII fields untouched
    assert masked["joinedCount"] == 3 and masked["rosterCount"] == 10


def test_mask_live_summary_does_not_mutate_input():
    item = _summary(joinCode="ABC234")
    m.mask_live_summary(item)
    assert item["joinCode"] == "ABC234"
    assert item["trainerEmail"] == "trainer@dynatrace.com"


def test_mask_live_detail_drops_roster_and_joined():
    detail = _summary(
        joinCode="ABC234",
        roster=["a@x.com", "b@y.com"],
        joined=[{"email": "a@x.com", "joinedAt": "2026-08-02T10:00:00+00:00"}])
    masked = m.mask_live_detail(detail)
    assert "roster" not in masked
    assert "joined" not in masked
    assert "joinCode" not in masked
    assert masked["trainerEmail"] == "tr***@d***"


def test_mask_readiness_masks_roster_emails_keeps_states():
    payload = {"results": [
        {"email": "alice@x.com", "state": "ready", "jobId": "mk3p9aqz-7f3a"},
        {"email": "bob@y.com", "state": "none"},
    ]}
    masked = m.mask_readiness(payload)
    assert masked["results"][0] == {
        "email": "al***@x***", "state": "ready", "jobId": "mk3p9aqz-7f3a"}
    assert masked["results"][1] == {"email": "bo***@y***", "state": "none"}
    # input untouched
    assert payload["results"][0]["email"] == "alice@x.com"


def test_mask_progress_hides_identities_but_keeps_the_board_readable():
    payload = {"results": [
        {"email": "alice@x.com", "state": "completed", "progressPct": 100,
         "tenant": "https://sro97894.apps.dynatrace.com"},
        {"email": "bob@y.com", "state": "not-started", "progressPct": None, "tenant": ""},
    ], "summary": {"total": 2, "completed": 1}}
    masked = m.mask_progress(payload)
    assert masked["results"][0]["email"] == "al***@x***"
    assert masked["results"][0]["tenant"] == "https://sro***"
    assert masked["results"][0]["progressPct"] == 100
    assert masked["results"][1]["email"] == "bo***@y***"
    assert masked["summary"] == {"total": 2, "completed": 1}
    assert payload["results"][0]["email"] == "alice@x.com"  # input untouched


def test_mask_progress_keeps_the_callers_own_row():
    payload = {"results": [
        {"email": "alice@x.com", "state": "in-progress", "tenant": "https://a.apps.dynatrace.com"},
        {"email": "bob@y.com", "state": "in-progress", "tenant": "https://b.apps.dynatrace.com"},
    ]}
    masked = m.mask_progress(payload, keep="Alice@X.com")
    assert masked["results"][0]["email"] == "alice@x.com"
    assert masked["results"][0]["tenant"] == "https://a.apps.dynatrace.com"
    assert masked["results"][1]["email"] == "bo***@y***"
    # An empty keep must not un-mask rows with an empty email.
    assert m.mask_progress({"results": [{"email": "", "state": "x"}]})["results"][0]["email"] == ""


def test_mask_pad_masks_question_author_emails():
    payload = {"sections": {"welcome": "hi"}, "qa": [
        {"qid": "1-0", "name": "Alice", "email": "alice@x.com",
         "text": "why?", "answers": []},
    ]}
    masked = m.mask_pad(payload)
    assert masked["qa"][0]["email"] == "al***@x***"
    assert masked["qa"][0]["name"] == "Alice"
    assert masked["sections"] == {"welcome": "hi"}


# ── RFE-C: Virtual Room rail + chat ───────────────────────────────────────────

def _rows():
    return [{"email": "amy@x.com", "name": "Amy", "role": "learner",
             "tenant": "https://sro97894.apps.dynatrace.com", "present": True},
            {"email": "bob@y.com", "name": "Bob", "role": "trainer",
             "tenant": "https://geu80787.apps.dynatrace.com", "present": False}]


def test_mask_attendees_hides_addresses_and_tenants_but_keeps_the_rail_useful():
    masked = m.mask_attendees(_rows())
    assert masked[0]["email"] == "am***@x***"
    assert masked[0]["tenant"] == "https://sro***"
    # The point of the rail survives masking.
    assert masked[0]["name"] == "Amy"
    assert masked[0]["present"] is True
    assert masked[1]["role"] == "trainer"


def test_mask_attendees_keeps_the_callers_own_row_readable():
    masked = m.mask_attendees(_rows(), keep="AMY@X.com")
    assert masked[0]["email"] == "amy@x.com"
    assert masked[0]["tenant"] == "https://sro97894.apps.dynatrace.com"
    assert masked[1]["email"] == "bo***@y***"


def test_mask_attendees_empty_keep_masks_everyone():
    assert all("***" in r["email"] for r in m.mask_attendees(_rows(), keep=""))
    assert m.mask_attendees([]) == []
    assert m.mask_attendees(None) == []


def _msgs():
    return [{"mid": "1-0", "email": "amy@x.com", "name": "Amy",
             "role": "learner", "text": "hello", "ts": "t", "pinned": False}]


def test_mask_chat_masks_the_sender_but_never_the_message():
    masked = m.mask_chat(_msgs())
    assert masked[0]["email"] == "am***@x***"
    assert masked[0]["text"] == "hello"
    assert masked[0]["name"] == "Amy"
    assert masked[0]["mid"] == "1-0"


def test_mask_chat_keeps_the_callers_own_address():
    assert m.mask_chat(_msgs(), keep="amy@x.com")[0]["email"] == "amy@x.com"
    assert m.mask_chat([], keep="amy@x.com") == []


# ── scrub_for_log (CodeQL py/log-injection) ──────────────────────────────────

def test_scrub_for_log_leaves_ordinary_values_alone():
    assert m.scrub_for_log("mk3p9aqz-7f3a") == "mk3p9aqz-7f3a"
    assert m.scrub_for_log("maria.gonzalez@dynatrace.com") == "maria.gonzalez@dynatrace.com"


def test_scrub_for_log_kills_the_forged_second_line():
    forged = "real-id\n2026-08-14 12:00:00 INFO live: terminate-all everything"
    out = m.scrub_for_log(forged)
    assert "\n" not in out and "\r" not in out
    assert out.startswith("real-id ")


def test_scrub_for_log_kills_carriage_returns_and_escapes():
    # \r alone overwrites the line in a terminal; \x1b starts an ANSI sequence
    # that can erase the lines above it.
    assert m.scrub_for_log("a\rb") == "a b"
    assert m.scrub_for_log("a\x1b[2Kb") == "a [2Kb"
    assert m.scrub_for_log("a\x00\x0b\x7fb") == "a   b"


def test_scrub_for_log_caps_length():
    out = m.scrub_for_log("x" * 500)
    assert len(out) == 201 and out.endswith("…")
    assert m.scrub_for_log("x" * 10, limit=4) == "xxxx…"


def test_scrub_for_log_absent_values_log_as_absent():
    assert m.scrub_for_log(None) == ""
    assert m.scrub_for_log("") == ""
    assert m.scrub_for_log(0) == ""


def test_scrub_for_log_accepts_non_strings():
    assert m.scrub_for_log(ValueError("boom\nfake")) == "boom fake"
    assert m.scrub_for_log(42) == "42"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all masking tests passed")
