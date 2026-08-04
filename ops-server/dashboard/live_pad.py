"""Pure decision logic for the structured workshop pad (EPIC-002).

No Redis, no FastAPI — everything here is deterministic and unit-tested in
dashboard/test_workshops.py. The /api/live/*/pad* endpoints in app.py stay
thin: they read/write the Redis keys and delegate every decision here.

Redis model (streams, NOT pub/sub):
  live:pad:{id}:sections  hash    section key -> markdown (keys: welcome,
                                  solutions)
  live:pad:{id}:qa        stream  XADD {type: question|answer, qid, email,
                                  name, text, ts}. A question's qid is its
                                  own stream entry id; answers carry the qid
                                  of the question they answer.
  live:pad:{id}:export    str     standalone HTML snapshot, 30-day TTL,
                                  written on the end/cancel transition
  live:padtoken:{token}   str     single-use JSON handoff (60 s TTL) — same
                                  pattern as shell:token:{token}
  live:padsession:{tok2}  str     claimed pad identity JSON (8 h TTL):
                                  sessionId, email, name, role
"""

import html
import json
import re
from datetime import datetime, timezone

from dashboard import live_sessions

SECTION_KEYS = ("welcome", "solutions")
ROLES = ("trainer", "learner")

QUESTION_MAX_CHARS = 2000

# Chat (RFE-C) is conversation, not the structured record — a chat line that
# needs 2000 characters belongs in the Q&A panel, which keeps it, exports it
# and gets it answered.
CHAT_MAX_CHARS = 1000

# A 100+ attendee bootcamp room needs a floor under a flood. 5 messages per
# 10 s is faster than anyone types a real sentence, so only scripted or pasted
# bursts reach it — a legitimate fast exchange never does.
CHAT_RATE_LIMIT = 5
CHAT_RATE_WINDOW_SECONDS = 10

# Someone is "in the room" while their Virtual Room holds an SSE connection,
# which re-stamps their heartbeat every ~15 s. Two minutes is eight missed
# beats: long enough to survive a sleeping laptop or a reconnect, short enough
# that the rail is not a list of ghosts.
PRESENCE_WINDOW_SECONDS = 120

# TTLs — the single-use handoff token mirrors shell:token (60 s); the claimed
# pad session outlives a full workshop day; the export matches a month of
# "can I still get the notes?" follow-ups.
PAD_TOKEN_TTL_SECONDS = 60
PAD_SESSION_TTL_SECONDS = 8 * 3600
EXPORT_TTL_SECONDS = 30 * 24 * 3600

_TAG_RE = re.compile(r"<[^>]*>")


def strip_html(text) -> str:
    """Drop HTML tags and unescape entities — pad text is stored as plain
    text/markdown and escaped again on render, so markup never round-trips."""
    return html.unescape(_TAG_RE.sub("", text or ""))


def clean_text(text, field="text", limit=QUESTION_MAX_CHARS) -> str:
    """Normalize a question/answer/chat body: strip HTML + whitespace, enforce
    the cap. Raises ValueError (→ HTTP 400) when empty or too long."""
    cleaned = strip_html(text).strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return cleaned


def validate_role(role) -> str:
    """'trainer' or 'learner' — anything else is a 400."""
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise ValueError("role must be 'trainer' or 'learner'")
    return role


# ── Chat + attendee rail (RFE-C) ──────────────────────────────────────────────
# Chat is deliberately a SEPARATE stream from Q&A. Q&A is the structured,
# answered, exported record of the workshop; chat is the conversation around
# it. Merging them would cost the export its shape.

def _id_tuple(stream_id) -> tuple[int, int]:
    """'1722700000123-4' -> (1722700000123, 4).

    Redis stream ids order numerically per component, so they must never be
    compared as strings — '10-0' sorts before '9-0' lexicographically."""
    ms, _, seq = str(stream_id or "0-0").partition("-")
    try:
        return int(ms), int(seq or 0)
    except ValueError:
        return 0, 0


def clean_chat(text) -> str:
    """A chat line: same HTML stripping as Q&A, shorter cap."""
    return clean_text(text, field="message", limit=CHAT_MAX_CHARS)


def rate_limit_error(count):
    """Return (429, detail) when the sender has spent their window, else None.

    `count` is the value AFTER the incrementing INCR, so the Nth message of a
    window is allowed and the N+1th is refused."""
    if count > CHAT_RATE_LIMIT:
        return 429, (f"you are sending messages too quickly — "
                     f"{CHAT_RATE_LIMIT} per {CHAT_RATE_WINDOW_SECONDS}s")
    return None


def assemble_chat(entries, pins=(), cleared_before="") -> list[dict]:
    """[(stream_entry_id, fields)] → chat messages in arrival order.

    `pins` are the message ids the trainer pinned; `cleared_before` is the id
    of the last message a "clear the room" covered. Cleared messages drop out
    of the view but stay in the stream — the clear is a soft delete, so the
    record survives its 7-day TTL for an audit even though the room, and the
    export handed to the cohort, never show it again."""
    watermark = _id_tuple(cleared_before) if cleared_before else None
    pinned = set(pins or ())
    out = []
    for entry_id, fields in entries or []:
        if watermark and _id_tuple(entry_id) <= watermark:
            continue
        out.append({
            "mid": entry_id,
            "email": fields.get("email", ""),
            "name": fields.get("name", ""),
            "role": fields.get("role", ""),
            "text": fields.get("text", ""),
            "ts": fields.get("ts", ""),
            "pinned": entry_id in pinned,
        })
    return out


def display_name(name, email) -> str:
    """A name to show in the rail. Falls back to the local part of the email
    so a row is never blank, and never to the full address — the rail is read
    by learners whose view of everyone else is masked."""
    name = (name or "").strip()
    return name or str(email or "").partition("@")[0]


def is_present(last_seen, now=None) -> bool:
    """Was this heartbeat stamped inside the presence window?"""
    if not last_seen:
        return False
    try:
        seen = datetime.fromisoformat(str(last_seen))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return 0 <= (now - seen).total_seconds() <= PRESENCE_WINDOW_SECONDS


def _presence_record(raw) -> dict:
    """Presence values are JSON ({name, role, ts}); tolerate a bare ISO string
    so a heartbeat written by an older build still counts as presence."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"ts": str(raw)}
    return parsed if isinstance(parsed, dict) else {"ts": str(raw)}


def shape_attendees(joined, tenants, session, presence, now=None) -> list[dict]:
    """The Virtual Room's right rail: everyone in this workshop, the tenant
    they joined from, and whether they are in the room right now.

    Presence here means ROOM presence — an open Virtual Room heartbeat — not
    training activity. The two are different claims: a learner can be deep in
    the lab with the room closed. The rail says "who is here", so it answers
    exactly that and leaves lab progress to the Workshop Board.

    The trainer is always a row even though trainers never "join" their own
    session, and rows sort trainer → present → name so the useful half of a
    100-person cohort stays above the fold."""
    now = now or datetime.now(timezone.utc)
    joined, tenants, presence = joined or {}, tenants or {}, presence or {}
    trainer = live_sessions.normalize_email((session or {}).get("trainerEmail", ""))

    emails = {live_sessions.normalize_email(e) for e in joined}
    emails.update(live_sessions.normalize_email(e) for e in presence)
    emails.discard("")
    if trainer:
        emails.add(trainer)

    rows = []
    for email in emails:
        record = _presence_record(presence.get(email))
        is_trainer = bool(trainer) and email == trainer
        rows.append({
            "email": email,
            "name": display_name(record.get("name"), email),
            "role": "trainer" if is_trainer else "learner",
            "tenant": tenants.get(email, ""),
            "present": is_present(record.get("ts"), now),
            "joinedAt": joined.get(email, ""),
        })
    rows.sort(key=lambda r: (r["role"] != "trainer", not r["present"],
                             r["name"].lower(), r["email"]))
    return rows


def section_error(key, email, session):
    """Return (http_status, detail) blocking a section write, or None.

    Sections are trainer-only and limited to the two structured keys."""
    if key not in SECTION_KEYS:
        return 400, f"key must be one of: {', '.join(SECTION_KEYS)}"
    if not live_sessions.is_trainer(email, session):
        return 403, "only the trainer can edit pad sections"
    return None


def assemble_qa(entries) -> list[dict]:
    """[(stream_entry_id, fields)] → questions with their answers nested.

    A question's qid is its stream entry id; answers reference it via their
    qid field. Answers whose question is unknown are dropped (never shown
    detached). Questions and answers both keep stream (arrival) order."""
    questions, by_qid = [], {}
    for entry_id, fields in entries or []:
        kind = fields.get("type", "")
        if kind == "question":
            question = {
                "qid":     entry_id,
                "name":    fields.get("name", ""),
                "email":   fields.get("email", ""),
                "text":    fields.get("text", ""),
                "ts":      fields.get("ts", ""),
                "answers": [],
            }
            questions.append(question)
            by_qid[entry_id] = question
        elif kind == "answer":
            question = by_qid.get(fields.get("qid", ""))
            if question is not None:
                question["answers"].append({
                    "name": fields.get("name", ""),
                    "text": fields.get("text", ""),
                    "ts":   fields.get("ts", ""),
                })
    return questions


def shape_pad(sections, entries) -> dict:
    """GET /api/live/sessions/{id}/pad payload: the two structured sections
    (always present, '' when unset) + the assembled Q&A."""
    return {
        "sections": {key: (sections or {}).get(key, "")
                     for key in SECTION_KEYS},
        "qa": assemble_qa(entries),
    }


# ── Export snapshot ───────────────────────────────────────────────────────────

def _esc(text) -> str:
    return html.escape(text or "", quote=True)


def render_export(session, sections, qa, chat=()) -> str:
    """Standalone, print-friendly HTML snapshot of the Virtual Room (sections
    + full Q&A + the chat transcript) — stored as live:pad:{id}:export on the
    end/cancel transition."""
    title = session.get("title", "") or "Workshop"
    parts = [f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Virtual Room · {_esc(title)}</title>
<style>
body{{font-family:Georgia,'Times New Roman',serif;max-width:820px;margin:0 auto;
  padding:32px 24px;color:#1a1a2e;background:#fff;line-height:1.55}}
h1{{font-size:26px;border-bottom:2px solid #1a1a2e;padding-bottom:8px}}
h2{{font-size:18px;margin-top:32px;border-bottom:1px solid #ccc;padding-bottom:4px}}
.meta{{color:#555;font-size:13px;margin-bottom:24px}}
pre{{white-space:pre-wrap;word-break:break-word;background:#f6f6f4;
  border:1px solid #ddd;border-radius:4px;padding:12px;font-size:13px;
  font-family:ui-monospace,Menlo,monospace}}
.q{{margin:18px 0;page-break-inside:avoid}}
.q .who{{font-weight:bold;font-size:13px}}
.q .text{{margin:4px 0 0 0}}
.a{{margin:8px 0 0 24px;padding-left:12px;border-left:3px solid #888}}
.c{{margin:10px 0;page-break-inside:avoid}}
.c .who{{font-weight:bold;font-size:13px}}
.c .text{{margin:2px 0 0 0}}
.pin{{font-size:11px;color:#7a5c00;background:#fdf3d0;border:1px solid #e6d08a;
      border-radius:3px;padding:0 4px;margin-left:6px}}
.ts{{color:#888;font-size:11px;font-weight:normal;margin-left:6px}}
@media print{{body{{padding:0}}}}
</style>
</head>
<body>
<h1>{_esc(title)}</h1>
<div class="meta">Training: {_esc(session.get("trainingId", ""))}
 · Trainer: {_esc(session.get("trainerEmail", ""))}
 · State: {_esc(session.get("state", ""))}</div>"""]
    for key in SECTION_KEYS:
        markdown = (sections or {}).get(key, "")
        if markdown:
            parts.append(f"<h2>{_esc(key.capitalize())}</h2>\n"
                         f"<pre>{_esc(markdown)}</pre>")
    parts.append("<h2>Q&amp;A</h2>")
    if not qa:
        parts.append("<p>No questions were asked.</p>")
    for question in qa or []:
        block = (f'<div class="q"><div class="who">{_esc(question.get("name") or question.get("email", ""))}'
                 f'<span class="ts">{_esc(question.get("ts", ""))}</span></div>'
                 f'<p class="text">{_esc(question.get("text", ""))}</p>')
        for answer in question.get("answers", []):
            block += (f'<div class="a"><div class="who">{_esc(answer.get("name", ""))}'
                      f'<span class="ts">{_esc(answer.get("ts", ""))}</span></div>'
                      f'<p class="text">{_esc(answer.get("text", ""))}</p></div>')
        parts.append(block + "</div>")

    # The chat transcript is frozen alongside the Q&A so the export is the
    # whole room, not half of it. Messages a trainer cleared are already gone
    # from `chat` — a soft delete the cohort's copy must respect.
    parts.append("<h2>Chat</h2>")
    if not chat:
        parts.append("<p>No chat messages.</p>")
    for message in chat or []:
        pin = ' <span class="pin">pinned</span>' if message.get("pinned") else ""
        parts.append(
            f'<div class="c"><span class="who">'
            f'{_esc(message.get("name") or message.get("email", ""))}</span>{pin}'
            f'<span class="ts">{_esc(message.get("ts", ""))}</span>'
            f'<p class="text">{_esc(message.get("text", ""))}</p></div>')

    parts.append("</body>\n</html>")
    return "\n".join(parts)


# ── Raise hand (live teaching) ───────────────────────────────────────────────
#
# Modelled on presence, not on chat: a raised hand is STATE ("this person is
# stuck right now"), not an event. A hash keyed by email means raising twice is
# idempotent, lowering is a delete, and the trainer's board always shows the
# current set rather than a log to reconcile. Same 2-minute-style staleness
# rule does NOT apply — a hand stays up until it is lowered, because a learner
# who is stuck stays stuck whether or not their laptop is awake.

HAND_NOTE_MAX_CHARS = 280


def hand_entry(name, step="", note="", now=None):
    """The stored value for one raised hand."""
    now = now or datetime.now(timezone.utc)
    return json.dumps({
        "name": strip_html(name).strip()[:80],
        "step": strip_html(str(step)).strip()[:80],
        "note": strip_html(note).strip()[:HAND_NOTE_MAX_CHARS],
        "ts": now.astimezone(timezone.utc).isoformat(),
    })


def shape_hands(raw):
    """Redis hash (email -> JSON) -> list, oldest raised first.

    Oldest first is deliberate: it is a queue. The person who has been stuck
    longest is the one the trainer should reach next.
    """
    out = []
    for email, value in (raw or {}).items():
        try:
            data = json.loads(value)
        except (ValueError, TypeError):
            data = {}
        out.append({
            "email": email,
            "name": data.get("name") or "",
            "step": data.get("step") or "",
            "note": data.get("note") or "",
            "ts": data.get("ts") or "",
        })
    out.sort(key=lambda h: h["ts"])
    return out


# ── Broadcast ────────────────────────────────────────────────────────────────
#
# A stream, like chat: a broadcast is an event with a history the trainer may
# want in the export, and late joiners should be able to see what was already
# announced. Only the LATEST is pushed at learners as a modal — replaying every
# announcement at someone who joins at minute 50 would be a wall of dialogs.

BROADCAST_MAX_CHARS = 500


def broadcast_fields(text, name="", now=None):
    """XADD field dict for one broadcast."""
    now = now or datetime.now(timezone.utc)
    return {
        "text": strip_html(text).strip()[:BROADCAST_MAX_CHARS],
        "name": strip_html(name).strip()[:80],
        "ts": now.astimezone(timezone.utc).isoformat(),
    }


def shape_broadcasts(entries):
    """Stream entries -> list, newest LAST (chronological, like chat)."""
    out = []
    for entry_id, fields in (entries or ()):
        text = (fields or {}).get("text") or ""
        if not text:
            continue
        out.append({
            "mid": entry_id,
            "text": text,
            "name": (fields or {}).get("name") or "",
            "ts": (fields or {}).get("ts") or "",
        })
    return out


def latest_broadcast(entries):
    """The one broadcast a learner should be shown, or None."""
    shaped = shape_broadcasts(entries)
    return shaped[-1] if shaped else None
