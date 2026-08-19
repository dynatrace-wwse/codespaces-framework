#!/usr/bin/env bash
# Who from the APAC bootcamp follow-up has actually healed?
#
#   ./apac-followup-status.sh [ISO-cutoff]
#
# Reads Orbital's deploy audit and reports, per affected tenant, whether it has
# re-deployed since the fix shipped and what the outbound self-test said. This is
# the "automatic update that the issue is gone": an SE clicking Update now writes
# an audit row carrying `selftest`, so nobody has to be chased for a reply.
#
# Verdicts:
#   BLOCKED        self-test ran and found blocked hosts — still broken, chase it
#   WAITING        no deploy since the fix — they have not clicked Update now
#   UNPROVEN       deployed, but the app was too old to self-test (pre-1.0.351)
#   FIXED-BY-HAND  repaired directly on 2026-08-19 (Edrick, Cruz)
#   HEALED         self-test passed
#
# Note on the python below: it is passed with `-c`, NOT as a heredoc on stdin.
# `python3 - <<HEREDOC` feeds python its SCRIPT on stdin, which leaves sys.stdin
# exhausted — the first version of this script did exactly that and cheerfully
# reported every tenant as WAITING because it never read a single audit row.

set -uo pipefail
PASS=$(sudo grep -E '^REDIS_PASSWORD=' /home/ops/.env | cut -d= -f2-)
CUTOFF="${1:-2026-08-19T12:00:00}"    # when 1.0.352 went live

PYSRC=$(cat <<'PYEND'
import json, sys
cutoff = sys.argv[1]

# tenant -> (owner, group, note). Group 1 = outbound; Group 2 = delivery failures.
WATCH = {
    "bth17199": ("edrick.leong@dynatrace.com",    1, "repaired by hand 2026-08-19"),
    "uxn36332": ("cruz.lim@dynatrace.com",        1, "repaired by hand 2026-08-19"),
    "eox86326": ("jungwan.kim@dynatrace.com",     1, ""),
    "jzr21217": ("shin.aoyama@dynatrace.com",     1, ""),
    "rfy80809": ("nozomi.miyajima@dynatrace.com", 1, ""),
    "ckf69221": ("noritaka.kuroiwa@dynatrace.com",1, ""),
    "nzd75072": ("jason.nai@dynatrace.com",       1, ""),
    "uom62545": ("nalin.agrawal@dynatrace.com",   1, "needs a NEW OAuth client"),
    "epn02999": ("enric.choo@dynatrace.com",      1, "provisioned OK on the day"),
    "qlm31560": ("wilson.lai@dynatrace.com",      1, "provisioned OK on the day"),
    "zmw16947": ("siva.vadivelu@dynatrace.com",   1, "provisioned OK on the day"),
    "jxh41488": ("shiv.sivakumar@dynatrace.com",  2, "outbound block"),
    "hpm49270": ("abraham.anugrah@dynatrace.com", 2, "ActiveGate mint"),
    "bos01241": ("prasad.khamkar@dynatrace.com",  2, "needs IAM binding for documents:admin"),
}

rows_read = 0
latest = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
    except ValueError:
        continue
    rows_read += 1
    t = row.get("tenant")
    if t in WATCH and row.get("ts", "") >= cutoff:
        if t not in latest or row["ts"] > latest[t]["ts"]:
            latest[t] = row


def verdict(t):
    row = latest.get(t)
    if not row:
        return ("FIXED-BY-HAND" if "by hand" in WATCH[t][2] else "WAITING"), ""
    st = row.get("selftest") or {}
    status = st.get("status")
    if status == "ok":
        return "HEALED", "v" + str(row.get("to") or row.get("version") or "?")
    if status == "blocked":
        return "BLOCKED", ", ".join(st.get("blocked") or [])
    return "UNPROVEN", (st.get("detail") or "")[:60]


order = {"BLOCKED": 0, "WAITING": 1, "UNPROVEN": 2, "FIXED-BY-HAND": 3, "HEALED": 4}
rows = [(t, WATCH[t][0], WATCH[t][1], WATCH[t][2], *verdict(t)) for t in WATCH]
rows.sort(key=lambda r: (order[r[4]], r[2], r[0]))

if not rows_read:
    print("WARNING: read 0 audit rows — the audit is unreachable, so every verdict")
    print("         below is an artefact of no data, not a statement about a tenant.\n")

print("{:10} {:32} {:3} {:14} {}".format("TENANT", "OWNER", "GRP", "VERDICT", "DETAIL"))
for t, owner, grp, note, v, detail in rows:
    print("{:10} {:32} {:<3} {:14} {}".format(t, owner, grp, v, detail or note))

open_n = sum(1 for r in rows if r[4] in ("BLOCKED", "WAITING", "UNPROVEN"))
print("\n{} of {} closed out; {} still open.   ({} audit rows scanned, cutoff {})"
      .format(len(rows) - open_n, len(rows), open_n, rows_read, cutoff))
PYEND
)

AUDIT=$(redis-cli -a "$PASS" --no-auth-warning LRANGE audit:deploy 0 999 2>/dev/null)
python3 -c "$PYSRC" "$CUTOFF" <<<"$AUDIT"
