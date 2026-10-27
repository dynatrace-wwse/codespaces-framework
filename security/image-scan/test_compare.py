"""Offline tests for compare.py: the gate must go red on a new Critical/High, stay green on
an unchanged scan, and refuse to conclude anything from a scan it cannot trust."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import compare as cmp

HERE = pathlib.Path(__file__).resolve().parent
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def m(vid, sev, pkg="openssl", ver="3.0.13"):
    return {"vulnerability": {"id": vid, "severity": sev}, "artifact": {"name": pkg, "version": ver}}


def scan(*matches, digest="sha256:" + "a" * 64, built="2026-10-27T06:30:41Z"):
    return {"matches": list(matches), "source": {"target": {"manifestDigest": digest}},
            "descriptor": {"version": "0.121.0", "db": {"status": {"built": built}}}}


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


def run(tmp_path, s, base_keys=None, extra=()):
    args = ["--scan", write(tmp_path, "scan.json", s), "--image", "img:t", *extra]
    if base_keys is not None:
        args += ["--baseline", write(tmp_path, "base.json", {"image": "img:t", "keys": base_keys})]
    return cmp.main(args, now=NOW)


def test_only_critical_and_high_are_gated():
    s = scan(m("CVE-1", "Critical"), m("CVE-2", "High", "zlib", "1.3"), m("CVE-3", "Medium"), m("CVE-4", "Low"))
    assert cmp.finding_keys(s) == {"CVE-1|openssl|3.0.13", "CVE-2|zlib|1.3"}


def test_unchanged_scan_is_green(tmp_path, capsys):
    s = scan(m("CVE-1", "Critical"), m("CVE-3", "Medium"))
    assert run(tmp_path, s, ["CVE-1|openssl|3.0.13"]) == 0
    assert "NEW" not in capsys.readouterr().out


def test_new_high_is_red_and_named(tmp_path, capsys):
    s = scan(m("CVE-1", "Critical"), m("CVE-9", "High", "curl", "8.5.0"))
    assert run(tmp_path, s, ["CVE-1|openssl|3.0.13"]) == 2
    assert "+ NEW CVE-9|curl|8.5.0" in capsys.readouterr().out


def test_new_medium_does_not_gate(tmp_path):
    s = scan(m("CVE-1", "Critical"), m("CVE-9", "Medium"))
    assert run(tmp_path, s, ["CVE-1|openssl|3.0.13"]) == 0


def test_reclassified_upward_is_new(tmp_path):
    # Same CVE was Medium when the baseline was cut, so it was never in it.
    assert run(tmp_path, scan(m("CVE-5", "High")), []) == 2


def test_package_version_change_is_new(tmp_path):
    s = scan(m("CVE-1", "Critical", ver="3.0.14"))
    assert run(tmp_path, s, ["CVE-1|openssl|3.0.13"]) == 2


def test_fixed_finding_is_reported_not_red(tmp_path, capsys):
    s = scan(m("CVE-3", "Medium"))
    assert run(tmp_path, s, ["CVE-1|openssl|3.0.13"]) == 0
    assert "1 baseline finding(s) no longer reported" in capsys.readouterr().out


def test_stale_db_is_untrusted(tmp_path, capsys):
    s = scan(m("CVE-1", "Critical"), built="2026-10-20T00:00:00Z")
    assert run(tmp_path, s, ["CVE-1|openssl|3.0.13"]) == 1
    assert "days old" in capsys.readouterr().err


def test_zero_matches_is_untrusted(tmp_path):
    assert run(tmp_path, scan(), []) == 1


def test_missing_digest_is_untrusted(tmp_path):
    assert run(tmp_path, scan(m("CVE-1", "High"), digest=""), []) == 1


def test_not_grype_json_is_untrusted(tmp_path):
    p = write(tmp_path, "x.json", {"results": []})
    assert cmp.main(["--scan", p], now=NOW) == 1


def test_untrusted_beats_new(tmp_path):
    # A stale DB with a new finding must still say "cannot conclude", not "new".
    s = scan(m("CVE-9", "Critical"), built="2026-01-01T00:00:00Z")
    assert run(tmp_path, s, []) == 1


def test_summary_and_digest_change(tmp_path):
    out = tmp_path / "summary.json"
    base = write(tmp_path, "b.json", {"image": "img:t", "digest": "sha256:" + "b" * 64, "keys": []})
    rc = cmp.main(["--scan", write(tmp_path, "s.json", scan(m("CVE-1", "High"), m("CVE-2", "Low"))),
                   "--baseline", base, "--summary", str(out)], now=NOW)
    s = json.loads(out.read_text())
    assert rc == 2
    assert s["counts"]["High"] == 1 and s["counts"]["Low"] == 1
    assert s["new_vs_baseline"] == ["CVE-1|openssl|3.0.13"]
    assert s["digest_changed"] is True and s["image"] == "img:t"


def test_write_baseline_roundtrip(tmp_path):
    s = write(tmp_path, "s.json", scan(m("CVE-1", "Critical"), m("CVE-2", "High")))
    b = tmp_path / "b.json"
    assert cmp.main(["--scan", s, "--image", "img:t", "--write-baseline", str(b)], now=NOW) == 0
    assert json.loads(b.read_text())["keys"] == ["CVE-1|openssl|3.0.13", "CVE-2|openssl|3.0.13"]
    assert cmp.main(["--scan", s, "--baseline", str(b)], now=NOW) == 0


def test_committed_baselines_are_well_formed():
    files = sorted(HERE.glob("baseline-*.json"))
    assert {f.name for f in files} == {"baseline-v1.3.2.json", "baseline-v1.4.0.json"}
    for f in files:
        b = json.loads(f.read_text())
        tag = f.stem.removeprefix("baseline-")
        assert b["image"] == f"shinojosa/dt-enablement:{tag}"
        assert b["digest"].startswith("sha256:")
        assert b["keys"] == sorted(set(b["keys"])) and b["keys"]
        assert all(k.count("|") == 2 for k in b["keys"])


def test_workflow_scans_exactly_the_baselined_tags():
    wf = (HERE.parents[1] / ".github/workflows/image-scan.yaml").read_text()
    for f in HERE.glob("baseline-*.json"):
        assert f.stem.removeprefix("baseline-") in wf
