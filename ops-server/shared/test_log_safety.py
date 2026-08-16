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
    assert scrub_for_log(0) == ""


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
