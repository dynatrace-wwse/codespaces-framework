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


def test_mask_pad_masks_question_author_emails():
    payload = {"sections": {"welcome": "hi"}, "qa": [
        {"qid": "1-0", "name": "Alice", "email": "alice@x.com",
         "text": "why?", "answers": []},
    ]}
    masked = m.mask_pad(payload)
    assert masked["qa"][0]["email"] == "al***@x***"
    assert masked["qa"][0]["name"] == "Alice"
    assert masked["sections"] == {"welcome": "hi"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all masking tests passed")
