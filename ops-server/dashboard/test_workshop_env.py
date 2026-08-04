"""Environment lifetime sizing for workshops."""

from dashboard.app import workshop_session_hours, MAX_SESSION_HOURS

def test_no_duration_uses_default():
    assert workshop_session_hours({}) == 0
    assert workshop_session_hours({"durationMinutes": ""}) == 0
    assert workshop_session_hours({"durationMinutes": "0"}) == 0

def test_two_hour_workshop_outlives_itself():
    # The bug: at the 2h default a 120-minute workshop reaped every environment
    # exactly as the session ended.
    assert workshop_session_hours({"durationMinutes": "120"}) == 4

def test_partial_hour_rounds_up():
    assert workshop_session_hours({"durationMinutes": "90"}) == 4   # 2h + 2 grace
    assert workshop_session_hours({"durationMinutes": "30"}) == 3   # 1h + 2 grace

def test_clamped_to_max():
    assert workshop_session_hours({"durationMinutes": "10000"}) == MAX_SESSION_HOURS

def test_garbage_falls_back_to_default():
    assert workshop_session_hours({"durationMinutes": "soon"}) == 0
    assert workshop_session_hours(None) == 0
