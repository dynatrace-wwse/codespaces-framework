"""Contracts between index.html and app.js that only break at runtime.

Both files are hand-edited and neither is type-checked, so the failures they
produce are silent: a `colspan` that no longer spans the table leaves a ragged
"Loading…" row, a filter input whose id app.js never reads renders fine and
does nothing, and a sub-view the switcher cannot find just never appears.

The assertions here are structural, not cosmetic — each one corresponds to a
way the page has actually been broken.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_HTML = (_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
_JS = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")


def _section(html: str, start_marker: str, end_marker: str) -> str:
    i = html.index(start_marker)
    return html[i:html.index(end_marker, i)]


# ── History tab ──────────────────────────────────────────────────────────────

HISTORY_FILTER_IDS = [
    "history-repo", "history-arch", "history-branch", "history-status",
    "history-type", "history-trigger", "history-tenant", "history-user",
    "history-daemon", "history-limit",
]


def test_every_history_filter_control_exists_in_the_markup():
    for fid in HISTORY_FILTER_IDS:
        assert f'id="{fid}"' in _HTML, f"missing control #{fid}"


def test_every_history_filter_control_is_read_by_app_js():
    # An input nothing reads looks like a working filter and silently ignores you.
    for fid in HISTORY_FILTER_IDS:
        assert f"'{fid}'" in _JS, f"#{fid} is never read by app.js"


def test_history_colspan_matches_the_header_count():
    view = _section(_HTML, '<section id="view-history"', "</section>")
    headers = len(re.findall(r"<th[ >]", view))
    for colspan in re.findall(r'colspan="(\d+)"', view):
        assert int(colspan) == headers, \
            f"history placeholder spans {colspan} of {headers} columns"
    # app.js writes its own placeholder rows and must agree with the template.
    for colspan in re.findall(r'colspan="(\d+)"[^>]*class="loading">(?:Loading history|No matching runs|Error loading history)',
                              _JS):
        assert int(colspan) == headers


def test_history_row_template_emits_one_cell_per_header():
    view = _section(_HTML, '<section id="view-history"', "</section>")
    headers = len(re.findall(r"<th[ >]", view))
    # The single `return `<tr> … </tr>`` template inside loadHistory().
    row = _section(_JS, "        tbody.innerHTML = data.rows.map(r => {", "        }).join('');")
    tr = _section(row, "return `<tr>", "</tr>`")
    assert len(re.findall(r"<td[ >]", tr)) == headers


def test_daemon_and_enablement_app_are_selectable():
    # The two filters the History tab was missing for provisioning jobs.
    assert '<option value="daemon">' in _HTML
    assert "'enablement-app'" in _JS or "enablement-app" in _JS


# ── Register tab ─────────────────────────────────────────────────────────────

def test_register_has_three_subtabs_and_three_subviews():
    view = _section(_HTML, '<section id="view-register"', "</section>")
    tabs = re.findall(r'data-reg-view="([a-z]+)"', view)
    assert tabs == ["registration", "activity", "registry"]
    for name in tabs:
        assert f'id="reg-view-{name}"' in view


def test_only_the_registration_subtab_is_public():
    """Anonymous visitors reach #view-register (PUBLIC_VIEWS). The other two
    sub-tabs carry deployer emails and account URNs, so they must be
    writer-gated in the markup as well as at the endpoint."""
    view = _section(_HTML, '<section id="view-register"', "</section>")
    for name in ("activity", "registry"):
        tag = re.search(rf'<button[^>]*data-reg-view="{name}"[^>]*>', view).group(0)
        assert "tab-writer-only" in tag, f"{name} sub-tab is not writer-gated"
    reg_tag = re.search(r'<button[^>]*data-reg-view="registration"[^>]*>', view).group(0)
    assert "tab-writer-only" not in reg_tag


def test_gated_subviews_start_hidden():
    view = _section(_HTML, '<section id="view-register"', "</section>")
    for name in ("activity", "registry"):
        tag = re.search(rf'<div id="reg-view-{name}"[^>]*>', view).group(0)
        assert "hidden" in tag


def test_register_tables_are_only_fetched_for_writers():
    # Both endpoints are writer-only; fetching them as a guest yields a 401 and
    # an empty table, which reads as "no tenants" rather than "not allowed".
    for fn in ("async function loadRegisterAudit()", "async function loadTenantRegistry()"):
        body = _section(_JS, fn, "\n}\n")
        assert "isWriter()" in body, f"{fn} does not check isWriter()"


REG_FILTER_IDS = [
    "reg-audit-user", "reg-audit-tenant", "reg-audit-stage", "reg-audit-action",
    "reg-audit-result", "reg-audit-via", "reg-audit-limit",
    "reg-tn-search", "reg-tn-stage", "reg-tn-audience", "reg-tn-via", "reg-tn-version",
]


def test_every_register_filter_control_exists_and_is_read():
    for fid in REG_FILTER_IDS:
        assert f'id="{fid}"' in _HTML, f"missing control #{fid}"
        assert f"'{fid}'" in _JS, f"#{fid} is never read by app.js"


def test_register_table_colspans_match_their_headers():
    # (table id, the app.js function that renders its placeholder rows)
    for table_id, renderers in (("reg-audit", ("loadRegisterAudit", "renderRegisterAudit")),
                                ("reg-tenants", ("loadTenantRegistry", "renderTenantRegistry"))):
        table = _section(_HTML, f'<table id="{table_id}">', "</table>")
        headers = len(re.findall(r"<th[ >]", table))
        for colspan in re.findall(r'colspan="(\d+)"', table):
            assert int(colspan) == headers, f"{table_id}: colspan {colspan} != {headers}"
        # app.js writes the sign-in hint and the empty-state row for these tables.
        for fn in renderers:
            body = _section(_JS, f"function {fn}()", "\n}\n")
            for colspan in re.findall(r'colspan="(\d+)"', body):
                assert int(colspan) == headers, \
                    f"{fn}: colspan {colspan} != {headers} on #{table_id}"


def test_registry_row_template_emits_one_cell_per_header():
    table = _section(_HTML, '<table id="reg-tenants">', "</table>")
    headers = len(re.findall(r"<th[ >]", table))
    row = _section(_JS, "? rows.map(t => `<tr>", "`).join('')")
    assert len(re.findall(r"<td[ >]", row)) == headers


# ── Cache busting ────────────────────────────────────────────────────────────

def test_static_assets_are_cache_busted():
    """The browser caches per full URL, so a deployed app.js under an unchanged
    ?v= is invisible — new markup + old script reads as 'the control renders but
    does nothing'."""
    for asset in ("style.css", "app.js"):
        assert re.search(rf"/static/{re.escape(asset)}\?v=\d+", _HTML), \
            f"{asset} is referenced without a ?v= cache buster"
