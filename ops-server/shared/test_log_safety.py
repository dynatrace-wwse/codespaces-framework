"""Tests for shared/log_safety.py.

Runnable two ways:
  - pytest:     python3 -m pytest shared/test_log_safety.py
  - standalone: /home/ops/ops-venv/bin/python -m shared.test_log_safety

`dashboard/test_masking.py` additionally asserts that `masking.scrub_for_log`
is still the same object, so `pytest dashboard/` fails if the re-export breaks.
"""

from shared.log_safety import scrub_for_log


def test_leaves_ordinary_values_alone():
    assert scrub_for_log("mk3p9aqz-7f3a") == "mk3p9aqz-7f3a"
    assert scrub_for_log("maria.gonzalez@dynatrace.com") == "maria.gonzalez@dynatrace.com"
    assert scrub_for_log("https://sro97894.apps.dynatrace.com") == "https://sro97894.apps.dynatrace.com"


def test_kills_the_forged_second_line():
    forged = "real-id\n2026-08-16 12:00:00 INFO live: terminate-all everything"
    out = scrub_for_log(forged)
    assert "\n" not in out and "\r" not in out
    assert out.startswith("real-id ")


def test_kills_carriage_returns_and_escapes():
    # \r alone overwrites the line in a terminal; \x1b starts an ANSI sequence
    # that can erase the lines above it.
    assert scrub_for_log("a\rb") == "a b"
    assert scrub_for_log("a\x1b[2Kb") == "a [2Kb"
    assert scrub_for_log("a\x00\x0b\x7fb") == "a   b"
    assert scrub_for_log("a\r\nb") == "a  b"


def test_caps_length():
    out = scrub_for_log("x" * 500)
    assert len(out) == 201 and out.endswith("…")
    assert scrub_for_log("x" * 10, limit=4) == "xxxx…"
    # Exactly at the limit is not truncated, so the ellipsis always means
    # "there was more".
    assert scrub_for_log("x" * 4, limit=4) == "xxxx"


def test_absent_values_log_as_absent():
    assert scrub_for_log(None) == ""
    assert scrub_for_log("") == ""


def test_zero_is_a_value_not_an_absence():
    # A falsy check here would log `rows=` when the caller sent `rows=0`,
    # which is a worse log line than the one it replaced.
    assert scrub_for_log(0) == "0"
    assert scrub_for_log(False) == "False"
    assert scrub_for_log(0.0) == "0.0"


def test_accepts_non_strings():
    assert scrub_for_log(ValueError("boom\nfake")) == "boom fake"
    assert scrub_for_log(42) == "42"


def test_a_container_is_safe_by_repr_not_by_replacement():
    """A list/dict argument is stringified with repr(), which escapes the
    newline into a literal backslash-n. Nothing is left for the replace to do —
    the value is already one line. Asserted so nobody 'fixes' the scrub to
    recurse into containers on the belief that it currently misses them."""
    out = scrub_for_log(["i-0123\n", "i-0456"])
    assert "\n" not in out
    assert out == r"['i-0123\n', 'i-0456']"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all log-safety tests passed")


# ── safe_error_detail ───────────────────────────────────────────────────────

def test_error_fields_are_kept():
    from .log_safety import safe_error_detail
    out = safe_error_detail('{"error":"invalid_request","error_description":"",'
                            '"issueId":"52UFH6KTL7CHGRRE"}')
    assert "error=invalid_request" in out
    assert "issueId=52UFH6KTL7CHGRRE" in out


def test_a_token_in_the_body_is_never_copied_into_the_log():
    """The reason this exists. A token endpoint's body is the only useful
    diagnostic on a 4xx, and the same shape of body can carry an access_token —
    logging one in clear text turns a failed mint into a leaked credential."""
    from .log_safety import safe_error_detail
    out = safe_error_detail('{"access_token":"eyJhbGciOiJFUzI1NiJ9.SUPERSECRET",'
                            '"scope":"platform-token:tokens:write"}')
    assert "SUPERSECRET" not in out
    assert "eyJ" not in out
    # It still says what WAS there, so a new error shape is visible.
    assert "access_token" in out and "keys:" in out


def test_an_unparseable_body_is_reported_by_length_not_quoted():
    from .log_safety import safe_error_detail
    out = safe_error_detail("<html>token=abc123</html>")
    assert "abc123" not in out
    assert "unparseable" in out and "25 chars" in out


def test_empty_and_missing_bodies_do_not_crash():
    from .log_safety import safe_error_detail
    assert safe_error_detail(None) == "(no body)"
    assert safe_error_detail("") == "(empty body)"
    assert safe_error_detail("   ") == "(empty body)"
    assert "list" in safe_error_detail('[1,2,3]')


def test_control_characters_in_an_error_field_are_still_scrubbed():
    from .log_safety import safe_error_detail
    out = safe_error_detail('{"message":"bad\\n2026-01-01 INFO fleet scaled to 0"}')
    assert "\n" not in out
