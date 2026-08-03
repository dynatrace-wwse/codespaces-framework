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
import re

from dashboard import live_sessions

SECTION_KEYS = ("welcome", "solutions")
ROLES = ("trainer", "learner")

QUESTION_MAX_CHARS = 2000

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


def clean_text(text, field="text") -> str:
    """Normalize a question/answer body: strip HTML + whitespace, enforce the
    2000-char cap. Raises ValueError (→ HTTP 400) when empty or too long."""
    cleaned = strip_html(text).strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > QUESTION_MAX_CHARS:
        raise ValueError(f"{field} exceeds {QUESTION_MAX_CHARS} characters")
    return cleaned


def validate_role(role) -> str:
    """'trainer' or 'learner' — anything else is a 400."""
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise ValueError("role must be 'trainer' or 'learner'")
    return role


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


def render_export(session, sections, qa) -> str:
    """Standalone, print-friendly HTML snapshot of the pad (sections + full
    Q&A) — stored as live:pad:{id}:export on the end/cancel transition."""
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
    parts.append("</body>\n</html>")
    return "\n".join(parts)
