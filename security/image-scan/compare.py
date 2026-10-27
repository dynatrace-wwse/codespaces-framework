#!/usr/bin/env python3
"""compare.py — gate a grype scan of a published dt-enablement image against its baseline.

The nightly workflow (.github/workflows/image-scan.yaml) scans each published tag with grype
and runs this on the native JSON (`grype -o json`). A finding is the key
`<vulnerability id>|<package>|<version>`, counted only at Critical or High. The gate goes red
on a key that is not in the committed baseline: a new CVE, a package that changed, or a
finding reclassified upward. A key that disappears (fixed, or withdrawn) is reported and does
not fail. Refresh the baseline in a PR with --write-baseline after looking at what changed.

    compare.py --scan grype-v1.4.0.json --baseline baseline-v1.4.0.json --summary summary.json
    compare.py --scan grype-v1.4.0.json --write-baseline baseline-v1.4.0.json

Exit: 0 no new Critical/High · 2 new Critical/High (listed) ·
      1 the scan cannot be trusted (unparseable, no digest, no matches at all, or a grype DB
        older than --max-db-age-days). A broken instrument is never read as "nothing new".

Stdlib only. Reads files and writes files; no network.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone

GATED = ("Critical", "High")
SEVERITIES = ("Critical", "High", "Medium", "Low", "Negligible", "Unknown")


class Untrusted(Exception):
    """The scan file does not support a conclusion either way."""


def load_scan(path: str) -> dict:
    try:
        with open(path) as f:
            scan = json.load(f)
    except (OSError, ValueError) as e:
        raise Untrusted(f"cannot read scan {path}: {e}") from e
    if not isinstance(scan, dict) or not isinstance(scan.get("matches"), list):
        raise Untrusted(f"{path}: not grype JSON (no matches list)")
    return scan


def digest(scan: dict) -> str:
    return ((scan.get("source") or {}).get("target") or {}).get("manifestDigest") or ""


def db_built(scan: dict) -> str:
    db = (scan.get("descriptor") or {}).get("db") or {}
    return (db.get("status") or {}).get("built") or db.get("built") or ""


def finding_keys(scan: dict) -> set[str]:
    out = set()
    for m in scan["matches"]:
        v, a = m.get("vulnerability") or {}, m.get("artifact") or {}
        if v.get("severity") in GATED:
            out.add(f"{v.get('id')}|{a.get('name')}|{a.get('version')}")
    return out


def check_trust(scan: dict, max_db_age_days: float, now: datetime | None = None) -> None:
    if not digest(scan):
        raise Untrusted("scan has no image manifest digest: cannot say what was scanned")
    if not scan["matches"]:
        # Every published dt-enablement tag has hundreds of findings at some severity; zero
        # means grype read nothing (wrong platform, empty DB), not a clean image.
        raise Untrusted("scan has zero matches at any severity")
    built = db_built(scan)
    if not built:
        raise Untrusted("scan does not record when its vulnerability DB was built")
    try:
        when = datetime.fromisoformat(built.replace("Z", "+00:00"))
    except ValueError as e:
        raise Untrusted(f"unparseable DB build time {built!r}") from e
    age = ((now or datetime.now(timezone.utc)) - when).total_seconds() / 86400
    if age > max_db_age_days:
        raise Untrusted(f"grype DB built {built} is {age:.1f} days old (> {max_db_age_days})")


def summarize(scan: dict, image: str, baseline: dict | None) -> dict:
    counts = Counter((m.get("vulnerability") or {}).get("severity", "Unknown") for m in scan["matches"])
    keys = finding_keys(scan)
    base = set(baseline["keys"]) if baseline else set()
    s = {
        "image": image,
        "digest": digest(scan),
        "grype_version": (scan.get("descriptor") or {}).get("version", ""),
        "db_built": db_built(scan),
        "counts": {k: counts.get(k, 0) for k in SEVERITIES},
        "gated_keys": len(keys),
        "new_vs_baseline": sorted(keys - base) if baseline else [],
        "gone_vs_baseline": sorted(base - keys) if baseline else [],
    }
    if baseline:
        s["baseline_digest"] = baseline.get("digest", "")
        s["digest_changed"] = bool(baseline.get("digest")) and baseline["digest"] != s["digest"]
    return s


def render(s: dict) -> str:
    c = s["counts"]
    lines = [f"{s['image']}  {s['digest'][:19]}  grype {s['grype_version']}  db {s['db_built']}",
             f"  Critical {c['Critical']} · High {c['High']} · Medium {c['Medium']} · "
             f"Low {c['Low']}  ({s['gated_keys']} unique Critical/High findings)"]
    if s.get("digest_changed"):
        lines.append(f"  NOTE: tag now points at {s['digest'][:19]}, baseline was {s['baseline_digest'][:19]}")
    for k in s["new_vs_baseline"]:
        lines.append(f"  + NEW {k}")
    if s["gone_vs_baseline"]:
        lines.append(f"  - {len(s['gone_vs_baseline'])} baseline finding(s) no longer reported")
    return "\n".join(lines)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scan", required=True, help="grype native JSON")
    ap.add_argument("--image", default="", help="image reference, for the summary")
    ap.add_argument("--baseline", help="baseline JSON to gate against")
    ap.add_argument("--write-baseline", metavar="FILE", help="write this scan's keys as the baseline")
    ap.add_argument("--summary", metavar="FILE", help="write summary JSON here")
    ap.add_argument("--max-db-age-days", type=float, default=3.0)
    a = ap.parse_args(argv)
    try:
        scan = load_scan(a.scan)
        check_trust(scan, a.max_db_age_days, now)
        baseline = None
        if a.baseline:
            with open(a.baseline) as f:
                baseline = json.load(f)
            if not isinstance(baseline.get("keys"), list):
                raise Untrusted(f"{a.baseline}: no keys list")
    except Untrusted as e:
        print(f"UNTRUSTED: {e}", file=sys.stderr)
        return 1
    image = a.image or (baseline or {}).get("image", "")
    s = summarize(scan, image, baseline)
    print(render(s))
    if a.summary:
        with open(a.summary, "w") as f:
            json.dump(s, f, indent=2)
            f.write("\n")
    if a.write_baseline:
        with open(a.write_baseline, "w") as f:
            json.dump({"image": image, "digest": s["digest"], "grype_version": s["grype_version"],
                       "db_built": s["db_built"], "gated": list(GATED),
                       "keys": sorted(finding_keys(scan))}, f, indent=1)
            f.write("\n")
    return 2 if s["new_vs_baseline"] else 0


if __name__ == "__main__":
    sys.exit(main())
