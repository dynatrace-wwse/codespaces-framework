"""Tests for sync.core.delivery + sync check-delivery (ENH-152 step 1)."""

import textwrap
from types import SimpleNamespace

import pytest

from sync.core.delivery import (
    DeliveryError, check_delivery, check_repo, index_md_path, parse_front_matter,
)
from sync.commands import check_delivery as cmd
from sync.commands.validate import _validate_delivery


def _repo(tmp_path, front_matter=None, create=False, start=False,
          docs_dir=None, body="# Title\n"):
    if docs_dir:
        (tmp_path / "mkdocs.yaml").write_text(f"site_name: x\ndocs_dir: {docs_dir}\n")
    docs = tmp_path / (docs_dir or "docs")
    docs.mkdir(parents=True, exist_ok=True)
    text = body if front_matter is None else f"---\n{textwrap.dedent(front_matter)}---\n{body}"
    (docs / "index.md").write_text(text)
    dc = tmp_path / ".devcontainer"
    dc.mkdir(exist_ok=True)
    if create:
        (dc / "workshop-post-create.sh").write_text("#!/bin/bash\n")
    if start:
        (dc / "workshop-post-start.sh").write_text("#!/bin/bash\n")
    return tmp_path


# ── parse_front_matter ────────────────────────────────────────────────────

class TestParseFrontMatter:
    def test_none(self):
        assert parse_front_matter("# Title\n") == {}

    def test_reads_mapping(self):
        fm = parse_front_matter("---\ndescription: x\ntags: [a]\n---\n# T\n")
        assert fm == {"description": "x", "tags": ["a"]}

    def test_empty_block(self):
        assert parse_front_matter("---\n---\n# T\n") == {}

    def test_not_at_start_is_not_front_matter(self):
        assert parse_front_matter("# T\n---\ndelivery: x\n---\n") == {}

    def test_invalid_yaml_raises(self):
        with pytest.raises(DeliveryError):
            parse_front_matter("---\ndelivery: [unclosed\n---\n")

    def test_non_mapping_raises(self):
        with pytest.raises(DeliveryError):
            parse_front_matter("---\n- a\n- b\n---\n")


# ── check_delivery: the rules ─────────────────────────────────────────────

VALID = [
    # (front matter, shared, expected availability, tenancy)
    ({}, False, "self-service", "any"),                             # absent = today
    ({"description": "x"}, False, "self-service", "any"),
    ({"delivery": {}}, False, "self-service", "any"),
    ({"delivery": {"availability": "self-service"}}, False, "self-service", "any"),
    ({"delivery": {"availability": "workshop-only"}}, False, "workshop-only", "any"),
    ({"delivery": {"availability": "workshop-only", "tenancy": "multi"}}, False,
     "workshop-only", "multi"),
    ({"delivery": {"availability": "self-service", "tenancy": "single"}}, False,
     "self-service", "single"),
    ({"delivery": {"availability": "workshop-only", "tenancy": "single"}}, True,
     "workshop-only", "single"),                                     # v1 shared
]


@pytest.mark.parametrize("fm,shared,avail,ten", VALID)
def test_valid(fm, shared, avail, ten):
    eff, issues = check_delivery(fm, has_shared_create=shared)
    assert issues == []
    assert eff == {"availability": avail, "tenancy": ten, "sharedInfra": shared}


INVALID = [
    # (front matter, shared, substring every run must report)
    ({"delivery": "workshop-only"}, False, "must be a mapping"),
    ({"delivery": {"availability": "workshop"}}, False, "delivery.availability"),
    ({"delivery": {"availability": True}}, False, "delivery.availability"),  # YAML `yes`
    ({"delivery": {"tenancy": "both"}}, False, "delivery.tenancy"),
    ({"delivery": {"availabilty": "workshop-only"}}, False, "unknown key"),  # typo
    ({"delivery": {"sharedInfra": True}}, False, "unknown key"),            # derived, not declared
    # shared infrastructure ⇒ workshop-only AND single (D6, D10)
    ({}, True, "workshop-only"),
    ({}, True, "tenancy: single"),
    ({"delivery": {"availability": "self-service", "tenancy": "single"}}, True,
     "workshop-only"),
    ({"delivery": {"availability": "workshop-only"}}, True, "tenancy: single"),
    ({"delivery": {"availability": "workshop-only", "tenancy": "multi"}}, True,
     "multi-tenant shared infrastructure is not supported"),
]


@pytest.mark.parametrize("fm,shared,needle", INVALID)
def test_invalid(fm, shared, needle):
    _, issues = check_delivery(fm, has_shared_create=shared)
    assert any(needle in i for i in issues), issues


def test_bad_value_does_not_count_as_default():
    """A refused value must not leave a 'valid' effective answer behind it."""
    eff, issues = check_delivery(
        {"delivery": {"availability": "workshop-only", "tenancy": "bogus"}},
        has_shared_create=True)
    assert issues
    assert eff["tenancy"] == "any"   # default kept, and still refused for shared


def test_start_script_without_create():
    eff, issues = check_delivery({}, has_shared_create=False, has_shared_start=True)
    assert eff["sharedInfra"] is False
    assert any("without" in i for i in issues)


def test_shared_both_rules_reported_together():
    _, issues = check_delivery({}, has_shared_create=True)
    assert len(issues) == 2


# ── check_repo: files on disk ─────────────────────────────────────────────

class TestCheckRepo:
    def test_no_block_no_script(self, tmp_path):
        eff, issues = check_repo(_repo(tmp_path))
        assert issues == [] and eff["availability"] == "self-service"

    def test_shared_valid(self, tmp_path):
        r = _repo(tmp_path, "delivery:\n  availability: workshop-only\n  tenancy: single\n",
                  create=True, start=True)
        eff, issues = check_repo(r)
        assert issues == [] and eff["sharedInfra"] is True

    def test_shared_script_without_block_refused(self, tmp_path):
        _, issues = check_repo(_repo(tmp_path, create=True))
        assert len(issues) == 2

    def test_missing_index_still_checks_shared(self, tmp_path):
        """No index.md is 'no block', not 'nothing to check'."""
        (tmp_path / ".devcontainer").mkdir()
        (tmp_path / ".devcontainer/workshop-post-create.sh").write_text("")
        _, issues = check_repo(tmp_path)
        assert issues

    def test_missing_index_no_script_ok(self, tmp_path):
        assert check_repo(tmp_path)[1] == []

    def test_unparseable_front_matter(self, tmp_path):
        eff, issues = check_repo(_repo(tmp_path, "delivery: [x\n"))
        assert eff is None and "not valid YAML" in issues[0]

    def test_docs_dir_honoured(self, tmp_path):
        r = _repo(tmp_path, "delivery:\n  availability: nope\n", docs_dir="site-src")
        assert index_md_path(r) == r / "site-src" / "index.md"
        assert check_repo(r)[1]

    def test_existing_enh062_front_matter_untouched(self, tmp_path):
        r = _repo(tmp_path, 'description: "x"\ntags: [interactive, k8s]\n'
                            "difficulty: beginner\nduration: 90\n")
        assert check_repo(r)[1] == []


# ── CLI: exit code is the refusal ─────────────────────────────────────────

class TestCheckDeliveryCommand:
    def test_valid_exits_zero(self, tmp_path, capsys):
        cmd.run(SimpleNamespace(path=str(_repo(tmp_path))))
        assert "+ delivery: self-service, tenancy any, shared infra no" in capsys.readouterr().out

    def test_invalid_exits_one(self, tmp_path, capsys):
        r = _repo(tmp_path, "delivery:\n  tenancy: everyone\n")
        with pytest.raises(SystemExit) as e:
            cmd.run(SimpleNamespace(path=str(r)))
        assert e.value.code == 1
        assert "delivery.tenancy" in capsys.readouterr().out

    def test_cli_dispatch(self, tmp_path, monkeypatch):
        from sync import cli
        r = _repo(tmp_path, create=True)
        monkeypatch.setattr("sys.argv", ["sync", "check-delivery", str(r)])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 1


class TestValidateWiring:
    """sync validate prints the delivery line for each local clone."""

    def test_reports_issue(self, tmp_path, capsys):
        _validate_delivery(_repo(tmp_path, create=True))
        assert "❌ delivery: 2 issue(s)" in capsys.readouterr().out

    def test_reports_ok(self, tmp_path, capsys):
        _validate_delivery(_repo(tmp_path))
        assert "✅ delivery: self-service" in capsys.readouterr().out

    def test_validate_local_calls_it(self, tmp_path, make_repo_entry, capsys, monkeypatch):
        from sync.commands import validate
        called = []
        monkeypatch.setattr(validate, "_validate_delivery", lambda p: called.append(p))
        _repo(tmp_path)
        validate._validate_local(make_repo_entry(name="lab", repo="org/lab"), str(tmp_path))
        assert called and str(called[0]) == str(tmp_path)
