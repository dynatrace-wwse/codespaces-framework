/* Enablement Ops Dashboard — Client */

const API = '';

// Auth state — combines oauth2-proxy /oauth2/userinfo (am I signed in?) with
// the dashboard's /api/auth/role (am I a writer or guest?). Only writers can
// execute actions; guests are read-only across the whole UI including the
// Synchronizer tab.
const authState = {
    signedIn: false,
    user: null,
    role: 'guest',     // 'writer' | 'guest'
    orgRole: '',       // 'admin' | 'member' | ''
};

function isWriter() { return authState.role === 'writer'; }

async function loadAuthState() {
    try {
        const res = await fetch('/oauth2/userinfo', { credentials: 'same-origin' });
        if (res.ok) {
            const data = await res.json();
            authState.signedIn = true;
            authState.user = data.user || data.preferredUsername || data.email || 'signed in';
        } else {
            authState.signedIn = false;
        }
    } catch {
        authState.signedIn = false;
    }
    // Resolve role separately — even a signed-in user could fail the org check.
    try {
        const res = await fetch('/api/auth/role', { credentials: 'same-origin' });
        if (res.ok) {
            const data = await res.json();
            authState.role = data.role || 'guest';
            authState.orgRole = data.org_role || '';
            if (data.user) authState.user = data.user;
        }
    } catch {
        authState.role = 'guest';
    }
    renderAuthHeader();
    applyRoleGating();
}

function renderAuthHeader() {
    const signInBtn = document.getElementById('sign-in-btn');
    const userInfo = document.getElementById('user-info');
    const userName = document.getElementById('user-name');
    if (authState.signedIn) {
        signInBtn.hidden = true;
        userInfo.hidden = false;
        const roleLabel = isWriter()
            ? `<span class="role-badge writer" title="Org member — actions enabled">writer</span>`
            : `<span class="role-badge guest" title="Read-only — sign in as an org member to execute actions">guest</span>`;
        userName.innerHTML = escapeHtml(authState.user) + ' ' + roleLabel;
    } else {
        signInBtn.hidden = false;
        userInfo.hidden = true;
    }
}

// Toggle a body class for CSS-driven guest gating. We don't touch the
// disabled attribute on individual elements — that would clobber legitimate
// state-based disabling (e.g. "no branch selected", "trigger in flight"). CSS
// rule body.role-guest [data-action] disables interaction for guests.
function applyRoleGating() {
    const writer = isWriter();
    document.body.classList.toggle('role-guest', !writer);
    document.body.classList.toggle('role-writer', writer);
}

// ── Tab Navigation ──────────────────────────────────────────────────────────

function activateTab(view) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const tab = document.querySelector(`.tab[data-view="${view}"]`);
    if (!tab) return;
    tab.classList.add('active');
    document.getElementById(`view-${view}`).classList.add('active');
    location.hash = view;
    if (view === 'history') loadHistory();
    if (view === 'running') loadRunningDetail();
    if (view === 'sync') loadSyncTab();
    if (view === 'agentic') loadAgentic();
}

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => activateTab(tab.dataset.view));
});

// Restore tab from URL hash on load
(function () {
    const hash = location.hash.replace('#', '');
    if (hash && document.querySelector(`.tab[data-view="${hash}"]`)) activateTab(hash);
})();

// ── Health Check ────────────────────────────────────────────────────────────

async function checkHealth() {
    try {
        const res = await fetch(`${API}/api/health`);
        const data = await res.json();
        const badge = document.getElementById('health-status');
        if (data.status === 'healthy') {
            badge.textContent = `healthy | ${data.workers} workers`;
            badge.className = 'health-badge healthy';
        } else {
            badge.textContent = 'unhealthy';
            badge.className = 'health-badge unhealthy';
        }
    } catch {
        const badge = document.getElementById('health-status');
        badge.textContent = 'unreachable';
        badge.className = 'health-badge unhealthy';
    }
}

// ── Fleet View ──────────────────────────────────────────────────────────────

// repo+arch → { job_id, ref, started_at } when a test is currently running.
// Refreshed every poll so we can show spinners.
let runningMap = {};

async function loadRunning() {
    try {
        const res = await fetch(`${API}/api/builds/running`);
        const data = await res.json();
        runningMap = {};
        for (const r of (data.running || [])) {
            runningMap[`${r.repo}|${r.arch}`] = r;
        }
        // Update spinners on existing rows without re-rendering everything
        document.querySelectorAll('tr[data-repo]').forEach(row => {
            const repo = row.dataset.repo;
            for (const arch of ['arm64', 'amd64']) {
                const cell = row.querySelector(`td[data-arch="${arch}"]`);
                if (!cell) continue;
                const isRunning = !!runningMap[`${repo}|${arch}`];
                cell.classList.toggle('running', isRunning);
                let spinner = cell.querySelector('.running-dot');
                if (isRunning && !spinner) {
                    const r = runningMap[`${repo}|${arch}`];
                    cell.insertAdjacentHTML('afterbegin',
                        `<a class="running-dot log-link" title="Running — view live log" href="#"
                            data-job-id="${r.job_id}" data-arch="${arch}">●</a> `);
                } else if (!isRunning && spinner) {
                    spinner.remove();
                }
            }
        });
    } catch {}
}

// Cache of latest fleet rows for client-side filtering. Keyed by repo, holds
// the {arm, amd} build objects so we can apply status/branch filters without
// refetching.
let fleetRowsByRepo = {};

async function loadFleet() {
    const res = await fetch(`${API}/api/repos`);
    const data = await res.json();
    const tbody = document.getElementById('fleet-body');

    fleetRowsByRepo = {};
    tbody.innerHTML = data.repos.map(repo => {
        fleetRowsByRepo[repo.repo] = repo;
        const arm = repo.builds.arm64;
        const amd = repo.builds.amd64;
        const armHistory = (repo.history || {}).arm64 || [];
        const amdHistory = (repo.history || {}).amd64 || [];
        const safeRepo = repo.repo.replace(/[^a-z0-9-]/gi, '_');
        const repoUrl = `https://github.com/${repo.repo}`;
        const ghPagesUrl = `https://${repo.repo.split('/')[0]}.github.io/${repo.repo.split('/')[1]}/`;
        const repoShort = repo.repo.split('/').pop();
        const tag = repo.latest_tag || '';
        const releaseLabel = tag ? escapeHtml(tag) : 'no releases';
        const releaseClass = tag ? 'repo-action repo-action-release' : 'repo-action repo-action-release repo-action-norelease';
        return `<tr data-repo="${repo.repo}">
            <td class="fleet-repo-cell">
                <a class="fleet-repo-link" href="${repoUrl}" target="_blank" rel="noopener"
                   title="Open ${repo.repo} on GitHub">
                    <strong>${escapeHtml(repo.name)}</strong>
                    <span class="fleet-repo-org">${escapeHtml(repo.repo)}</span>
                </a>
                <div class="fleet-repo-actions">
                    <a class="repo-action" href="${repoUrl}/issues" target="_blank" rel="noopener" title="Open issues">Issues</a>
                    <a class="repo-action" href="${repoUrl}/pulls" target="_blank" rel="noopener" title="Open pull requests">PRs</a>
                    <a class="repo-action" href="${repoUrl}/actions" target="_blank" rel="noopener" title="GitHub Actions">Actions</a>
                    <a class="repo-action" href="${ghPagesUrl}" target="_blank" rel="noopener" title="GitHub Pages docs">Docs</a>
                    <a class="${releaseClass}" href="${repoUrl}/releases" target="_blank" rel="noopener" title="Releases">${releaseLabel}</a>
                </div>
            </td>
            <td><span class="arch-badge">${repo.arch}</span></td>
            <td data-arch="arm64">${buildCell(arm, armHistory)}</td>
            <td data-arch="amd64">${buildCell(amd, amdHistory)}</td>
            <td>
                <select class="branch-select" id="branch-${safeRepo}"
                        data-repo="${repo.repo}" data-loaded="0">
                    <option value="main" selected>main</option>
                </select>
            </td>
            <td>
                <div class="trigger-form">
                <select class="action-select" id="action-${safeRepo}"
                        onchange="onRowActionChange('${safeRepo}')">
                    <option value="integration-test">Integration test</option>
                    <option value="deploy-ghpages">Deploy pages</option>
                    <option value="daemon">Training</option>
                </select>
                <select class="arch-select" id="arch-${safeRepo}" data-action>
                    <option value="both">both</option>
                    <option value="arm64">arm64</option>
                    <option value="amd64">amd64</option>
                </select>
                <button class="btn btn-small" data-action
                        onclick="triggerBuildFromRow('${repo.repo}', '${safeRepo}', this)">
                    Trigger
                </button>
                </div>
            </td>
        </tr>`;
    }).join('');

    // Filter handlers
    const filt = document.getElementById('repo-filter');
    filt.oninput = applyFleetFilters;

    // Lazy-load branches when a branch dropdown is opened
    tbody.querySelectorAll('.branch-select').forEach(sel => {
        sel.addEventListener('mousedown', loadBranchesForSelect, { once: true });
        sel.addEventListener('focus',     loadBranchesForSelect, { once: true });
    });

    // Re-apply gating now that buttons exist
    applyRoleGating();
    applyFleetFilters();

    // Wire spinners that already exist on first paint
    await loadRunning();
}

// ── Fleet filters (status, branch, repo, arch) ──────────────────────────────
function applyFleetFilters() {
    const repoFilter   = (document.getElementById('repo-filter')?.value || '').toLowerCase();
    const archFilter   = document.getElementById('arch-filter')?.value || 'all';
    const statusFilter = document.getElementById('fleet-status-filter')?.value || 'all';
    const branchFilter = document.getElementById('fleet-branch-filter')?.value || '';

    document.querySelectorAll('#fleet-body tr[data-repo]').forEach(row => {
        const repoFull = row.dataset.repo;
        const meta = fleetRowsByRepo[repoFull];
        if (!meta) { row.style.display = ''; return; }

        const arm = meta.builds.arm64, amd = meta.builds.amd64;
        const arches = [];
        if (arm) arches.push(arm);
        if (amd) arches.push(amd);

        // Repo text match
        if (repoFilter && !row.textContent.toLowerCase().includes(repoFilter)) {
            row.style.display = 'none'; return;
        }
        // Arch toggle: meta.arch is the configured arches ('arm64'|'amd64'|'both')
        if (archFilter !== 'all' && meta.arch !== archFilter) {
            row.style.display = 'none'; return;
        }
        // Status filter — applies to "best" recent build across both arches
        if (statusFilter !== 'all') {
            if (statusFilter === 'never-run') {
                if (arches.length) { row.style.display = 'none'; return; }
            } else if (statusFilter === 'passed') {
                if (!arches.some(b => b.passed && (b.status || 'completed') !== 'terminated')) {
                    row.style.display = 'none'; return;
                }
            } else if (statusFilter === 'failed') {
                // anyone failed (and not terminated)
                if (!arches.some(b => !b.passed && (b.status || 'completed') !== 'terminated')) {
                    row.style.display = 'none'; return;
                }
            } else if (statusFilter === 'terminated') {
                if (!arches.some(b => b.status === 'terminated')) {
                    row.style.display = 'none'; return;
                }
            }
        }
        // Branch filter — branch is set per row's branch dropdown
        if (branchFilter) {
            const sel = row.querySelector('.branch-select');
            const cur = sel ? sel.value : 'main';
            if (cur !== branchFilter) { row.style.display = 'none'; return; }
        }
        row.style.display = '';
    });
}

// ── Cross-repo branch trigger ───────────────────────────────────────────────
// Pulls the union of branches across active repos and lets a writer push a
// build for that branch to every repo that has it. Most useful for fan-out
// validation of feature branches like "fix/badges-and-rum-ids" that span the
// fleet.
let branchesAggCache = null;

async function loadFleetTriggerPanel() {
    const panel = document.getElementById('fleet-trigger-panel');
    if (!panel) return;
    panel.hidden = false;
    try {
        const res = await fetch(`${API}/api/branches/all`);
        if (!res.ok) return;
        const data = await res.json();
        branchesAggCache = data;
        const sel = document.getElementById('fleet-branch');
        const filterSel = document.getElementById('fleet-branch-filter');
        // Populate cross-repo trigger dropdown — annotate with repo count
        sel.innerHTML = `<option value="">Select a branch…</option>` +
            data.branches.map(b =>
                `<option value="${escapeHtml(b.name)}">${escapeHtml(b.name)} · ${b.count} repo${b.count === 1 ? '' : 's'}</option>`
            ).join('');
        // Populate fleet branch filter (no repo count — just names)
        const seenBranches = data.branches.map(b => b.name);
        filterSel.innerHTML = `<option value="">All branches (selected)</option>` +
            seenBranches.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('');
    } catch {}

    // Wire action dropdown to toggle arch visibility and update help/button text
    const actionSel = document.getElementById('fleet-action');
    if (actionSel) actionSel.addEventListener('change', onFleetActionChange);
}

function onFleetActionChange() {
    const action  = document.getElementById('fleet-action')?.value || 'integration-test';
    const archSel = document.getElementById('fleet-arch');
    const helpEl  = document.getElementById('fleet-trigger-help');
    const btn     = document.getElementById('fleet-trigger-btn');
    if (action === 'deploy-ghpages') {
        if (archSel) archSel.hidden = true;
        if (helpEl)  helpEl.textContent = 'Dispatch the deploy-ghpages.yaml workflow on every repo that has the chosen branch.';
        if (btn)     btn.textContent    = 'Deploy fleet pages';
    } else {
        if (archSel) archSel.hidden = false;
        if (helpEl)  helpEl.textContent = 'Run integration tests on every repo that has the chosen branch.';
        if (btn)     btn.textContent    = 'Trigger fleet build';
    }
}

async function triggerFleetBuild() {
    if (!isWriter()) {
        if (!authState.signedIn) {
            window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
        } else {
            alert('Only org members can trigger fleet builds.');
        }
        return;
    }
    const branch = document.getElementById('fleet-branch').value;
    const arch   = document.getElementById('fleet-arch').value;
    const action = document.getElementById('fleet-action')?.value || 'integration-test';
    if (!branch) { alert('Select a branch first.'); return; }

    const meta  = (branchesAggCache?.branches || []).find(b => b.name === branch);
    const count = meta?.count || 0;
    const isDeployPages = action === 'deploy-ghpages';
    const confirmMsg = isDeployPages
        ? `Queue deploy-ghpages for branch "${branch}" on ${count} repo${count === 1 ? '' : 's'}?\n\nEach repo will run mkdocs build + gh-deploy locally.`
        : `Trigger an integration test for branch "${branch}" on ${count} repo${count === 1 ? '' : 's'} (${arch})?\n\nEach repo will be queued; per-(repo,branch,arch) locks still apply.`;
    if (!confirm(confirmMsg)) return;

    const btn = document.getElementById('fleet-trigger-btn');
    btn.disabled = true;
    btn.textContent = 'Queueing…';
    try {
        if (isDeployPages) {
            const res = await fetch(`${API}/api/ghpages/trigger-fleet`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ branch }),
            });
            if (res.status === 401) {
                window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
                return;
            }
            if (!res.ok) {
                const body = await res.text();
                alert(`Fleet deploy failed (${res.status}): ${body}`);
                return;
            }
            const data = await res.json();
            const errCount = (data.errors || []).length;
            const skipCount = (data.skipped_no_branch || []).length;
            alert(
                `Queued deploy-ghpages on ${data.dispatched_count} repo(s) for branch "${data.branch}".` +
                (errCount  ? `\n${errCount} error(s).` : '') +
                (skipCount ? `\n${skipCount} repo(s) skipped (branch not present).` : '')
            );
        } else {
            const res = await fetch(`${API}/api/builds/trigger-fleet`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ branch, arch }),
            });
            if (res.status === 401) {
                window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
                return;
            }
            if (!res.ok) {
                const body = await res.text();
                alert(`Fleet trigger failed (${res.status}): ${body}`);
                return;
            }
            const data = await res.json();
            const skipped = (data.skipped_no_branch || []).length;
            alert(
                `Queued ${data.queued.length} job(s) for branch ${data.branch}.` +
                (skipped ? `\n${skipped} repo(s) skipped (branch not present).` : '')
            );
            loadRunning();
        }
    } finally {
        btn.disabled = false;
        // Restore label based on current action selection (user may not have changed it)
        onFleetActionChange();
        applyRoleGating();
    }
}

async function loadBranchesForSelect(e) {
    const sel = e.currentTarget || e.target;
    if (sel.dataset.loaded === '1') return;
    const repo = sel.dataset.repo;
    if (!repo) return;
    sel.dataset.loaded = '1';
    try {
        const res = await fetch(`${API}/api/repos/${repo}/branches`);
        if (!res.ok) return;
        const data = await res.json();
        const current = sel.value;
        sel.innerHTML = data.branches.map(b =>
            `<option value="${escapeHtml(b)}"${b === current ? ' selected' : ''}>${escapeHtml(b)}</option>`
        ).join('');
    } catch {
        // Keep the existing main option, mark for retry next time
        sel.dataset.loaded = '0';
    }
}

function buildCell(build, history) {
    let html = '';
    if (!build) {
        html = '<span class="status-none">—</span>';
    } else {
        let cls, icon;
        if (build.status === 'terminated') {
            cls = 'status-terminated'; icon = 'TERM';
        } else if (build.passed) {
            cls = 'status-pass'; icon = 'PASS';
        } else {
            cls = 'status-fail'; icon = 'FAIL';
        }
        if (build.job_id) {
            html = `<a href="#" class="${cls} log-link"
                       data-final-job="${build.job_id}"
                       title="View worker log (status: ${build.status || (build.passed ? 'completed' : 'failed')})">${icon}</a>`;
        } else if (build.run_url) {
            html = `<a href="${build.run_url}" target="_blank" rel="noopener" class="${cls} log-link" title="View run on GitHub Actions">${icon}</a>`;
        } else {
            html = `<span class="${cls}">${icon}</span>`;
        }
    }
    if (history && history.length > 0) {
        html += '<div class="build-spark">' + renderBuildSpark(history) + '</div>';
    }
    return html;
}

function renderBuildSpark(history) {
    // history is newest-first; render oldest-first (left to right)
    return [...history].reverse().map(h => {
        const isTerminated = h.status === 'terminated';
        const statusKey = isTerminated ? 'term' : (h.passed ? 'pass' : 'fail');
        const sym = isTerminated ? '–' : '|';
        const cls = `spark-bar spark-${statusKey}`;
        const label = escapeHtml(`${statusKey.toUpperCase()} · ${formatTime(h.finished_at)}`);
        if (h.job_id) {
            return `<a href="#" class="${cls}" data-final-job="${h.job_id}" title="${label}">${sym}</a>`;
        }
        return `<span class="${cls}" title="${label}">${sym}</span>`;
    }).join('');
}

function onRowActionChange(safeRepo) {
    const action  = document.getElementById(`action-${safeRepo}`)?.value || 'integration-test';
    const archSel = document.getElementById(`arch-${safeRepo}`);
    if (archSel) archSel.hidden = action === 'deploy-ghpages';
}

async function triggerBuildFromRow(repo, safeRepo, btn) {
    if (!authState.signedIn) {
        window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
        return;
    }
    if (!isWriter()) {
        alert('Only org members can trigger builds. You are signed in as a guest.');
        return;
    }
    const branch = document.getElementById(`branch-${safeRepo}`).value.trim() || 'main';
    const arch   = document.getElementById(`arch-${safeRepo}`)?.value || 'both';
    const action = document.getElementById(`action-${safeRepo}`)?.value || 'integration-test';
    btn.disabled = true; btn.textContent = '…';

    if (action === 'deploy-ghpages') {
        try {
            const res = await fetch(`${API}/api/ghpages/trigger`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ repo, ref: branch }),
            });
            if (res.status === 401) {
                window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
                return;
            }
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                alert(`Deploy Pages failed: ${body.detail || 'HTTP ' + res.status}`);
                btn.disabled = false; btn.textContent = 'Trigger';
            } else {
                btn.textContent = '✓ Sent';
                setTimeout(() => { btn.disabled = false; btn.textContent = 'Trigger'; }, 2000);
            }
        } catch (e) {
            btn.disabled = false; btn.textContent = 'Trigger';
            alert('Network error: ' + e);
        }
    } else {
        try {
            const res = await fetch(`${API}/api/builds/trigger`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ repo, arch, ref: branch, type: action, requested_by: 'dashboard' }),
            });
            if (res.status === 401) {
                window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
                return;
            }
            if (!res.ok) {
                alert('Trigger failed: HTTP ' + res.status);
            }
        } finally {
            btn.disabled = false; btn.textContent = 'Trigger';
            await loadRunning();
        }
    }
}

// ── ANSI colour rendering ───────────────────────────────────────────────────
// Convert raw ANSI escape sequences (\x1b[...m) into <span style="...">
// so the log retains the same coloring you'd see in a terminal.

const ANSI_BASIC = {
    '30':'#000000', '31':'#cd3131', '32':'#0dbc79', '33':'#e5e510',
    '34':'#2472c8', '35':'#bc3fbc', '36':'#11a8cd', '37':'#e5e5e5',
    '90':'#666666', '91':'#f14c4c', '92':'#23d18b', '93':'#f5f543',
    '94':'#3b8eea', '95':'#d670d6', '96':'#29b8db', '97':'#ffffff',
};

function ansi256(n) {
    if (n < 16) return ANSI_BASIC[String(n < 8 ? 30 + n : 90 + (n - 8))];
    if (n < 232) {
        n -= 16;
        const r = Math.floor(n / 36) * 51;
        const g = Math.floor((n / 6) % 6) * 51;
        const b = (n % 6) * 51;
        return `rgb(${r},${g},${b})`;
    }
    const v = (n - 232) * 10 + 8;
    return `rgb(${v},${v},${v})`;
}

function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function ansiToHtml(text) {
    let out = '';
    let i = 0;
    let openSpans = 0;
    // Match \x1b[...m
    const re = /\x1b\[([0-9;]*)m/g;
    let m;
    while ((m = re.exec(text)) !== null) {
        out += escapeHtml(text.slice(i, m.index));
        i = m.index + m[0].length;
        const codes = m[1].split(';').filter(c => c !== '');
        if (codes.length === 0 || codes[0] === '0') {
            while (openSpans-- > 0) out += '</span>';
            openSpans = 0;
            continue;
        }
        let color = null, bold = false, j = 0;
        while (j < codes.length) {
            const c = codes[j];
            if (c === '0') {
                while (openSpans-- > 0) out += '</span>';
                openSpans = 0;
            } else if (c === '1') {
                bold = true;
            } else if (c === '38' && codes[j+1] === '5' && codes[j+2] !== undefined) {
                color = ansi256(parseInt(codes[j+2], 10));
                j += 2;
            } else if (ANSI_BASIC[c]) {
                color = ANSI_BASIC[c];
            }
            j++;
        }
        if (color || bold) {
            const styles = [];
            if (color) styles.push('color:' + color);
            if (bold) styles.push('font-weight:bold');
            out += `<span style="${styles.join(';')}">`;
            openSpans++;
        }
    }
    out += escapeHtml(text.slice(i));
    while (openSpans-- > 0) out += '</span>';
    return out;
}

// ── Live log modal ──────────────────────────────────────────────────────────

let livelogPoll = null;
let currentJobId = null;
let currentJobIsLive = false;
let livelogAppTabsLoaded = false;
let livelogPollCount = 0;
// Most-recent rendered raw text (after ANSI processing). The search bar
// re-highlights against this whenever the log refreshes or the query
// changes, so search-state survives polling without losing position.
let currentLogText = '';
let currentLogHtml = '';
let currentSearchTerm = '';
let currentSearchIdx = -1;
let currentSearchTotal = 0;
const WRAP_KEY = 'livelog-wrap';

function getWrapPref() {
    // Default: noWrap. Long lines stay on one line so structure is preserved;
    // user can toggle to wrap with the button or the "W" hotkey.
    return localStorage.getItem(WRAP_KEY) === '1';
}

function applyWrapPref() {
    const pre = document.getElementById('livelog-pre');
    if (!pre) return;
    const wrap = getWrapPref();
    pre.classList.toggle('nowrap', !wrap);
    const btn = document.getElementById('livelog-wrap-toggle');
    if (btn) btn.textContent = wrap ? '↩ Wrap' : '→ NoWrap';
}

function openLiveLog(jobId, title) {
    currentJobId = jobId;
    currentJobIsLive = false;
    livelogAppTabsLoaded = false;
    livelogPollCount = 0;
    currentSearchTerm = '';
    currentSearchIdx = -1;
    currentSearchTotal = 0;
    const searchInput = document.getElementById('livelog-search');
    if (searchInput) searchInput.value = '';
    document.getElementById('livelog-search-count').textContent = '';
    document.getElementById('livelog-title').textContent = title;
    document.getElementById('livelog-app-tabs').innerHTML = '';
    const livelogFrame = document.getElementById('livelog-app-frame');
    livelogFrame.style.display = 'none';
    livelogFrame.src = '';
    const pre = document.getElementById('livelog-pre');
    pre.style.display = '';
    pre.innerHTML = '<em style="color:var(--text-muted)">Initializing isolation container…</em>';
    document.getElementById('livelog-modal').hidden = false;
    applyWrapPref();

    // Wire fullscreen + terminate + shell buttons for this job
    const fsBtn = document.getElementById('livelog-fullscreen');
    if (fsBtn) fsBtn.href = `/log/${jobId}`;
    const termBtn = document.getElementById('livelog-terminate');
    if (termBtn) termBtn.hidden = true;  // unhide once we confirm livelog (running)
    const shellBtn = document.getElementById('livelog-shell');
    if (shellBtn) shellBtn.hidden = true;  // unhide once we confirm livelog (running)

    if (livelogPoll) clearInterval(livelogPoll);

    const fetchOnce = async () => {
        try {
            // Try livelog first (running). 404 → fall back to final log + stop polling.
            let res = await fetch(`/api/jobs/${jobId}/livelog`);
            if (res.status === 404) {
                res = await fetch(`/api/jobs/${jobId}/log`);
                if (livelogPoll) { clearInterval(livelogPoll); livelogPoll = null; }
                currentJobIsLive = false;
                if (termBtn) termBtn.hidden = true;
                if (shellBtn) shellBtn.hidden = true;
            } else if (res.ok) {
                currentJobIsLive = true;
                if (termBtn) termBtn.hidden = !isWriter();
                if (shellBtn) shellBtn.hidden = !isWriter();
                livelogPollCount++;
                // Load on first live poll; refresh every ~30 s (15 × 2 s) to pick up new apps.
                if (!livelogAppTabsLoaded || livelogPollCount % 15 === 0) {
                    livelogAppTabsLoaded = true;
                    _loadLivelogAppTabs(jobId);
                }
            }
            if (res.ok) {
                const text = await res.text();
                const wasAtBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
                currentLogText = text;
                currentLogHtml = ansiToHtml(text);
                renderLogWithSearch();
                if (wasAtBottom && !currentSearchTerm) pre.scrollTop = pre.scrollHeight;
            }
        } catch {}
    };
    fetchOnce();
    livelogPoll = setInterval(fetchOnce, 2000);
}

function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function renderLogWithSearch(scrollToMatch = false) {
    const pre = document.getElementById('livelog-pre');
    if (!pre) return;
    if (!currentSearchTerm) {
        pre.innerHTML = currentLogHtml;
        currentSearchTotal = 0;
        currentSearchIdx = -1;
        document.getElementById('livelog-search-count').textContent = '';
        return;
    }
    // Highlight matches against the rendered HTML's text content. Build
    // text→HTML by re-running ANSI rendering with case-insensitive markers
    // around matches in the source text, then re-rendering. Simpler: run
    // ANSI→HTML, then walk text nodes inserting <mark>.
    const tmp = document.createElement('div');
    tmp.innerHTML = currentLogHtml;
    const re = new RegExp(escapeRegex(currentSearchTerm), 'gi');
    let total = 0;
    function walk(node) {
        if (node.nodeType === 3) { // Text
            const t = node.nodeValue;
            if (!re.test(t)) return;
            re.lastIndex = 0;
            const frag = document.createDocumentFragment();
            let last = 0, m;
            while ((m = re.exec(t)) !== null) {
                if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
                const mark = document.createElement('mark');
                mark.className = 'log-match';
                mark.textContent = m[0];
                mark.dataset.matchIdx = String(total);
                frag.appendChild(mark);
                total += 1;
                last = m.index + m[0].length;
                if (m[0].length === 0) re.lastIndex++; // safety
            }
            if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
            node.parentNode.replaceChild(frag, node);
        } else {
            // Walk children (snapshot first because we mutate)
            const kids = Array.from(node.childNodes);
            kids.forEach(walk);
        }
    }
    walk(tmp);
    pre.innerHTML = '';
    while (tmp.firstChild) pre.appendChild(tmp.firstChild);
    currentSearchTotal = total;
    if (total === 0) {
        currentSearchIdx = -1;
        document.getElementById('livelog-search-count').textContent = '0 / 0';
        return;
    }
    if (currentSearchIdx < 0 || currentSearchIdx >= total) currentSearchIdx = 0;
    highlightCurrentSearchMatch(scrollToMatch);
}

function highlightCurrentSearchMatch(scroll = true) {
    const marks = document.querySelectorAll('#livelog-pre mark.log-match');
    marks.forEach(m => m.classList.remove('current'));
    document.getElementById('livelog-search-count').textContent =
        currentSearchTotal ? `${currentSearchIdx + 1} / ${currentSearchTotal}` : '0 / 0';
    if (!marks.length) return;
    const cur = marks[currentSearchIdx];
    if (!cur) return;
    cur.classList.add('current');
    if (scroll) cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function moveSearch(delta) {
    if (!currentSearchTotal) return;
    currentSearchIdx = (currentSearchIdx + delta + currentSearchTotal) % currentSearchTotal;
    highlightCurrentSearchMatch(true);
}

function onSearchInput(e) {
    currentSearchTerm = e.target.value;
    currentSearchIdx = 0;
    renderLogWithSearch(true);
}

function toggleWrap() {
    const cur = getWrapPref();
    localStorage.setItem(WRAP_KEY, cur ? '0' : '1');
    applyWrapPref();
}

function closeLiveLog() {
    document.getElementById('livelog-modal').hidden = true;
    if (livelogPoll) { clearInterval(livelogPoll); livelogPoll = null; }
    document.getElementById('livelog-app-tabs').innerHTML = '';
    const livelogFrame = document.getElementById('livelog-app-frame');
    livelogFrame.style.display = 'none';
    livelogFrame.src = '';
    document.getElementById('livelog-app-empty').style.display = 'none';
    document.getElementById('livelog-pre').style.display = '';
    currentJobId = null;
    currentJobIsLive = false;
    livelogAppTabsLoaded = false;
    livelogPollCount = 0;
}

async function terminateCurrentJob() {
    if (!currentJobId || !currentJobIsLive) return;
    if (!isWriter()) {
        alert('Only org members can terminate jobs.');
        return;
    }
    if (!confirm(`Terminate job ${currentJobId}?\n\nThis kills the test container and marks the job as 'terminated'.`)) return;
    const termBtn = document.getElementById('livelog-terminate');
    if (termBtn) { termBtn.disabled = true; termBtn.textContent = 'Terminating…'; }
    try {
        const res = await fetch(`/api/jobs/${currentJobId}/terminate`, { method: 'POST' });
        if (!res.ok) {
            const body = await res.text();
            alert(`Termination failed (${res.status}): ${body}`);
        }
    } catch (e) {
        alert('Network error requesting termination: ' + e);
    } finally {
        if (termBtn) { termBtn.disabled = false; termBtn.textContent = '■ Terminate'; }
    }
}

document.addEventListener('click', e => {
    if (e.target.id === 'livelog-close') { closeLiveLog(); return; }
    if (e.target.id === 'livelog-terminate') { terminateCurrentJob(); return; }
    if (e.target.id === 'livelog-shell') {
        if (currentJobId) openShell(currentJobId, document.getElementById('livelog-title').textContent);
        return;
    }
    if (e.target.id === 'livelog-wrap-toggle') { toggleWrap(); return; }
    if (e.target.id === 'livelog-search-prev') { moveSearch(-1); return; }
    if (e.target.id === 'livelog-search-next') { moveSearch(1); return; }
    if (e.target.id === 'fleet-trigger-btn') { triggerFleetBuild(); return; }

    // Running dot (green ●) → open live-tailing modal
    const spin = e.target.closest('a.running-dot');
    if (spin && spin.dataset.jobId) {
        e.preventDefault();
        const row = spin.closest('tr');
        const repo = row ? row.dataset.repo : '';
        openLiveLog(spin.dataset.jobId, `${repo} (${spin.dataset.arch})`);
        return;
    }

    // Final PASS/FAIL link → open same modal with the historical log
    const finalLink = e.target.closest('a[data-final-job]');
    if (finalLink) {
        e.preventDefault();
        const row = finalLink.closest('tr');
        const repo = (row && row.dataset.repo) || '';
        const arch = (finalLink.closest('td')?.dataset.arch) || '';
        const title = repo
            ? `${repo}${arch ? ' (' + arch + ')' : ''}`
            : finalLink.dataset.finalJob;
        openLiveLog(finalLink.dataset.finalJob, title);
        return;
    }

    // ESC closes the modal — handled separately, but treat backdrop click as close
    if (e.target.id === 'livelog-modal') closeLiveLog();
});

// ESC closes the live-log modal; Enter/Shift+Enter walk search matches when
// focus is in the search box; "/" focuses the search bar; "w" toggles wrap.
document.addEventListener('keydown', e => {
    const modal = document.getElementById('livelog-modal');
    if (!modal || modal.hidden) return;
    if (e.key === 'Escape') { closeLiveLog(); return; }
    const inSearch = e.target && e.target.id === 'livelog-search';
    if (inSearch && e.key === 'Enter') {
        e.preventDefault();
        moveSearch(e.shiftKey ? -1 : 1);
        return;
    }
    if (!inSearch && e.key === '/') {
        e.preventDefault();
        const inp = document.getElementById('livelog-search');
        if (inp) inp.focus();
        return;
    }
    if (!inSearch && (e.key === 'w' || e.key === 'W')) {
        e.preventDefault();
        toggleWrap();
        return;
    }
});

document.addEventListener('input', e => {
    if (e.target && e.target.id === 'livelog-search') onSearchInput(e);
});

// Fleet filter change handlers — re-apply on every dropdown change.
document.addEventListener('change', e => {
    if (['repo-filter', 'arch-filter', 'fleet-status-filter', 'fleet-branch-filter']
            .includes(e.target.id)) {
        applyFleetFilters();
    }
    // When a per-row branch dropdown changes and a branch filter is active,
    // re-apply so the row hides if it no longer matches.
    if (e.target.classList && e.target.classList.contains('branch-select')) {
        applyFleetFilters();
    }
});

// ── Workers View ────────────────────────────────────────────────────────────

async function loadWorkers() {
    const [workersRes, buildsRes, healthRes] = await Promise.all([
        fetch(`${API}/api/workers`),
        fetch(`${API}/api/builds/running`),
        fetch(`${API}/api/health`),
    ]);
    const workersData = await workersRes.json();
    const buildsData = await buildsRes.json();
    let healthData = null;
    try { healthData = await healthRes.json(); } catch {}

    const grid = document.getElementById('worker-grid');
    if (workersData.workers.length === 0) {
        grid.innerHTML = '<p class="loading">No workers registered</p>';
    } else {
        const now = Date.now();
        grid.innerHTML = workersData.workers.map(w => {
            const isMaster = w.role === 'master';
            const ageSec = w.last_heartbeat
                ? Math.round((now - new Date(w.last_heartbeat).getTime()) / 1000)
                : -1;
            const stale = ageSec >= 0 && ageSec > 60;
            const badge = isMaster
                ? '<span class="role-badge master" title="Master ARM worker (this host)">master</span>'
                : '<span class="role-badge agent" title="Remote worker agent">agent</span>';
            const masterExtras = isMaster && healthData ? `
                <div>Redis: <strong style="color:${healthData.redis === 'connected' ? 'var(--green)' : 'var(--red)'}">${escapeHtml(healthData.redis || '?')}</strong></div>
                <div>Total registered: ${workersData.total}</div>
            ` : '';
            const statusKey = stale ? 'offline' : (w.status || 'offline');
            const statusLabel = stale ? `stale (${ageSec}s)` : statusKey;
            const statusPill = `<span class="worker-status-pill ${statusKey}">${escapeHtml(statusLabel)}</span>`;
            return `
                <div class="worker-card ${isMaster ? 'is-master' : ''} ${stale ? 'offline' : ''}">
                    <h4>${escapeHtml(w.worker_id)} ${badge} ${statusPill}</h4>
                    <div class="meta">
                        <div>Arch: <strong>${escapeHtml(w.arch || '')}</strong></div>
                        <div>Active: ${escapeHtml(String(w.active_jobs || '0'))} / ${escapeHtml(String(w.capacity || '?'))}</div>
                        <div>Last heartbeat: ${formatTime(w.last_heartbeat)}</div>
                        ${masterExtras}
                    </div>
                </div>
            `;
        }).join('');
    }

    const queueGrid = document.getElementById('queue-status');
    queueGrid.innerHTML = Object.entries(buildsData.queues).map(([name, count]) => `
        <div class="queue-item">
            <div class="count">${count}</div>
            <div class="label">${name}</div>
        </div>
    `).join('');
}

// ── Nightly View ────────────────────────────────────────────────────────────

async function loadNightly() {
    const res = await fetch(`${API}/api/nightly/latest`);
    const data = await res.json();

    const summary = document.getElementById('nightly-summary');
    if (!data.run_id) {
        summary.innerHTML = '<p class="loading">No nightly runs yet</p>';
        return;
    }

    summary.innerHTML = `
        <div class="stat"><div class="value">${data.total}</div><div class="label">Total</div></div>
        <div class="stat"><div class="value" style="color:var(--green)">${data.passed}</div><div class="label">Passed</div></div>
        <div class="stat"><div class="value" style="color:var(--red)">${data.failed}</div><div class="label">Failed</div></div>
        <div style="flex:1"></div>
        <div style="color:var(--text-muted);font-size:0.8rem">${data.run_id}</div>
    `;

    const tbody = document.getElementById('nightly-body');
    tbody.innerHTML = data.results.map(job => {
        const r = job.result || {};
        const arch = r.arch || job.worker_arch || '?';
        const isTerminated = job.status === 'terminated';
        const cls = isTerminated ? 'status-terminated' : (r.passed ? 'status-pass' : 'status-fail');
        const label = isTerminated ? 'TERM' : (r.passed ? 'PASS' : 'FAIL');
        const status = job.job_id
            ? `<a href="#" class="${cls} log-link" data-final-job="${escapeHtml(job.job_id)}"
                  title="View log">${label}</a>`
            : `<span class="${cls}">${label}</span>`;
        const historyHtml = (job.history && job.history.length > 0)
            ? `<div class="build-spark">${renderBuildSpark(job.history)}</div>` : '—';
        return `<tr>
            <td>${escapeHtml(job.repo.split('/').pop())}</td>
            <td><span class="arch-badge">${escapeHtml(arch)}</span></td>
            <td>${status}</td>
            <td>${historyHtml}</td>
            <td>${r.duration_seconds || 0}s</td>
            <td>${formatTime(job.finished_at)}</td>
        </tr>`;
    }).join('');
}

// ── History View ────────────────────────────────────────────────────────────

let historyFilters = {};
let historyDistinct = { repos: [], arches: [], branches: [] };

async function loadHistory() {
    const params = new URLSearchParams();
    const repo   = document.getElementById('history-repo').value.trim();
    const arch   = document.getElementById('history-arch').value;
    const branch = document.getElementById('history-branch').value;
    const status = document.getElementById('history-status').value;
    const type   = document.getElementById('history-type')?.value || '';
    if (repo)   params.set('repo', repo);
    if (arch)   params.set('arch', arch);
    if (branch) params.set('branch', branch);
    if (status) params.set('status', status);
    if (type)   params.set('type', type);
    const limit = document.getElementById('history-limit')?.value || '50';
    params.set('limit', limit);

    const tbody = document.getElementById('history-body');
    tbody.innerHTML = '<tr><td colspan="11" class="loading">Loading history…</td></tr>';
    try {
        const res = await fetch(`${API}/api/builds/history?` + params.toString());
        const data = await res.json();
        // Populate branch dropdown from returned distinct values
        if (JSON.stringify(data.filters.branches) !== JSON.stringify(historyDistinct.branches)) {
            historyDistinct = data.filters;
            const branchSel = document.getElementById('history-branch');
            const cur = branchSel.value;
            branchSel.innerHTML = `<option value="">All branches</option>` +
                data.filters.branches.map(v =>
                    `<option value="${escapeHtml(v)}"${v === cur ? ' selected' : ''}>${escapeHtml(v)}</option>`
                ).join('');
        }
        document.getElementById('history-count').textContent = `${data.total_returned} runs`;

        if (!data.rows.length) {
            tbody.innerHTML = '<tr><td colspan="11" class="loading">No matching runs.</td></tr>';
            return;
        }
        tbody.innerHTML = data.rows.map(r => {
            const dur = formatDuration(r.duration);
            const statusCls = r.status === 'terminated' ? 'status-terminated'
                            : r.passed ? 'status-pass' : 'status-fail';
            const statusLabel = r.status === 'terminated' ? 'TERM'
                              : r.passed ? 'PASS' : 'FAIL';
            const repoShort = r.repo.split('/').pop();
            const repoLink = `<a href="https://github.com/${r.repo}" target="_blank" rel="noopener" title="${escapeHtml(r.repo)}">${escapeHtml(repoShort)}</a>`;
            const logLink = r.job_id
                ? `<a href="#" class="log-link" data-final-job="${escapeHtml(r.job_id)}" title="View log">log</a>
                   · <a href="/log/${escapeHtml(r.job_id)}" target="_blank" rel="noopener" title="Fullscreen">⤢</a>`
                : '—';
            const rerunBtn = (r.job_id && r.type === 'integration-test' && isWriter())
                ? `<button class="btn btn-small btn-secondary row-rerun" data-action data-job-id="${escapeHtml(r.job_id)}" title="Re-queue this job" aria-label="Re-run this integration test">↻ Rerun</button>`
                : '—';
            return `<tr>
                <td title="${escapeHtml(r.started_at || '')}">${formatTime(r.started_at)}</td>
                <td>${repoLink}</td>
                <td>${escapeHtml(r.branch)}</td>
                <td><span class="arch-badge">${escapeHtml(r.arch)}</span></td>
                <td>${dur}</td>
                <td><span class="${statusCls}">${statusLabel}</span></td>
                <td style="font-size:0.8rem;color:var(--text-2)">${escapeHtml(formatJobType(r.type))}</td>
                <td>${escapeHtml(r.trigger || '')}</td>
                <td><span style="font-size:0.75rem;color:var(--text-muted)">${escapeHtml(r.worker_id || '')}</span></td>
                <td>${logLink}</td>
                <td>${rerunBtn}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="11" class="loading">Error loading history: ${escapeHtml(String(e))}</td></tr>`;
    }
}

// Debounce the search bar, instant for selects
let _historySearchTimer;
document.addEventListener('input', e => {
    if (e.target.id === 'history-repo') {
        clearTimeout(_historySearchTimer);
        _historySearchTimer = setTimeout(loadHistory, 300);
    }
});
['history-arch', 'history-branch', 'history-status', 'history-type', 'history-limit'].forEach(id => {
    document.addEventListener('change', e => {
        if (e.target.id === id) loadHistory();
    });
});
document.addEventListener('click', e => {
    if (e.target.id === 'history-refresh') loadHistory();
});

// ── Running Jobs Detail View ────────────────────────────────────────────────

async function loadRunningDetail() {
    const tbody = document.getElementById('running-body');
    const dtbody = document.getElementById('deferred-body');
    const qtbody = document.getElementById('queued-body');
    const summary = document.getElementById('running-summary');
    try {
        const [runRes, qRes] = await Promise.all([
            fetch(`${API}/api/builds/running`),
            fetch(`${API}/api/queue/list`),
        ]);
        const data = await runRes.json();
        const qData = await qRes.json();
        const now = Date.now();

        summary.innerHTML = `
            <div class="stat"><div class="value">${data.running.length}</div><div class="label">Running</div></div>
            <div class="stat"><div class="value">${data.deferred.length}</div><div class="label">Deferred triples</div></div>
            <div class="stat"><div class="value">${data.queues.arm64}</div><div class="label">arm64 queued</div></div>
            <div class="stat"><div class="value">${data.queues.amd64}</div><div class="label">amd64 queued</div></div>
        `;

        if (!data.running.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="loading">No jobs running.</td></tr>';
        } else {
            tbody.innerHTML = data.running.map(r => {
                const elapsed = r.started_at ? Math.round((now - new Date(r.started_at).getTime()) / 1000) : 0;
                const repoShort = (r.repo || '').split('/').pop();
                const repoLink = r.repo
                    ? `<a href="https://github.com/${r.repo}" target="_blank" rel="noopener">${escapeHtml(repoShort)}</a>`
                    : '—';
                const termBtn = r.job_id
                    ? `<button class="btn btn-small btn-danger row-terminate" data-action data-job-id="${escapeHtml(r.job_id)}" title="Terminate this job">■ Terminate</button>
                       <button class="btn btn-small btn-secondary row-shell" data-action data-job-id="${escapeHtml(r.job_id)}" data-title="${escapeHtml((r.repo || '').split('/').pop() + ' · ' + (r.arch || ''))}" title="Open shell in test container">⌨ Shell</button>
                       <a href="#" class="btn btn-small btn-secondary log-link" data-final-job="${escapeHtml(r.job_id)}" title="View live log">log</a>
                       <a href="/log/${escapeHtml(r.job_id)}" target="_blank" rel="noopener" class="btn btn-small btn-secondary" title="Fullscreen">⤢</a>`
                    : '—';
                return `<tr>
                    <td>${repoLink}</td>
                    <td>${escapeHtml(r.branch || r.ref || '')}</td>
                    <td><span class="arch-badge">${escapeHtml(r.arch || '')}</span></td>
                    <td style="font-size:0.8rem;color:var(--text-2)">${escapeHtml(formatJobType(r.type))}</td>
                    <td><span style="font-size:0.75rem;color:var(--text-muted)">${escapeHtml(r.worker_id || '')}</span></td>
                    <td title="${escapeHtml(r.started_at || '')}">${formatTime(r.started_at)}</td>
                    <td>${formatDuration(elapsed)}</td>
                    <td>${termBtn}</td>
                </tr>`;
            }).join('');
        }

        if (!data.deferred.length) {
            dtbody.innerHTML = '<tr><td colspan="2" class="loading">None</td></tr>';
        } else {
            dtbody.innerHTML = data.deferred.map(d =>
                `<tr><td>${escapeHtml(d.triple)}</td><td>${d.depth}</td></tr>`
            ).join('');
        }

        const qCount = document.getElementById('queued-count');
        if (qCount) qCount.textContent = qData.items && qData.items.length ? `(${qData.items.length})` : '';

        if (!qData.items || !qData.items.length) {
            qtbody.innerHTML = '<tr><td colspan="7" class="loading">Queue is empty.</td></tr>';
        } else {
            qtbody.innerHTML = qData.items.map(q => {
                const repoShort = (q.repo || '').split('/').pop();
                const repoLink = q.repo
                    ? `<a href="https://github.com/${q.repo}" target="_blank" rel="noopener">${escapeHtml(repoShort)}</a>`
                    : '—';
                const delBtn = (q.job_id && isWriter())
                    ? `<button class="btn btn-small btn-danger row-queue-delete" data-action data-job-id="${escapeHtml(q.job_id)}" title="Remove from queue" aria-label="Remove job from queue">✕ Remove</button>`
                    : '—';
                const byUser = q.requested_by
                    ? `<span style="font-size:0.75rem;color:var(--text-2)">${escapeHtml(q.requested_by)}</span>`
                    : '—';
                return `<tr>
                    <td>${repoLink}</td>
                    <td>${escapeHtml(q.ref || '')}</td>
                    <td><span class="arch-badge">${escapeHtml(q.arch || '')}</span></td>
                    <td style="font-size:0.8rem;color:var(--text-2)">${escapeHtml(formatJobType(q.type || 'integration-test'))}</td>
                    <td>${byUser}</td>
                    <td title="${escapeHtml(q.queued_at || '')}">${formatTime(q.queued_at)}</td>
                    <td>${delBtn}</td>
                </tr>`;
            }).join('');
        }
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
        if (document.getElementById('queued-body'))
            document.getElementById('queued-body').innerHTML = `<tr><td colspan="6" class="loading">Error loading queue.</td></tr>`;
    }
}

document.addEventListener('click', async e => {
    const btn = e.target.closest('.row-terminate');
    if (!btn) return;
    e.preventDefault();
    if (!isWriter()) {
        alert('Only org members can terminate jobs.');
        return;
    }
    const jobId = btn.dataset.jobId;
    if (!confirm(`Terminate job ${jobId}?`)) return;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`/api/jobs/${jobId}/terminate`, { method: 'POST' });
        if (!res.ok) alert(`Termination failed (${res.status})`);
    } finally {
        btn.disabled = false; btn.textContent = '■ Terminate';
        loadRunningDetail();
    }
});

document.addEventListener('click', async e => {
    const btn = e.target.closest('.row-queue-delete');
    if (!btn) return;
    e.preventDefault();
    if (!isWriter()) {
        alert('Only org members can remove queue items.');
        return;
    }
    const jobId = btn.dataset.jobId;
    if (!confirm(`Remove job ${jobId} from the queue?`)) return;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`/api/queue/item?job_id=${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            alert(`Remove failed (${res.status}): ${body.detail || ''}`);
        }
    } finally {
        loadRunningDetail();
    }
});

document.addEventListener('click', async e => {
    const btn = e.target.closest('.row-rerun');
    if (!btn) return;
    e.preventDefault();
    if (!isWriter()) {
        alert('Only org members can rerun jobs.');
        return;
    }
    const jobId = btn.dataset.jobId;
    if (!confirm(`Re-queue job ${jobId}?`)) return;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`/api/builds/rerun/${encodeURIComponent(jobId)}`, { method: 'POST' });
        if (res.ok) {
            btn.textContent = '✓ queued';
            setTimeout(() => { btn.disabled = false; btn.textContent = '↻ Rerun'; }, 2000);
        } else {
            const body = await res.json().catch(() => ({}));
            btn.textContent = `✕ failed (${res.status})`;
            setTimeout(() => { btn.disabled = false; btn.textContent = '↻ Rerun'; }, 3000);
        }
    } catch {
        btn.disabled = false; btn.textContent = '↻ Rerun';
    }
});

// ── Synchronizer tab ────────────────────────────────────────────────────────

let syncCommandsCache = null;
let activeSyncView = 'commands';

// Sub-tab switching
document.addEventListener('click', e => {
    const stab = e.target.closest('.sync-tab');
    if (!stab) return;
    const view = stab.dataset.syncView;
    if (!view) return;
    activeSyncView = view;
    document.querySelectorAll('.sync-tab').forEach(t => t.classList.toggle('active', t === stab));
    document.querySelectorAll('.sync-subview').forEach(sv => sv.hidden = true);
    document.getElementById(`sync-view-${view}`).hidden = false;
    if (view === 'status') loadSyncStatus();
    if (view === 'prs')    loadSyncPRs();
    if (view === 'issues') loadSyncIssues();
});

async function loadSyncTab() {
    if (!syncCommandsCache) {
        try {
            const res = await fetch(`${API}/api/sync/commands`);
            syncCommandsCache = (await res.json()).commands;
        } catch (e) {
            syncCommandsCache = [];
        }
    }
    const grid = document.getElementById('sync-cards');
    grid.innerHTML = syncCommandsCache.map(c => `
        <div class="sync-card" data-action data-cmd-id="${escapeHtml(c.id)}">
            <h4>${c.icon || '⚙'} ${escapeHtml(c.label)}${c.destructive ? ' <span style="color:var(--red);font-size:0.7rem">⚠ DESTRUCTIVE</span>' : ''}</h4>
            <p>${escapeHtml(c.description)}</p>
            <span class="cmd">sync ${c.args.join(' ')}</span>
        </div>
    `).join('');
    loadSyncHistory();
    // Restore the active sub-view
    document.querySelectorAll('.sync-subview').forEach(sv => sv.hidden = true);
    document.getElementById(`sync-view-${activeSyncView}`).hidden = false;
    document.querySelectorAll('.sync-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.syncView === activeSyncView);
    });
}

async function loadSyncHistory() {
    const tbody = document.getElementById('sync-history-body');
    try {
        const res = await fetch(`${API}/api/sync/history?limit=30`);
        const data = await res.json();
        if (!data.rows.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="loading">No sync runs yet.</td></tr>';
            return;
        }
        tbody.innerHTML = data.rows.map(r => {
            const passed = r.exit_code === 0;
            const cls = r.status === 'terminated' ? 'status-terminated' : (passed ? 'status-pass' : 'status-fail');
            const label = r.status === 'terminated' ? 'TERM' : (passed ? 'OK' : 'FAIL');
            const log = r.job_id ? `<a href="#" class="log-link" data-final-job="${escapeHtml(r.job_id)}">log</a>
                                    · <a href="/log/${escapeHtml(r.job_id)}" target="_blank" rel="noopener">⤢</a>` : '—';
            return `<tr>
                <td>${formatTime(r.started_at)}</td>
                <td><strong>${escapeHtml(r.command_label || r.command_id)}</strong>
                    <div style="font-size:0.7rem;color:var(--text-3);font-family:ui-monospace,monospace">${escapeHtml(r.command_id)}</div>
                </td>
                <td>${formatDuration(r.duration)}</td>
                <td><span class="${cls}">${label}</span></td>
                <td><span style="font-size:0.78rem;color:var(--text-2)">${escapeHtml(r.requested_by || '')}</span></td>
                <td>${log}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

// ── Synchronizer: Status sub-tab ─────────────────────────────────────────────

async function loadSyncStatus(force = false) {
    const tbody = document.getElementById('sync-status-body');
    tbody.innerHTML = '<tr><td colspan="5" class="loading">Running sync status…</td></tr>';
    try {
        const url = force ? `${API}/api/sync/status-summary?bust=${Date.now()}` : `${API}/api/sync/status-summary`;
        const res = await fetch(url);
        const data = await res.json();
        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="5" class="loading" style="color:var(--red)">Error: ${escapeHtml(data.error)}</td></tr>`;
            return;
        }
        const rows = Array.isArray(data.rows) ? data.rows : [];
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="loading">No status data returned.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(r => {
            const repo    = escapeHtml(r.repo || r.name || '');
            const pinned  = escapeHtml(r.framework_version || r.pinned_version || r.version || '—');
            const latest  = escapeHtml(r.latest_tag || r.latest_version || r.latest || '—');
            const status  = r.status || '';
            let drift;
            if (status === 'up-to-date') {
                drift = '<span style="color:var(--green)">up to date</span>';
            } else if (status === 'behind' || status.includes('behind')) {
                drift = `<span style="color:var(--amber)">${escapeHtml(status)}</span>`;
            } else if (status === 'error') {
                drift = '<span style="color:var(--red)">error</span>';
            } else if (status === 'unknown') {
                drift = '<span style="color:var(--text-3)">unknown</span>';
            } else {
                drift = escapeHtml(status) || '—';
            }
            const ci = r.ci === false
                ? '<span style="color:var(--text-3)">off</span>'
                : (r.ci === true ? '<span style="color:var(--green)">on</span>' : '—');
            return `<tr>
                <td><a href="https://github.com/${repo}" target="_blank" rel="noopener">${repo}</a></td>
                <td style="font-family:ui-monospace,monospace">${pinned}</td>
                <td style="font-family:ui-monospace,monospace">${latest}</td>
                <td>${drift}</td>
                <td>${ci}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

document.getElementById('sync-status-refresh').addEventListener('click', () => {
    loadSyncStatus(true);
});

// ── Synchronizer: PRs sub-tab ────────────────────────────────────────────────

let syncPRsData = [];

async function loadSyncPRs(force = false) {
    const tbody = document.getElementById('sync-prs-body');
    tbody.innerHTML = '<tr><td colspan="6" class="loading">Fetching open PRs…</td></tr>';
    try {
        if (force) await fetch(`${API}/api/sync/prs/invalidate`, { method: 'POST' });
        const res = await fetch(`${API}/api/sync/prs`);
        const data = await res.json();
        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="6" class="loading" style="color:var(--red)">Error: ${escapeHtml(data.error)}</td></tr>`;
            return;
        }
        syncPRsData = Array.isArray(data.rows) ? data.rows : [];
        // Derive org label from first row
        const firstPR = syncPRsData[0];
        if (firstPR) {
            const org = (firstPR.repository?.nameWithOwner || '').split('/')[0] || '';
            if (org) document.getElementById('sync-prs-org').textContent = org;
        }
        renderSyncPRs();
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

function renderSyncPRs() {
    const filter = (document.getElementById('sync-prs-filter').value || '').toLowerCase();
    const tbody = document.getElementById('sync-prs-body');
    const rows = syncPRsData.filter(r => {
        if (!filter) return true;
        const title = (r.title || '').toLowerCase();
        const repo  = (r.repository?.nameWithOwner || r.repository?.name || '').toLowerCase();
        return title.includes(filter) || repo.includes(filter);
    });
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">No open PRs found.</td></tr>';
        return;
    }
    const isSergioUser = authState.user === 'sergiohinojosa';
    tbody.innerHTML = rows.map(r => {
        const repo    = r.repository?.nameWithOwner || r.repository?.name || '—';
        const author  = r.author?.login || r.author || '—';
        const labels  = (r.labels || []).map(l => `<span class="label-chip">${escapeHtml(l.name || l)}</span>`).join(' ');
        const updated = formatTime(r.updatedAt || r.updated_at);
        const ci = r._ci;
        let ciBadge = '<span class="ci-badge none">—</span>';
        let ciJobLink = '';
        if (ci) {
            const isTerminated = ci.status === 'terminated';
            const ciFailed = !ci.passed && !isTerminated;
            if (isTerminated)    ciBadge = '<span class="ci-badge term">TERM</span>';
            else if (ci.passed)  ciBadge = '<span class="ci-badge pass">PASS</span>';
            else                 ciBadge = '<span class="ci-badge fail">FAIL</span>';
            if (ci.job_id) {
                ciBadge = `<a href="#" class="log-link" data-final-job="${escapeHtml(ci.job_id)}" title="View integration test log">${ciBadge}</a>`;
            }
        }
        // Fix with AI: only for failed integration tests, only for sergiohinojosa
        const showFix = isSergioUser && ci && !ci.passed && ci.status !== 'terminated';
        const fixBtn = showFix
            ? `<button class="btn-fix-ai fix-pr-btn" data-action
                   data-repo="${escapeHtml(repo)}"
                   data-pr="${escapeHtml(String(r.number))}"
                   data-branch="${escapeHtml(r.headRefName || '')}"
                   data-ci-job="${escapeHtml(ci?.job_id || '')}"
                   title="Let AI analyze the failure and fix the repo or flag a framework issue">✨ Fix with AI</button>`
            : '—';
        return `<tr>
            <td><a href="https://github.com/${escapeHtml(repo)}" target="_blank" rel="noopener">${escapeHtml(repo.split('/').pop())}</a></td>
            <td><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">#${escapeHtml(String(r.number))}</a></td>
            <td>${escapeHtml(r.title)}</td>
            <td>${escapeHtml(String(author))}</td>
            <td>${ciBadge}</td>
            <td>${labels || '—'}</td>
            <td>${updated}</td>
            <td>${fixBtn}</td>
        </tr>`;
    }).join('');
}

document.getElementById('sync-prs-filter').addEventListener('input', renderSyncPRs);
document.getElementById('sync-prs-refresh').addEventListener('click', () => loadSyncPRs(true));

// ── Synchronizer: Issues sub-tab ─────────────────────────────────────────────

let syncIssuesData = [];

async function loadSyncIssues(force = false) {
    const tbody = document.getElementById('sync-issues-body');
    tbody.innerHTML = '<tr><td colspan="6" class="loading">Fetching open issues…</td></tr>';
    try {
        if (force) await fetch(`${API}/api/sync/issues/invalidate`, { method: 'POST' });
        const res = await fetch(`${API}/api/sync/issues`);
        const data = await res.json();
        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="6" class="loading" style="color:var(--red)">Error: ${escapeHtml(data.error)}</td></tr>`;
            return;
        }
        syncIssuesData = Array.isArray(data.rows) ? data.rows : [];
        const firstIssue = syncIssuesData[0];
        if (firstIssue) {
            const org = (firstIssue.repository?.nameWithOwner || '').split('/')[0] || '';
            if (org) document.getElementById('sync-issues-org').textContent = org;
        }
        renderSyncIssues();
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

function renderSyncIssues() {
    const filter = (document.getElementById('sync-issues-filter').value || '').toLowerCase();
    const tbody = document.getElementById('sync-issues-body');
    const rows = syncIssuesData.filter(r => {
        if (!filter) return true;
        const title = (r.title || '').toLowerCase();
        const repo  = (r.repository?.nameWithOwner || r.repository?.name || '').toLowerCase();
        return title.includes(filter) || repo.includes(filter);
    });
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="loading">No open issues found.</td></tr>';
        return;
    }
    const isSergioUser = authState.user === 'sergiohinojosa';
    tbody.innerHTML = rows.map(r => {
        const repo    = r.repository?.nameWithOwner || r.repository?.name || '—';
        const author  = r.author?.login || r.author || '—';
        const labels  = (r.labels || []).map(l => `<span class="label-chip">${escapeHtml(l.name || l)}</span>`).join(' ');
        const updated = formatTime(r.updatedAt || r.updated_at);
        const fixBtn = isSergioUser
            ? `<button class="btn-fix-ai fix-issue-btn" data-action
                   data-repo="${escapeHtml(repo)}"
                   data-issue="${escapeHtml(String(r.number))}"
                   data-title="${escapeHtml(r.title)}"
                   title="Let AI analyze and fix this issue">✨ Fix with AI</button>`
            : '—';
        return `<tr>
            <td><a href="https://github.com/${escapeHtml(repo)}" target="_blank" rel="noopener">${escapeHtml(repo.split('/').pop())}</a></td>
            <td><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">#${escapeHtml(String(r.number))}</a></td>
            <td>${escapeHtml(r.title)}</td>
            <td>${escapeHtml(String(author))}</td>
            <td>${labels || '—'}</td>
            <td>${updated}</td>
            <td>${fixBtn}</td>
        </tr>`;
    }).join('');
}

document.getElementById('sync-issues-filter').addEventListener('input', renderSyncIssues);
document.getElementById('sync-issues-refresh').addEventListener('click', () => loadSyncIssues(true));

// ── Fix with AI modal ────────────────────────────────────────────────────────

let fixAiContext = null;  // { type: 'pr'|'issue', repo, number, branch?, ciJobId? }

function openFixWithAI(type, data) {
    fixAiContext = { type, ...data };
    const modal = document.getElementById('fix-ai-modal');
    const title = document.getElementById('fix-ai-title');
    const desc  = document.getElementById('fix-ai-description');
    const ciInfo = document.getElementById('fix-ai-ci-info');
    document.getElementById('fix-ai-instructions').value = '';

    if (type === 'pr') {
        title.textContent = `Fix with AI — PR #${data.number} · ${data.repo.split('/').pop()}`;
        desc.textContent = 'The AI agent will fetch the failed integration test log, determine whether the root cause is in this repo or the shared framework, then apply a surgical fix. If the framework is at fault, the PR stays open and you will be notified. If the repo is at fault, a fix is committed to this branch and a new CI run is triggered.';
        if (data.ciJobId) {
            ciInfo.hidden = false;
            ciInfo.innerHTML = `Failed integration test job: <a href="#" class="log-link" data-final-job="${escapeHtml(data.ciJobId)}">view log</a>`;
        } else {
            ciInfo.hidden = true;
        }
    } else {
        title.textContent = `Fix with AI — Issue #${data.number} · ${data.repo.split('/').pop()}`;
        desc.textContent = 'The AI agent will read this issue, understand the problem, and commit a fix to a new branch in the repo, then open a pull request.';
        ciInfo.hidden = true;
    }

    modal.hidden = false;
    document.getElementById('fix-ai-instructions').focus();
}

function closeFixWithAI() {
    document.getElementById('fix-ai-modal').hidden = true;
    fixAiContext = null;
}

async function submitFixWithAI() {
    if (!fixAiContext) return;
    const instructions = document.getElementById('fix-ai-instructions').value.trim();
    const submitBtn = document.getElementById('fix-ai-submit');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting…';

    try {
        let endpoint, payload;
        if (fixAiContext.type === 'pr') {
            endpoint = '/api/agent/fix-pr';
            payload = {
                repo: fixAiContext.repo,
                pr_number: fixAiContext.number,
                branch: fixAiContext.branch || 'main',
                instructions,
            };
        } else {
            endpoint = '/api/agent/fix-issue';
            payload = {
                repo: fixAiContext.repo,
                issue_number: fixAiContext.number,
                instructions,
            };
        }

        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload),
        });

        if (res.status === 401) {
            window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
            return;
        }
        if (res.status === 403) {
            const body = await res.json().catch(() => ({}));
            alert(body.detail || 'Access denied.');
            return;
        }
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            alert(`Failed to submit (${res.status}): ${body.detail || ''}`);
            return;
        }

        const data = await res.json();
        closeFixWithAI();
        const repoShort = fixAiContext.repo.split('/').pop();
        const label = fixAiContext.type === 'pr'
            ? `Fix PR #${fixAiContext.number} · ${repoShort}`
            : `Fix Issue #${fixAiContext.number} · ${repoShort}`;
        openLiveLog(data.job_id, label);
    } catch (e) {
        alert('Network error: ' + e);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = '✨ Submit fix';
    }
}

document.addEventListener('click', e => {
    // Fix with AI — PR button
    const prBtn = e.target.closest('.fix-pr-btn');
    if (prBtn) {
        e.preventDefault();
        if (!isWriter()) { alert('Sign in as an org member to use Fix with AI.'); return; }
        openFixWithAI('pr', {
            repo: prBtn.dataset.repo,
            number: prBtn.dataset.pr,
            branch: prBtn.dataset.branch,
            ciJobId: prBtn.dataset.ciJob,
        });
        return;
    }
    // Fix with AI — Issue button
    const issueBtn = e.target.closest('.fix-issue-btn');
    if (issueBtn) {
        e.preventDefault();
        if (!isWriter()) { alert('Sign in as an org member to use Fix with AI.'); return; }
        openFixWithAI('issue', {
            repo: issueBtn.dataset.repo,
            number: issueBtn.dataset.issue,
            title: issueBtn.dataset.title,
        });
        return;
    }
    // Fix AI modal — close/cancel
    if (e.target.id === 'fix-ai-close' || e.target.id === 'fix-ai-cancel' || e.target.id === 'fix-ai-modal') {
        closeFixWithAI(); return;
    }
    // Fix AI modal — submit
    if (e.target.id === 'fix-ai-submit') {
        submitFixWithAI(); return;
    }
});

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        const modal = document.getElementById('fix-ai-modal');
        if (modal && !modal.hidden) { closeFixWithAI(); return; }
    }
});

document.addEventListener('click', async e => {
    const card = e.target.closest('.sync-card');
    if (!card) return;
    const cmdId = card.dataset.cmdId;
    const spec = (syncCommandsCache || []).find(c => c.id === cmdId);
    if (!spec) return;
    if (!authState.signedIn) {
        if (confirm('Sign in to run sync commands?')) {
            window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
        }
        return;
    }
    if (!isWriter()) {
        alert('Only org members can run sync commands. You are signed in as a guest.');
        return;
    }
    if (spec.destructive && !confirm(`This is a destructive command:\n\nsync ${spec.args.join(' ')}\n\nProceed?`)) return;
    card.style.opacity = '0.5';
    try {
        const res = await fetch(`${API}/api/sync/run`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ command: cmdId }),
        });
        if (!res.ok) {
            alert(`Sync command failed to enqueue: ${res.status}`);
            return;
        }
        const data = await res.json();
        openLiveLog(data.job_id, `sync ${spec.args.join(' ')}`);
        setTimeout(loadSyncHistory, 1500);
    } finally {
        card.style.opacity = '1';
    }
});

// ── Helpers ─────────────────────────────────────────────────────────────────

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'
    }[c]));
}

function formatJobType(type) {
    if (!type || type === 'integration-test') return 'Integration test';
    if (type === 'daemon') return 'Training';
    if (type === 'sync-command') return 'Sync';
    if (type === 'deploy-ghpages') return 'Deploy Pages';
    if (['fix-issue','fix-ci','review-pr','migrate-gen3','scaffold-lab','validate-after-push'].includes(type)) return 'Agent';
    return type;
}

function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '—';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

function formatTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// ── Shell terminal ──────────────────────────────────────────────────────────

let shellTerm = null;
let shellFitAddon = null;
let shellWs = null;
let shellJobId = null;   // job ID for the current shell session (≠ currentJobId which is livelog)
let shellActiveTab = 'terminal'; // 'terminal' or an app name

async function openShell(jobId, title) {
    shellJobId = jobId;
    if (!isWriter()) {
        alert('Only org members can open a shell.');
        return;
    }
    document.getElementById('shell-modal-title').textContent = `Shell · ${title}`;
    document.getElementById('shell-modal').hidden = false;

    // Tear down any previous session
    if (shellWs) { try { shellWs.close(); } catch {} shellWs = null; }
    if (shellTerm) { shellTerm.dispose(); shellTerm = null; }
    document.getElementById('shell-terminal').innerHTML = '';
    document.getElementById('shell-app-tabs').innerHTML = '';
    document.getElementById('shell-app-frame').style.display = 'none';
    document.getElementById('shell-app-frame').src = '';
    shellActiveTab = 'terminal';

    // Fetch registered apps in the background and render tabs when ready
    _loadShellAppTabs(jobId);

    const term = new Terminal({
        cursorBlink: true,
        fontFamily: '"MesloLGS NF", "Cascadia Code NF", "Hack Nerd Font", ui-monospace, Menlo, monospace',
        fontSize: 13,
        theme: { background: '#000000', foreground: '#e2e8f2', cursor: '#00b4de' },
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('shell-terminal'));
    // Wait for MesloLGS NF to load before fitting — otherwise xterm measures
    // character width with the fallback font and gets the wrong column count,
    // causing lines to wrap / go blank at the wrong position.
    await document.fonts.load('13px "MesloLGS NF"').catch(() => {});
    fitAddon.fit();
    shellTerm = term;
    shellFitAddon = fitAddon;

    term.write('\x1b[36m◈  Connecting to isolation container…\x1b[0m\r\n');

    // auth_request is incompatible with WebSocket upgrade in nginx, so we
    // obtain a short-lived token via a regular (auth-gated) HTTP request first.
    let token = '';
    try {
        const res = await fetch(`/api/jobs/${jobId}/shell-token`, { method: 'POST' });
        if (!res.ok) {
            term.write(`\r\n\x1b[31mFailed to get shell token (${res.status}) — is the job still running?\x1b[0m\r\n`);
            return;
        }
        ({ token } = await res.json());
    } catch (err) {
        term.write(`\r\n\x1b[31mFailed to get shell token: ${err}\x1b[0m\r\n`);
        return;
    }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    // Pass the current terminal dimensions so the server sets the PTY size
    // before starting the subprocess — TUI apps (k9s, kubectl completions)
    // query the terminal size at startup and won't re-query after SIGWINCH.
    const ws = new WebSocket(
        `${proto}://${location.host}/ws/jobs/${jobId}/shell` +
        `?token=${token}&rows=${term.rows}&cols=${term.cols}`
    );
    ws.binaryType = 'arraybuffer';
    shellWs = ws;

    ws.onopen = () => {
        term.write('\x1b[32m◈  Tunnel established — spawning shell\x1b[0m\r\n\r\n');
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
    };
    ws.onmessage = e => {
        if (e.data instanceof ArrayBuffer) {
            term.write(new Uint8Array(e.data));
        } else {
            term.write(e.data);
        }
    };
    ws.onclose = () => {
        term.write('\r\n\x1b[90m[connection closed]\x1b[0m\r\n');
    };
    ws.onerror = () => {
        term.write('\r\n\x1b[31m[WebSocket error — check that the job is still running]\x1b[0m\r\n');
    };

    term.onData(data => {
        if (shellWs && shellWs.readyState === WebSocket.OPEN) {
            shellWs.send(new TextEncoder().encode(data));
        }
    });
    term.onResize(({ rows, cols }) => {
        if (shellWs && shellWs.readyState === WebSocket.OPEN) {
            shellWs.send(JSON.stringify({ type: 'resize', rows, cols }));
        }
    });
}

function closeShell() {
    document.getElementById('shell-modal').hidden = true;
    if (shellWs) { try { shellWs.close(); } catch {} shellWs = null; }
    if (shellTerm) { shellTerm.dispose(); shellTerm = null; }
    document.getElementById('shell-app-tabs').innerHTML = '';
    document.getElementById('shell-app-frame').src = '';
    document.getElementById('shell-app-frame').style.display = 'none';
    document.getElementById('shell-app-empty').style.display = 'none';
    document.getElementById('shell-terminal').style.display = '';
    shellJobId = null;
    shellActiveTab = 'terminal';
}

async function _loadShellAppTabs(jobId) {
    if (shellJobId !== jobId) return;
    let apps = [];
    try {
        const res = await fetch(`/api/jobs/${jobId}/apps`);
        if (res.ok) apps = (await res.json()).apps || [];
    } catch {}
    if (shellJobId !== jobId) return;

    const tabBar = document.getElementById('shell-app-tabs');
    tabBar.innerHTML = '';

    const termBtn = document.createElement('button');
    termBtn.className = 'btn btn-small btn-secondary shell-tab-btn active';
    termBtn.dataset.tab = 'terminal';
    termBtn.textContent = '⌨ Terminal';
    tabBar.appendChild(termBtn);

    if (apps.length === 0) {
        const btn = document.createElement('button');
        btn.className = 'btn btn-small btn-secondary shell-tab-btn';
        btn.dataset.tab = '__empty__';
        btn.textContent = '⬡ Apps';
        tabBar.appendChild(btn);
    } else {
        for (const app of apps) {
            const btn = document.createElement('button');
            btn.className = 'btn btn-small btn-secondary shell-tab-btn';
            btn.dataset.tab = app.name;
            btn.dataset.proxyUrl = app.proxy_url;
            btn.textContent = `⬡ ${app.name}`;
            tabBar.appendChild(btn);
        }
    }

    tabBar.addEventListener('click', e => {
        const btn = e.target.closest('.shell-tab-btn');
        if (!btn) return;
        _switchShellTab(btn.dataset.tab, btn.dataset.proxyUrl || '');
    });
}

function _switchShellTab(tab, proxyUrl) {
    shellActiveTab = tab;
    const terminal = document.getElementById('shell-terminal');
    const frame = document.getElementById('shell-app-frame');
    const empty = document.getElementById('shell-app-empty');

    document.querySelectorAll('.shell-tab-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });

    if (tab === 'terminal') {
        terminal.style.display = '';
        frame.style.display = 'none';
        frame.src = '';
        empty.style.display = 'none';
        if (shellFitAddon && shellTerm) {
            requestAnimationFrame(() => shellFitAddon.fit());
        }
    } else if (tab === '__empty__') {
        terminal.style.display = 'none';
        frame.style.display = 'none';
        frame.src = '';
        empty.style.display = '';
        empty.innerHTML = '<h4>No apps deployed</h4><p>Open a shell and run <code>deployApp</code> without arguments to list available apps, then deploy one.</p>';
    } else {
        terminal.style.display = 'none';
        empty.style.display = 'none';
        frame.style.display = '';
        if (frame.src !== location.origin + proxyUrl) {
            frame.src = proxyUrl;
        }
    }
}

// Fit terminal on window resize
window.addEventListener('resize', () => {
    if (shellFitAddon && shellTerm && !document.getElementById('shell-modal').hidden) {
        shellFitAddon.fit();
    }
});

async function _loadLivelogAppTabs(jobId) {
    if (currentJobId !== jobId) return;
    let apps = [];
    try {
        const res = await fetch(`/api/jobs/${jobId}/apps`);
        if (res.ok) apps = (await res.json()).apps || [];
    } catch {}
    if (currentJobId !== jobId) return;

    const tabBar = document.getElementById('livelog-app-tabs');
    // Preserve active tab across refreshes
    const activeTab = tabBar.querySelector('.livelog-tab-btn.active')?.dataset.tab || 'log';
    tabBar.innerHTML = '';

    const logBtn = document.createElement('button');
    logBtn.className = 'btn btn-small btn-secondary livelog-tab-btn';
    logBtn.dataset.tab = 'log';
    logBtn.textContent = '📋 Log';
    tabBar.appendChild(logBtn);

    if (apps.length === 0) {
        const btn = document.createElement('button');
        btn.className = 'btn btn-small btn-secondary livelog-tab-btn';
        btn.dataset.tab = '__empty__';
        btn.textContent = '⬡ Apps';
        tabBar.appendChild(btn);
    } else {
        for (const app of apps) {
            const btn = document.createElement('button');
            btn.className = 'btn btn-small btn-secondary livelog-tab-btn';
            btn.dataset.tab = app.name;
            btn.dataset.proxyUrl = app.proxy_url;
            btn.textContent = `⬡ ${app.name}`;
            tabBar.appendChild(btn);
        }
    }

    // Re-activate the previously active tab (or log if it disappeared)
    const tabToActivate = tabBar.querySelector(`[data-tab="${activeTab}"]`) || logBtn;
    tabToActivate.classList.add('active');

    tabBar.addEventListener('click', e => {
        const btn = e.target.closest('.livelog-tab-btn');
        if (!btn) return;
        _switchLivelogTab(btn.dataset.tab, btn.dataset.proxyUrl || '');
    });
}

function _switchLivelogTab(tab, proxyUrl) {
    const pre = document.getElementById('livelog-pre');
    const frame = document.getElementById('livelog-app-frame');
    const empty = document.getElementById('livelog-app-empty');

    document.querySelectorAll('.livelog-tab-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });

    if (tab === 'log') {
        pre.style.display = '';
        frame.style.display = 'none';
        frame.src = '';
        empty.style.display = 'none';
    } else if (tab === '__empty__') {
        pre.style.display = 'none';
        frame.style.display = 'none';
        frame.src = '';
        empty.style.display = '';
        empty.innerHTML = '<h4>No apps deployed</h4><p>Open a shell and run <code>deployApp</code> without arguments to list available apps, then deploy one.</p>';
    } else {
        pre.style.display = 'none';
        empty.style.display = 'none';
        frame.style.display = '';
        if (frame.src !== location.origin + proxyUrl) {
            frame.src = proxyUrl;
        }
    }
}

function shellPopupHtml(jobId, title) {
    // Self-contained terminal page written into a popup window.
    // Shares cookies with the parent page so token fetch is authenticated.
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>${title.replace(/</g,'&lt;')}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
@font-face{font-family:'MesloLGS NF';src:url('https://cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/MesloLGS%20NF%20Regular.ttf') format('truetype');font-weight:normal;font-style:normal}
@font-face{font-family:'MesloLGS NF';src:url('https://cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/MesloLGS%20NF%20Bold.ttf') format('truetype');font-weight:bold;font-style:normal}
html,body{margin:0;padding:0;background:#000;width:100%;height:100vh;overflow:hidden}
#t{width:100%;height:100vh;padding:4px;box-sizing:border-box}
</style>
</head>
<body>
<div id="t"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"><\/script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"><\/script>
<script>
(async()=>{
  const jobId=${JSON.stringify(jobId)};
  const term=new Terminal({cursorBlink:true,fontFamily:'"MesloLGS NF","Cascadia Code NF",ui-monospace,monospace',fontSize:13,theme:{background:'#000000',foreground:'#e2e8f2',cursor:'#00b4de'}});
  const fit=new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById('t'));
  await document.fonts.load('13px "MesloLGS NF"').catch(()=>{});
  fit.fit();
  term.write('\\x1b[36m◈  Connecting to isolation container…\\x1b[0m\\r\\n');
  let token='';
  try{
    const r=await fetch('/api/jobs/'+jobId+'/shell-token',{method:'POST',credentials:'include'});
    if(!r.ok){term.write('\\r\\n\\x1b[31mFailed to get shell token ('+r.status+')\\x1b[0m\\r\\n');return;}
    ({token}=await r.json());
  }catch(err){term.write('\\r\\n\\x1b[31mError: '+err+'\\x1b[0m\\r\\n');return;}
  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(proto+'://'+location.host+'/ws/jobs/'+jobId+'/shell?token='+token+'&rows='+term.rows+'&cols='+term.cols);
  ws.binaryType='arraybuffer';
  ws.onopen=()=>{term.write('\\x1b[32m◈  Tunnel established — spawning shell\\x1b[0m\\r\\n\\r\\n');ws.send(JSON.stringify({type:'resize',rows:term.rows,cols:term.cols}));};
  ws.onmessage=e=>{term.write(e.data instanceof ArrayBuffer?new Uint8Array(e.data):e.data);};
  ws.onclose=()=>term.write('\\r\\n\\x1b[90m[connection closed]\\x1b[0m\\r\\n');
  ws.onerror=()=>term.write('\\r\\n\\x1b[31m[WebSocket error]\\x1b[0m\\r\\n');
  term.onData(d=>{if(ws.readyState===WebSocket.OPEN)ws.send(new TextEncoder().encode(d));});
  term.onResize(({rows,cols})=>{if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:'resize',rows,cols}));});
  window.addEventListener('resize',()=>fit.fit());
})();
<\/script>
</body>
</html>`;
}

document.addEventListener('fullscreenchange', () => {
    const btn = document.getElementById('shell-fullscreen');
    if (!btn) return;
    btn.textContent = document.fullscreenElement ? '⛶ Exit Full' : '⛶ Fullscreen';
    // The fullscreen transition and CSS reflow take longer than two paint frames.
    // Wait 300ms so the browser has fully applied the new layout before we
    // measure the container and send the resize to the PTY.
    setTimeout(() => {
        if (!shellFitAddon || !shellTerm) return;
        shellFitAddon.fit();
        // Explicitly push the new size to the server so TUI apps like k9s
        // receive SIGWINCH even if onResize didn't fire (e.g. same row/col count).
        if (shellWs && shellWs.readyState === WebSocket.OPEN) {
            shellWs.send(JSON.stringify({ type: 'resize', rows: shellTerm.rows, cols: shellTerm.cols }));
        }
    }, 300);
});

document.addEventListener('click', e => {
    if (e.target.id === 'shell-close') { closeShell(); return; }
    if (e.target.id === 'shell-fullscreen') {
        const inner = document.querySelector('.shell-modal-inner');
        if (!document.fullscreenElement) {
            inner.requestFullscreen().catch(() => {});
        } else {
            document.exitFullscreen().catch(() => {});
        }
        return;
    }
    if (e.target.id === 'shell-newwin') {
        if (!shellJobId) return;
        // Open blank popup immediately (sync with click → bypasses popup blocker)
        const popup = window.open('', '_blank',
            'width=1280,height=800,menubar=no,toolbar=no,location=no,status=no,scrollbars=no,resizable=yes');
        if (!popup) return;
        const winTitle = document.getElementById('shell-modal-title')?.textContent || 'Shell';
        popup.document.write(shellPopupHtml(shellJobId, winTitle));
        popup.document.close();
        return;
    }
    if (e.target.id === 'shell-modal') { closeShell(); return; }

    const btn = e.target.closest('.row-shell');
    if (btn) {
        e.preventDefault();
        openShell(btn.dataset.jobId, btn.dataset.title || btn.dataset.jobId);
    }
});

document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !document.getElementById('shell-modal').hidden) {
        // Only close shell if the livelog modal isn't also open
        if (document.getElementById('livelog-modal').hidden) closeShell();
    }
});

// ── Agentic View ────────────────────────────────────────────────────────────

const AGENT_TYPES = new Set(['fix-ci', 'fix-issue', 'review-pr', 'migrate-gen3', 'scaffold-lab', 'validate-after-push', 'deploy-ghpages']);

function agentTypeLabel(type) {
    const map = {
        'fix-ci':              'Fix CI',
        'fix-issue':           'Fix Issue',
        'review-pr':           'Review PR',
        'migrate-gen3':        'Migrate Gen3',
        'scaffold-lab':        'Scaffold Lab',
        'validate-after-push': 'Validate Push',
        'deploy-ghpages':      'Deploy Pages',
    };
    return map[type] || type;
}

async function loadAgentic() {
    await Promise.all([loadAgenticRunning(), loadAgenticFailed(), loadAgenticHistory()]);
}

async function loadAgenticRunning() {
    const body = document.getElementById('agentic-running-body');
    const countEl = document.getElementById('agentic-running-count');
    try {
        const res  = await fetch(`${API}/api/builds/running`);
        const data = await res.json();
        const agents = (data.running || []).filter(r => AGENT_TYPES.has(r.type));
        if (countEl) countEl.textContent = agents.length ? `(${agents.length})` : '';
        if (!agents.length) {
            body.innerHTML = `<tr><td colspan="6" class="loading">No agents running</td></tr>`;
            return;
        }
        const now = Date.now();
        body.innerHTML = agents.map(r => {
            const elapsed = r.started_at
                ? Math.round((now - new Date(r.started_at).getTime()) / 1000)
                : 0;
            const elStr = elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed/60)}m ${elapsed%60}s`;
            const logLink = r.job_id
                ? `<a href="#" class="log-link" data-job-id="${escapeHtml(r.job_id)}" title="View live log">live log</a>`
                : '—';
            return `<tr>
                <td>${escapeHtml(r.repo)}</td>
                <td><code>${escapeHtml(r.branch || r.ref || '')}</code></td>
                <td><span class="agent-type-badge">${escapeHtml(agentTypeLabel(r.type))}</span></td>
                <td>${formatTime(r.started_at)}</td>
                <td>${elStr}</td>
                <td>${logLink}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        body.innerHTML = `<tr><td colspan="6" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

async function loadAgenticFailed() {
    const tbody = document.getElementById('agentic-failed-body');
    const repoFilter = document.getElementById('agentic-repo-filter')?.value || '';
    const archFilter = document.getElementById('agentic-arch-filter')?.value || '';
    try {
        const res  = await fetch(`${API}/api/builds/history?type=integration-test&limit=200`);
        const data = await res.json();
        const rows = data.rows || data;
        let failed = rows.filter(r => r.type === 'integration-test' && !r.passed);

        // Populate repo filter options
        const repoSel = document.getElementById('agentic-repo-filter');
        if (repoSel && repoSel.options.length <= 1) {
            const repos = [...new Set(failed.map(r => r.repo))].sort();
            repos.forEach(rp => {
                const opt = document.createElement('option');
                opt.value = rp; opt.textContent = rp.split('/').pop();
                repoSel.appendChild(opt);
            });
        }

        if (repoFilter) failed = failed.filter(r => r.repo === repoFilter);
        if (archFilter) failed = failed.filter(r => r.arch === archFilter);

        // Deduplicate: keep only the most recent failure per (repo, branch, arch)
        const seen = new Set();
        const deduped = [];
        for (const r of failed) {
            const key = `${r.repo}|${r.branch}|${r.arch}`;
            if (!seen.has(key)) { seen.add(key); deduped.push(r); }
        }
        failed = deduped.slice(0, 50);

        if (!failed.length) {
            tbody.innerHTML = `<tr><td colspan="9" class="loading">No recent failures</td></tr>`;
            return;
        }
        tbody.innerHTML = failed.map(r => {
            const repoShort = r.repo.split('/').pop();
            const failedStep = r.result?.failed_step || r.failed_step || '—';
            const dur = r.result?.duration_seconds != null
                ? `${Math.floor(r.result.duration_seconds / 60)}m ${r.result.duration_seconds % 60}s`
                : '—';
            const logLink = r.job_id
                ? `<a href="#" class="log-link" data-final-job="${escapeHtml(r.job_id)}" title="View log">log</a>`
                : '—';
            const safeJobId    = escapeHtml(r.job_id || '');
            const safeRepo     = escapeHtml(r.repo);
            const safeBranch   = escapeHtml(r.branch || '');
            const safeArch     = escapeHtml(r.arch || '');
            const safeStep     = escapeHtml(failedStep);
            const statusCls = r.status === 'terminated' ? 'status-terminated' : 'status-fail';
            const statusLabel = r.status === 'terminated' ? 'TERM' : 'FAIL';
            return `<tr id="agentic-row-${safeJobId}">
                <td title="${safeRepo}">${escapeHtml(repoShort)}</td>
                <td><code>${safeBranch}</code></td>
                <td><span class="arch-badge">${safeArch}</span></td>
                <td style="font-size:0.8rem">${safeStep}</td>
                <td><span class="${statusCls}">${statusLabel}</span></td>
                <td title="${escapeHtml(r.finished_at || '')}">${formatTime(r.finished_at)}</td>
                <td>${dur}</td>
                <td>${logLink}</td>
                <td><button class="btn btn-small btn-agent" data-action
                    onclick="triggerAgentFixCI('${safeJobId}','${safeRepo}','${safeBranch}','${safeArch}','${safeStep}',this)"
                    title="Ask Claude to analyse and fix this failure">Fix with AI</button></td>
            </tr>`;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="9" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

async function loadAgenticHistory() {
    const tbody = document.getElementById('agentic-history-body');
    try {
        const res  = await fetch(`${API}/api/builds/history?type=all&limit=200`);
        const data = await res.json();
        const rows = data.rows || data;
        const agents = rows.filter(r => AGENT_TYPES.has(r.type)).slice(0, 30);
        if (!agents.length) {
            tbody.innerHTML = `<tr><td colspan="7" class="loading">No agent runs yet</td></tr>`;
            return;
        }
        tbody.innerHTML = agents.map(r => {
            const repoShort = r.repo.split('/').pop();
            const statusCls = r.status === 'terminated' ? 'status-terminated'
                : r.status === 'failed' ? 'status-fail' : 'status-pass';
            const statusLabel = r.status === 'terminated' ? 'TERM'
                : r.status === 'failed' ? 'FAIL' : 'OK';
            const dur = r.result?.duration_seconds != null
                ? `${Math.floor(r.result.duration_seconds / 60)}m ${r.result.duration_seconds % 60}s`
                : '—';
            const logLink = r.job_id
                ? `<a href="#" class="log-link" data-final-job="${escapeHtml(r.job_id)}" title="View log">log</a>`
                : '—';
            return `<tr>
                <td title="${escapeHtml(r.started_at || '')}">${formatTime(r.started_at)}</td>
                <td title="${escapeHtml(r.repo)}">${escapeHtml(repoShort)}</td>
                <td><code>${escapeHtml(r.branch || r.ref || '')}</code></td>
                <td><span class="agent-type-badge">${escapeHtml(agentTypeLabel(r.type))}</span></td>
                <td><span class="${statusCls}">${statusLabel}</span></td>
                <td>${dur}</td>
                <td>${logLink}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

async function triggerAgentFixCI(failedJobId, repo, branch, arch, failedStep, btnEl) {
    if (!isWriter()) { alert('Sign in as a writer to trigger agent runs.'); return; }
    btnEl.disabled = true;
    btnEl.textContent = 'Queuing…';
    try {
        const res = await fetch(`${API}/api/agent/fix-ci`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                failed_job_id: failedJobId,
                repo, branch, arch,
                failed_step: failedStep,
            }),
        });
        if (!res.ok) {
            const txt = await res.text();
            btnEl.disabled = false;
            btnEl.textContent = 'Fix with AI';
            alert(`Error ${res.status}: ${txt}`);
            return;
        }
        const data = await res.json();
        btnEl.textContent = '✓ Queued';
        btnEl.classList.add('btn-success');
        // Show a link to the live log next to the button
        const td = btnEl.closest('td');
        if (td) {
            td.insertAdjacentHTML('beforeend',
                ` <a href="#" class="log-link" data-job-id="${escapeHtml(data.job_id)}" title="View live log">live log</a>`);
        }
        // Refresh running agents section
        setTimeout(loadAgenticRunning, 1500);
    } catch (e) {
        btnEl.disabled = false;
        btnEl.textContent = 'Fix with AI';
        alert(`Error: ${e}`);
    }
}

// Refresh agentic running section when that tab is active
document.getElementById('agentic-refresh')?.addEventListener('click', loadAgentic);

// ── Init ────────────────────────────────────────────────────────────────────

(async () => {
    await loadAuthState();   // resolves signedIn + role before fleet renders
    checkHealth();
    loadFleet();
    loadFleetTriggerPanel();
    loadWorkers();
    loadNightly();
})();

// Auto-refresh
setInterval(() => { checkHealth(); loadWorkers(); }, 30000);
setInterval(loadRunning, 5000);    // spinner liveness
setInterval(loadFleet, 120000);
// Refresh running detail when that tab is active
setInterval(() => {
    const active = document.querySelector('.tab.active')?.dataset.view;
    if (active === 'running') loadRunningDetail();
    if (active === 'agentic') loadAgenticRunning();
}, 5000);
