"""Make a caller-supplied value safe to put in a log record.

Tenant URLs, job ids, session ids, emails, repo names and GitHub logins all
arrive from outside and all end up in log lines. A value containing
"\\n2026-08-16 12:00:00 INFO fleet scaled to 0" writes a second line that reads
exactly like a real one. The journal is the evidence we reach for when a
workshop goes wrong, and evidence that a caller can write into is not evidence.

This is the whole defence, and it is deliberately NOT a logging.Filter:

  - A filter would have to truncate every string argument in the service to be
    useful, which silently damages the log lines that legitimately carry a long
    DQL query, a URL or a diff.
  - A filter that rewrote `record.args` would break `%d` formatting the moment
    it touched a non-string, and would have to special-case exc_info to avoid
    mangling tracebacks.
  - And CodeQL cannot see it, so every call site stays flagged and the next
    person cannot tell which ones were considered.

So it is applied per call site, where the author can see it.
"""

import re

# Everything else that could move the cursor or hide a line: the rest of the
# C0 range and DEL. A lone \x0b, or an \x1b[2K, is as good at erasing the line
# above it as a newline is at forging the line below.
_LOG_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def scrub_for_log(value, limit: int = 200) -> str:
    """Flatten a caller-supplied value into one log-safe line.

    Control characters become spaces and the result is capped, so one very long
    id cannot push the rest of a line out of view either.

    The two line terminators are stripped with `str.replace` before the
    catch-all regex, which is redundant at runtime and deliberate: CodeQL's
    py/log-injection barrier recognises `replace`, not `re.sub`, so writing it
    this way is what lets the analysis see that the path is cut here instead of
    re-reporting every call site. Do not "simplify" it away.

    Absent values become '' rather than 'None' so an absent field logs as
    absent. Absent means None or the empty string — NOT falsy: `0` and `False`
    are values somebody chose, and a line reading `rows=` when the caller sent
    `rows=0` is a worse log than one reading `rows=0`.
    """
    if value is None or value == "":
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _LOG_UNSAFE.sub(" ", text)
    return text if len(text) <= limit else text[:limit] + "…"
