/* Enablement Ops Dashboard — Client */

const API = '';

function showToast(message, duration = 4000) {
    let toast = document.getElementById('ops-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'ops-toast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('visible');
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => toast.classList.remove('visible'), duration);
}

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
    if (view === 'framework') loadFramework();
    if (view === 'content') loadContent();
    if (view === 'register') loadRegister();
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
        const sym = '|';
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
    // Label shows the ACTION the click performs (not the current state): when wrapping,
    // the button offers "NoWrap"; when not wrapping, it offers "Wrap". (Previously these
    // were swapped — the button showed the current state, which read backwards to users.)
    if (btn) btn.textContent = wrap ? '→ NoWrap' : '↩ Wrap';
}

function openLiveLog(jobId, title, isAgent = false) {
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
    pre.innerHTML = `<em style="color:var(--text-muted)">${isAgent ? 'Loading agent log…' : 'Initializing isolation container…'}</em>`;
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
            // Try livelog first (running job). On 404 try the final log.
            // If the final log is also absent the job is still setting up —
            // keep polling so we don't go dark during the Sysbox setup phase.
            let res = await fetch(`/api/jobs/${jobId}/livelog`);
            if (res.status === 404) {
                const finalRes = await fetch(`/api/jobs/${jobId}/log`);
                if (finalRes.ok) {
                    // Job finished — show final log and stop polling.
                    res = finalRes;
                    if (livelogPoll) { clearInterval(livelogPoll); livelogPoll = null; }
                    currentJobIsLive = false;
                    if (termBtn) termBtn.hidden = true;
                    if (shellBtn) shellBtn.hidden = true;
                } else {
                    // Both 404 — job is still in setup phase. Keep polling.
                    return;
                }
            } else if (res.ok) {
                currentJobIsLive = true;
                if (!isAgent) {
                    if (termBtn) termBtn.hidden = !isWriter();
                    if (shellBtn) shellBtn.hidden = !isWriter();
                    livelogPollCount++;
                    // Load on first live poll; refresh every ~30 s (15 × 2 s) to pick up new apps.
                    if (!livelogAppTabsLoaded || livelogPollCount % 15 === 0) {
                        livelogAppTabsLoaded = true;
                        _loadLivelogAppTabs(jobId);
                    }
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

    // Plain log-link with data-job-id (agent running jobs, post-queue links)
    const jobIdLink = e.target.closest('a.log-link[data-job-id]');
    if (jobIdLink && !e.target.closest('a.running-dot')) {
        e.preventDefault();
        const row = jobIdLink.closest('tr');
        const label = row ? (row.querySelector('td')?.textContent?.trim() || '') : '';
        openLiveLog(jobIdLink.dataset.jobId, label || jobIdLink.dataset.jobId, !!jobIdLink.dataset.agent);
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
        openLiveLog(finalLink.dataset.finalJob, title, !!finalLink.dataset.agent);
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
            const _pctBar = (val, label, warnAt = 80) => {
                if (val == null || val === '') return '';
                const pct = parseFloat(val);
                if (isNaN(pct)) return '';
                const color = pct >= warnAt ? 'var(--red)' : pct >= 60 ? 'var(--yellow, #f5a623)' : 'var(--green)';
                return `<div style="margin-top:4px">
                    <div style="display:flex;justify-content:space-between;font-size:0.72rem;color:var(--text-muted)">
                        <span>${label}</span><span>${pct}%</span>
                    </div>
                    <div style="background:var(--bg-2);border-radius:3px;height:5px;overflow:hidden">
                        <div style="width:${Math.min(pct,100)}%;height:100%;background:${color};transition:width 1s"></div>
                    </div>
                </div>`;
            };
            const metricsHtml = [
                _pctBar(w.cpu_pct, 'CPU'),
                _pctBar(w.mem_pct, `Mem${w.mem_used_gb ? ` (${w.mem_used_gb}/${w.mem_total_gb} GB)` : ''}`),
                _pctBar(w.disk_pct, 'Disk', 90),
                w.containers_running != null && w.containers_running !== ''
                    ? `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px">Containers: ${escapeHtml(String(w.containers_running))}</div>`
                    : '',
            ].filter(Boolean).join('');
            return `
                <div class="worker-card ${isMaster ? 'is-master' : ''} ${stale ? 'offline' : ''}">
                    <h4>${escapeHtml(w.worker_id)} ${badge} ${statusPill}</h4>
                    <div class="meta">
                        <div>Arch: <strong>${escapeHtml(w.arch || '')}</strong></div>
                        <div>Active: ${escapeHtml(String(w.active_jobs || '0'))} / ${escapeHtml(String(w.capacity || '?'))}</div>
                        <div>Last heartbeat: ${formatTime(w.last_heartbeat)}</div>
                        ${masterExtras}
                    </div>
                    ${metricsHtml}
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

let _nightlyAllResults = [];

async function loadNightlyRuns() {
    const res = await fetch(`${API}/api/nightly/runs`);
    const data = await res.json();
    const sel = document.getElementById('nightly-run-select');
    if (!sel) return;
    const runs = data.runs || [];
    sel.innerHTML = `<option value="latest">Latest run</option>` +
        runs.map(r => {
            const d = r.run_id.replace('nightly-', '').replace(/-\d{6}$/, '');
            return `<option value="${escapeHtml(r.run_id)}">${escapeHtml(d)} (${r.passed}✓ ${r.failed}✗)</option>`;
        }).join('');
}

async function loadNightly(runId) {
    runId = runId || document.getElementById('nightly-run-select')?.value || 'latest';
    const endpoint = runId === 'latest' ? `${API}/api/nightly/latest` : `${API}/api/nightly/run/${encodeURIComponent(runId)}`;
    const res = await fetch(endpoint);
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

    _nightlyAllResults = data.results || [];
    applyNightlyFilter();

    // Load common-error summary if there are failures
    const failCount = data.failed || 0;
    const panel = document.getElementById('nightly-errors-panel');
    if (panel) {
        if (failCount > 0 && data.run_id) {
            panel.hidden = false;
            document.getElementById('nightly-errors-body').innerHTML = '<span style="color:var(--text-muted)">Analysing failure patterns…</span>';
            loadNightlyErrorSummary(data.run_id);
        } else {
            panel.hidden = true;
        }
    }
}

function applyNightlyFilter() {
    const filter = document.getElementById('nightly-status-filter')?.value || 'all';
    const filtered = filter === 'all' ? _nightlyAllResults
        : _nightlyAllResults.filter(j => {
            const passed = j.result?.passed;
            const term = j.status === 'terminated';
            if (filter === 'passed') return passed && !term;
            if (filter === 'failed') return !passed || term;
            return true;
        });

    const tbody = document.getElementById('nightly-body');
    if (!filtered.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="loading">No results match filter</td></tr>`;
        return;
    }
    tbody.innerHTML = filtered.map(job => {
        const r = job.result || {};
        const arch = r.arch || job.worker_arch || '?';
        const isTerminated = job.status === 'terminated';
        const cls = isTerminated ? 'status-terminated' : (r.passed ? 'status-pass' : 'status-fail');
        const label = isTerminated ? 'TERM' : (r.passed ? 'PASS' : 'FAIL');
        const status = job.job_id
            ? `<a href="#" class="${cls} log-link" data-final-job="${escapeHtml(job.job_id)}" title="View log">${label}</a>`
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

async function loadNightlyErrorSummary(runId) {
    const body = document.getElementById('nightly-errors-body');
    if (!body) return;
    try {
        const res = await fetch(`${API}/api/nightly/run/${encodeURIComponent(runId)}/summary`);
        if (!res.ok) { body.innerHTML = '<span style="color:var(--text-muted)">Could not load error summary.</span>'; return; }
        const data = await res.json();
        const patterns = data.patterns || [];
        if (!patterns.length) {
            body.innerHTML = '<span style="color:var(--text-muted)">No common patterns found in failure logs.</span>';
            return;
        }
        body.innerHTML = patterns.map(p => `
            <div style="display:flex;gap:8px;align-items:baseline;margin-bottom:4px;border-left:3px solid var(--red);padding-left:8px">
                <span class="status-fail" style="font-size:0.75rem;flex-shrink:0">${p.count}×</span>
                <code style="font-size:0.75rem;color:var(--text-2);white-space:pre-wrap;word-break:break-all">${escapeHtml(p.line)}</code>
            </div>`).join('');
    } catch (e) {
        body.innerHTML = `<span style="color:var(--text-muted)">Error: ${escapeHtml(String(e))}</span>`;
    }
}

// ── Framework View ───────────────────────────────────────────────────────────

let _frameworkSuitesData = [];

async function loadFramework() {
    const [suitesRes, runsRes] = await Promise.all([
        fetch(`${API}/api/framework/suites`),
        fetch(`${API}/api/framework/runs`),
    ]);
    const suitesData = await suitesRes.json();
    const runsData = await runsRes.json();
    _frameworkSuitesData = suitesData.suites || [];
    renderFrameworkSuites(_frameworkSuitesData);
    renderFrameworkRuns(runsData.runs || []);
}

function renderFrameworkSuites(suites) {
    const grid = document.getElementById('framework-suite-grid');
    if (!grid) return;
    grid.innerHTML = suites.map(s => {
        const last = s.last;
        const comingSoon = s.status === 'coming_soon';
        const needsVM = s.requires_native;
        let resultHtml = '<span class="status-terminated">—</span>';
        let metaHtml = '<span style="color:var(--text-muted);font-size:0.75rem">Never run</span>';
        if (last) {
            const passed = last.passed === 'true';
            resultHtml = passed
                ? `<a href="#" class="status-pass log-link" data-job-id="${escapeHtml(last.job_id)}" title="View log">✅ PASS</a>`
                : `<a href="#" class="status-fail log-link" data-job-id="${escapeHtml(last.job_id)}" title="View log">❌ FAIL</a>`;
            const ts = last.timestamp ? formatTime(last.timestamp) : '';
            metaHtml = `<span style="color:var(--text-muted);font-size:0.75rem">${escapeHtml(last.arch)} · ${last.duration_s}s · ${ts}</span>`;
        }
        const badges = [
            needsVM ? '<span class="badge badge-warn">needs VM</span>' : null,
            s.needs_creds ? '<span class="badge badge-info">DT creds</span>' : null,
            comingSoon ? '<span class="badge badge-muted">coming soon</span>' : null,
        ].filter(Boolean).join(' ');
        const btnAttrs = comingSoon ? 'disabled title="Not yet implemented"' : `data-action data-suite="${escapeHtml(s.id)}"`;
        return `<div class="framework-card">
            <div class="framework-card-header">
                <span class="framework-card-name">${escapeHtml(s.name)}</span>
                ${badges}
            </div>
            <p class="framework-card-desc">${escapeHtml(s.description)}</p>
            <div class="framework-card-footer">
                <div>${resultHtml}<br>${metaHtml}</div>
                <button class="btn btn-small ${comingSoon ? 'btn-secondary' : ''} framework-run-btn" ${btnAttrs}>▶ Run</button>
            </div>
        </div>`;
    }).join('');
}

function renderFrameworkRuns(runs) {
    const tbody = document.getElementById('framework-runs-body');
    if (!tbody) return;
    if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="loading">No framework runs yet</td></tr>';
        return;
    }
    tbody.innerHTML = runs.map(r => {
        const cls = r.passed ? 'status-pass' : 'status-fail';
        const label = r.passed ? 'PASS' : 'FAIL';
        const logLink = r.job_id
            ? `<a href="#" class="log-link" data-job-id="${escapeHtml(r.job_id)}" title="View log">log</a>`
            : '—';
        return `<tr>
            <td>${formatTime(r.timestamp)}</td>
            <td>${escapeHtml(r.suite)}</td>
            <td><span class="arch-badge">${escapeHtml(r.arch)}</span></td>
            <td><span class="${cls}">${label}</span></td>
            <td>${r.duration_s || 0}s</td>
            <td>${logLink}</td>
        </tr>`;
    }).join('');
}

async function triggerFrameworkSuite(suiteId, ref) {
    if (!isWriter()) { showToast('⚠️ Sign in as a writer to trigger tests.'); return; }
    ref = ref || document.getElementById('framework-ref')?.value || 'main';
    const label = suiteId === 'all' ? 'all suites' : suiteId;
    showToast(`⏳ Firing ${label} on ${ref}…`);
    try {
        const res = await fetch(`${API}/api/framework/trigger`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ suite: suiteId, ref, arch: suiteId === 'bats' ? 'arm64' : 'amd64' }),
        });
        if (!res.ok) { showToast(`❌ Error ${res.status}: ${await res.text()}`, 6000); return; }
        const data = await res.json();
        if (!data.jobs || !data.jobs.length) {
            showToast('⚠️ Nothing queued (suite coming soon or unknown).', 6000);
            return;
        }
        showToast(`✅ Scheduled on ${data.ref}: ${data.jobs.map(j => `${j.suite} (${j.arch})`).join(', ')}`, 6000);
        setTimeout(loadFramework, 2000);
    } catch (e) { showToast(`❌ Error: ${e}`, 6000); }
}

// Framework tab events
document.addEventListener('click', e => {
    const runBtn = e.target.closest('.framework-run-btn[data-suite]');
    if (runBtn) {
        e.preventDefault();
        triggerFrameworkSuite(runBtn.dataset.suite);
        return;
    }
    const runAll = e.target.closest('#framework-run-all');
    if (runAll) {
        e.preventDefault();
        triggerFrameworkSuite('all');
        return;
    }
    const triggerFw = e.target.closest('#nightly-trigger-framework');
    if (triggerFw) {
        e.preventDefault();
        const ref = prompt('Branch to test?', 'main');
        if (ref) triggerFrameworkSuite('all', ref);
        return;
    }
});

document.getElementById('nightly-run-select')?.addEventListener('change', () => loadNightly());
document.getElementById('nightly-status-filter')?.addEventListener('change', applyNightlyFilter);

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
                <td>${escapeHtml(formatTrigger(r.trigger))}</td>
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
                    <td>
                        <span style="font-size:0.75rem;color:var(--text-muted)">${escapeHtml(r.worker_id || '')}</span>
                        ${r.arena_user ? `<br><span style="font-size:0.7rem;color:#4ec9b0" title="Arena session">👤 ${escapeHtml(r.arena_user)}</span>` : ''}
                        ${r.arena_tenant ? `<br><span style="font-size:0.7rem;color:var(--text-2)" title="Tenant">${escapeHtml(String(r.arena_tenant).split('.')[0])}${r.stage ? ' ' + stageBadge(r.stage) : ''}</span>` : ''}
                        ${r.provider === 'codespace' ? `<br><span style="font-size:0.7rem;color:#a78bfa" title="Runs in the learner's own GitHub Codespace, proxied by Orbital">☁ codespace</span>` : ''}
                    </td>
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
        tbody.innerHTML = `<tr><td colspan="8" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
        if (qtbody)
            qtbody.innerHTML = `<tr><td colspan="7" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
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

// ── Clear Queue button ───────────────────────────────────────────────────────

document.addEventListener('click', async e => {
    const btn = e.target.closest('#btn-clear-queue');
    if (!btn) return;
    e.preventDefault();
    if (!isWriter()) {
        alert('Only org members can clear the queue.');
        return;
    }
    if (!confirm('Remove ALL waiting jobs from both queues?')) return;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch('/api/queue/clear', { method: 'DELETE' });
        if (res.ok) {
            const data = await res.json();
            btn.textContent = `✓ Cleared (${data.total})`;
            setTimeout(() => { btn.disabled = false; btn.textContent = '✕ Clear Queue'; }, 2500);
        } else {
            btn.textContent = `✕ failed (${res.status})`;
            setTimeout(() => { btn.disabled = false; btn.textContent = '✕ Clear Queue'; }, 3000);
        }
    } catch {
        btn.disabled = false; btn.textContent = '✕ Clear Queue';
    }
    loadRunningDetail();
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
    if (view === 'audit')  loadSyncAudit();
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

let syncPRsFailedOnly = false;

function renderSyncPRs() {
    const filter = (document.getElementById('sync-prs-filter').value || '').toLowerCase();
    const tbody = document.getElementById('sync-prs-body');
    const rows = syncPRsData.filter(r => {
        if (syncPRsFailedOnly && r._ci?.overall !== 'fail') return false;
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
        const checksUrl = `${r.url}/checks`;
        let ciBadge = '<span class="ci-badge none">—</span>';
        if (ci) {
            if (ci.overall === 'pass')         ciBadge = '<span class="ci-badge pass">PASS</span>';
            else if (ci.overall === 'fail')    ciBadge = '<span class="ci-badge fail">FAIL</span>';
            else if (ci.overall === 'pending') ciBadge = '<span class="ci-badge pend">PEND</span>';
            else if (ci.overall === 'unknown') ciBadge = `<span class="ci-badge unknown" title="${escapeHtml('CI status unavailable (GitHub API error) — refresh to retry: ' + (ci.error || ''))}">?</span>`;
            ciBadge = `<a href="${escapeHtml(checksUrl)}" target="_blank" rel="noopener" title="View PR checks on GitHub">${ciBadge}</a>`;
        }
        const showFix = isSergioUser && ci?.overall === 'fail';
        const fixBtn = showFix
            ? `<button class="btn btn-small btn-agent fix-pr-btn" data-action
                   data-repo="${escapeHtml(repo)}"
                   data-pr="${escapeHtml(String(r.number))}"
                   data-branch="${escapeHtml(r.headRefName || '')}"
                   data-checks-url="${escapeHtml(checksUrl)}"
                   title="Let AI analyze the failure and fix the repo">Fix with AI</button>`
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
document.getElementById('sync-prs-failed-only').addEventListener('click', () => {
    syncPRsFailedOnly = !syncPRsFailedOnly;
    document.getElementById('sync-prs-failed-only').classList.toggle('active', syncPRsFailedOnly);
    renderSyncPRs();
});

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
            ? `<button class="btn btn-small btn-agent fix-issue-btn" data-action
                   data-repo="${escapeHtml(repo)}"
                   data-issue="${escapeHtml(String(r.number))}"
                   data-title="${escapeHtml(r.title)}"
                   title="Let AI analyze and fix this issue">Fix with AI</button>`
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

// ── Audit sub-tab ────────────────────────────────────────────────────────────

function _scopeAuditCSS(raw) {
    // Drop global reset and body rules, scope everything else to #audit-content
    return raw
        .replace(/\*\s*\{[^}]*\}/g, '')
        .replace(/body\s*\{[^}]*\}/g, '')
        .replace(/([^{}@\n][^{}]*)\{([^{}]*)\}/g, (m, sel, props) => {
            const s = sel.trim();
            if (!s || s === ':root') return m;  // keep :root as-is
            const scoped = s.split(',').map(p => `#audit-content ${p.trim()}`).join(', ');
            return `${scoped} {${props}}`;
        });
}

async function loadSyncAudit(force = false) {
    const container = document.getElementById('audit-content');
    if (!container) return;
    if (!force && container.dataset.loaded) return;
    container.innerHTML = '<div style="padding:1.5rem;color:var(--text-2)">Loading audit…</div>';
    try {
        const res = await fetch('/audit' + (force ? '?t=' + Date.now() : ''));
        const html = await res.text();
        const doc  = new DOMParser().parseFromString(html, 'text/html');
        let css = '';
        doc.querySelectorAll('style').forEach(s => { css += s.textContent; });
        container.innerHTML = `<style>${_scopeAuditCSS(css)}</style>` + doc.body.innerHTML;
        container.dataset.loaded = '1';
    } catch (e) {
        container.innerHTML = `<div style="color:var(--red);padding:1rem">Error loading audit: ${escapeHtml(String(e))}</div>`;
    }
}

document.getElementById('sync-audit-refresh').addEventListener('click', async () => {
    if (!isWriter()) return showToast('Sign in as an org member to regenerate the audit.');
    const btn = document.getElementById('sync-audit-refresh');
    btn.disabled = true;
    btn.textContent = 'Fetching from GitHub…';
    showToast('Pulling fresh data from GitHub (~2 min)…');
    try {
        const res = await fetch(`${API}/api/audit/refresh`, {
            method: 'POST',
            credentials: 'same-origin',
        });
        if (res.ok) {
            const data = await res.json();
            showToast(data.message || 'Audit refreshed.');
            delete document.getElementById('audit-content').dataset.loaded;
            await loadSyncAudit(true);
        } else {
            showToast('Audit refresh failed — check server logs.');
        }
    } catch (e) {
        showToast('Failed to regenerate audit.');
    } finally {
        btn.disabled = false;
        btn.textContent = '↻ Regenerate';
    }
});

// ── Fix with AI modal ────────────────────────────────────────────────────────

let fixAiContext = null;  // { type: 'pr'|'issue'|'ci', repo, ... }

function openFixWithAI(type, data) {
    fixAiContext = { type, ...data };
    const modal = document.getElementById('fix-ai-modal');
    const title = document.getElementById('fix-ai-title');
    const desc  = document.getElementById('fix-ai-description');
    const ciInfo = document.getElementById('fix-ai-ci-info');
    document.getElementById('fix-ai-instructions').value = '';

    if (type === 'pr') {
        title.textContent = `Fix with AI — PR #${data.number} · ${data.repo.split('/').pop()}`;
        desc.textContent = 'The AI agent will fetch the failed integration test log from GitHub, determine whether the root cause is in this repo or the shared framework, then apply a surgical fix. If the framework is at fault, the PR stays open and you will be notified. If the repo is at fault, a fix is committed to this branch and a new CI run is triggered.';
        if (data.checksUrl) {
            ciInfo.hidden = false;
            ciInfo.innerHTML = `Failed checks: <a href="${escapeHtml(data.checksUrl)}" target="_blank" rel="noopener">View on GitHub ↗</a>`;
        } else {
            ciInfo.hidden = true;
        }
    } else if (type === 'ci') {
        const repoShort = data.repo.split('/').pop();
        title.textContent = `Fix with AI — ${repoShort} [${data.arch}]`;
        desc.textContent = `The AI agent will read the failed test log, determine whether the root cause is in this repo or the shared framework, then commit a surgical fix to a new branch and open a PR.`;
        ciInfo.hidden = false;
        ciInfo.textContent = `Job: ${data.jobId || '—'} · Branch: ${data.branch || 'main'} · Step: ${data.failedStep || '—'}`;
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
        } else if (fixAiContext.type === 'ci') {
            endpoint = '/api/agent/fix-ci';
            payload = {
                failed_job_id: fixAiContext.jobId,
                repo: fixAiContext.repo,
                branch: fixAiContext.branch || 'main',
                arch: fixAiContext.arch,
                failed_step: fixAiContext.failedStep,
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
        const ctx = fixAiContext;
        closeFixWithAI();
        const repoShort = ctx.repo.split('/').pop();
        // Update the originating CI table button if present
        if (ctx.type === 'ci' && ctx._btn) {
            ctx._btn.textContent = '✓ Queued';
            ctx._btn.classList.add('btn-success');
            ctx._btn.disabled = true;
            const td = ctx._btn.closest('td');
            if (td && data.job_id) {
                td.insertAdjacentHTML('beforeend',
                    ` <a href="#" class="log-link" data-job-id="${escapeHtml(data.job_id)}" data-agent="true" title="View log">log</a>`);
            }
        }
        activateTab('agentic');
        setTimeout(loadAgenticRunning, 1200);
        showToast(`Agent queued for ${repoShort} — visible in the Agentic tab`);
    } catch (e) {
        alert('Network error: ' + e);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Submit fix';
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
            checksUrl: prBtn.dataset.checksUrl,
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

function formatTrigger(trigger) {
    // "arena" was the old name for the enablement-app provisioner; the Arena product
    // was renamed to the Enablement App. Map both the legacy value (old Redis jobs) and
    // the new value to a single friendly label so the History "Trigger" column is consistent.
    if (trigger === 'arena' || trigger === 'enablement-app') return 'Enablement App';
    return trigger || '';
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
            btn.dataset.proxyUrl = app.subdomain_url || app.proxy_url;
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
        const absUrl = proxyUrl.startsWith('http') ? proxyUrl : location.origin + proxyUrl;
        if (frame.src !== absUrl) {
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
            btn.dataset.proxyUrl = app.subdomain_url || app.proxy_url;
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
        const absUrl = proxyUrl.startsWith('http') ? proxyUrl : location.origin + proxyUrl;
        if (frame.src !== absUrl) {
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
            'width=1280,height=1200,menubar=no,toolbar=no,location=no,status=no,scrollbars=no,resizable=yes');
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

// ── Agentic redesign ─────────────────────────────────────────────────────────

let activeAgentView = 'history';
let agentHistoryData = [];
let agentFailedData  = [];
let agentPRsData     = [];
let agentIssuesData  = [];

// Agent sub-tab click handler
document.addEventListener('click', e => {
    const atab = e.target.closest('.agent-tab');
    if (!atab) return;
    const view = atab.dataset.agentView;
    if (!view) return;
    activeAgentView = view;
    document.querySelectorAll('.agent-tab').forEach(t => t.classList.toggle('active', t === atab));
    document.querySelectorAll('.agent-subview').forEach(sv => sv.hidden = true);
    document.getElementById(`agent-view-${view}`).hidden = false;
    if (view === 'history') loadAgentHistory();
    if (view === 'failed')  loadAgentFailed();
    if (view === 'prs')     loadAgentPRs();
    if (view === 'issues')  loadAgentIssues();
});

async function loadAgentic() {
    await loadAgenticRunning();
    document.querySelectorAll('.agent-subview').forEach(sv => sv.hidden = true);
    document.getElementById(`agent-view-${activeAgentView}`).hidden = false;
    document.querySelectorAll('.agent-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.agentView === activeAgentView);
    });
    if (activeAgentView === 'history') loadAgentHistory();
    else if (activeAgentView === 'failed')  loadAgentFailed();
    else if (activeAgentView === 'prs')     loadAgentPRs();
    else if (activeAgentView === 'issues')  loadAgentIssues();
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
                ? `<a href="#" class="log-link" data-job-id="${escapeHtml(r.job_id)}" data-agent="true" title="View log">log</a>`
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

// ── Agent History ─────────────────────────────────────────────────────────────

async function loadAgentHistory() {
    if (!agentHistoryData.length) {
        const tbody = document.getElementById('agent-history-body');
        tbody.innerHTML = `<tr><td colspan="7" class="loading">Loading…</td></tr>`;
        try {
            const res  = await fetch(`${API}/api/builds/history?type=all&limit=200`);
            const data = await res.json();
            const rows = data.rows || data;
            agentHistoryData = rows.filter(r => AGENT_TYPES.has(r.type));
        } catch (e) {
            document.getElementById('agent-history-body').innerHTML =
                `<tr><td colspan="7" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
            return;
        }
    }
    renderAgentHistory();
}

function renderAgentHistory(limit = 10) {
    const tbody   = document.getElementById('agent-history-body');
    const moreDiv = document.getElementById('agent-history-more');
    const filter  = (document.getElementById('agent-history-filter')?.value || '').toLowerCase();

    let items = agentHistoryData;
    if (filter) items = items.filter(r =>
        (r.repo || '').toLowerCase().includes(filter) ||
        (r.status || '').toLowerCase().includes(filter) ||
        (r.type || '').toLowerCase().includes(filter)
    );

    const total = items.length;
    if (!total) {
        tbody.innerHTML = `<tr><td colspan="7" class="loading">No agent runs yet</td></tr>`;
        moreDiv.hidden = true;
        return;
    }

    tbody.innerHTML = items.slice(0, limit).map(r => {
        const repoShort  = r.repo.split('/').pop();
        const statusCls  = r.status === 'terminated' ? 'status-terminated'
            : r.status === 'failed' ? 'status-fail' : 'status-pass';
        const statusLabel = r.status === 'terminated' ? 'TERM'
            : r.status === 'failed' ? 'FAIL' : 'OK';
        const dur = r.result?.duration_seconds != null
            ? `${Math.floor(r.result.duration_seconds / 60)}m ${r.result.duration_seconds % 60}s`
            : '—';
        const logLink    = r.job_id
            ? `<a href="#" class="log-link" data-final-job="${escapeHtml(r.job_id)}" data-agent="true" title="View log">log</a>`
            : '—';
        const instrTitle = r.instructions ? ` title="${escapeHtml(r.instructions)}"` : '';
        const instrMark  = r.instructions ? `<span class="instr-dot"${instrTitle}>✎</span> ` : '';
        return `<tr>
            <td title="${escapeHtml(r.started_at || '')}">${formatTime(r.started_at)}</td>
            <td title="${escapeHtml(r.repo)}">${escapeHtml(repoShort)}</td>
            <td><code>${escapeHtml(r.branch || r.ref || '')}</code></td>
            <td><span class="agent-type-badge">${escapeHtml(agentTypeLabel(r.type))}</span></td>
            <td>${instrMark}<span class="${statusCls}">${statusLabel}</span></td>
            <td>${dur}</td>
            <td>${logLink}</td>
        </tr>`;
    }).join('');

    if (total > limit) {
        moreDiv.hidden = false;
        moreDiv.innerHTML = `<button class="btn btn-small btn-secondary" onclick="renderAgentHistory(${total})">Show ${total - limit} more</button>`;
    } else {
        moreDiv.hidden = true;
    }
}

document.getElementById('agent-history-filter')?.addEventListener('input', () => renderAgentHistory());
document.getElementById('agent-history-refresh')?.addEventListener('click', () => { agentHistoryData = []; loadAgentHistory(); });

// ── Agent Failed Tests ────────────────────────────────────────────────────────

async function loadAgentFailed() {
    if (!agentFailedData.length) {
        const tbody = document.getElementById('agent-failed-body');
        tbody.innerHTML = `<tr><td colspan="9" class="loading">Loading…</td></tr>`;
        try {
            const res  = await fetch(`${API}/api/builds/history?type=integration-test&limit=200`);
            const data = await res.json();
            const rows = data.rows || data;
            const failed = rows.filter(r => r.type === 'integration-test' && !r.passed);
            const seen = new Set();
            for (const r of failed) {
                const key = `${r.repo}|${r.branch}|${r.arch}`;
                if (!seen.has(key)) { seen.add(key); agentFailedData.push(r); }
            }
        } catch (e) {
            document.getElementById('agent-failed-body').innerHTML =
                `<tr><td colspan="9" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
            return;
        }
    }
    renderAgentFailed();
}

function renderAgentFailed(limit = 10) {
    const tbody   = document.getElementById('agent-failed-body');
    const moreDiv = document.getElementById('agent-failed-more');
    const filter  = (document.getElementById('agent-failed-filter')?.value || '').toLowerCase();
    const arch    = document.getElementById('agent-failed-arch')?.value || '';

    let items = agentFailedData;
    if (filter) items = items.filter(r => (r.repo || '').toLowerCase().includes(filter));
    if (arch)   items = items.filter(r => r.arch === arch);

    const total = items.length;
    if (!total) {
        tbody.innerHTML = `<tr><td colspan="9" class="loading">No recent failures</td></tr>`;
        moreDiv.hidden = true;
        return;
    }

    tbody.innerHTML = items.slice(0, limit).map(r => {
        const repoShort  = r.repo.split('/').pop();
        const failedStep = r.result?.failed_step || r.failed_step || '—';
        const dur = r.result?.duration_seconds != null
            ? `${Math.floor(r.result.duration_seconds / 60)}m ${r.result.duration_seconds % 60}s`
            : '—';
        const logLink    = r.job_id
            ? `<a href="#" class="log-link" data-final-job="${escapeHtml(r.job_id)}" data-agent="true" title="View log">log</a>`
            : '—';
        const safeJobId  = escapeHtml(r.job_id || '');
        const safeRepo   = escapeHtml(r.repo);
        const safeBranch = escapeHtml(r.branch || '');
        const safeArch   = escapeHtml(r.arch || '');
        const safeStep   = escapeHtml(failedStep);
        const statusCls  = r.status === 'terminated' ? 'status-terminated' : 'status-fail';
        const statusLabel = r.status === 'terminated' ? 'TERM' : 'FAIL';
        return `<tr>
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

    if (total > limit) {
        moreDiv.hidden = false;
        moreDiv.innerHTML = `<button class="btn btn-small btn-secondary" onclick="renderAgentFailed(${total})">Show ${total - limit} more</button>`;
    } else {
        moreDiv.hidden = true;
    }
}

document.getElementById('agent-failed-filter')?.addEventListener('input', () => renderAgentFailed());
document.getElementById('agent-failed-arch')?.addEventListener('change', () => renderAgentFailed());
document.getElementById('agent-failed-refresh')?.addEventListener('click', () => { agentFailedData = []; loadAgentFailed(); });

// ── Agent Failed PRs ──────────────────────────────────────────────────────────

async function loadAgentPRs() {
    if (!agentPRsData.length) {
        const tbody = document.getElementById('agent-prs-body');
        tbody.innerHTML = `<tr><td colspan="7" class="loading">Fetching PRs…</td></tr>`;
        try {
            const res  = await fetch(`${API}/api/sync/prs`);
            const data = await res.json();
            if (data.error) {
                tbody.innerHTML = `<tr><td colspan="7" class="loading" style="color:var(--red)">Error: ${escapeHtml(data.error)}</td></tr>`;
                return;
            }
            const all = Array.isArray(data.rows) ? data.rows : [];
            agentPRsData = all.filter(r => r._ci?.overall === 'fail');
        } catch (e) {
            document.getElementById('agent-prs-body').innerHTML =
                `<tr><td colspan="7" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
            return;
        }
    }
    renderAgentPRs();
}

function renderAgentPRs(limit = 10) {
    const tbody   = document.getElementById('agent-prs-body');
    const moreDiv = document.getElementById('agent-prs-more');
    const filter  = (document.getElementById('agent-prs-filter')?.value || '').toLowerCase();

    let items = agentPRsData;
    if (filter) items = items.filter(r => {
        const title = (r.title || '').toLowerCase();
        const repo  = (r.repository?.nameWithOwner || r.repository?.name || '').toLowerCase();
        return title.includes(filter) || repo.includes(filter);
    });

    const total = items.length;
    if (!total) {
        tbody.innerHTML = `<tr><td colspan="7" class="loading">No failed PRs</td></tr>`;
        moreDiv.hidden = true;
        return;
    }

    const isSergioUser = authState.user === 'sergiohinojosa';
    tbody.innerHTML = items.slice(0, limit).map(r => {
        const repo      = r.repository?.nameWithOwner || r.repository?.name || '—';
        const author    = r.author?.login || r.author || '—';
        const updated   = formatTime(r.updatedAt || r.updated_at);
        const checksUrl = `${r.url}/checks`;
        const ciBadge   = `<a href="${escapeHtml(checksUrl)}" target="_blank" rel="noopener"><span class="ci-badge fail">FAIL</span></a>`;
        const fixBtn    = isSergioUser
            ? `<button class="btn btn-small btn-agent fix-pr-btn" data-action
                   data-repo="${escapeHtml(repo)}"
                   data-pr="${escapeHtml(String(r.number))}"
                   data-branch="${escapeHtml(r.headRefName || '')}"
                   data-checks-url="${escapeHtml(checksUrl)}"
                   title="Let AI analyze the failure and fix the repo">Fix with AI</button>`
            : '—';
        return `<tr>
            <td><a href="https://github.com/${escapeHtml(repo)}" target="_blank" rel="noopener">${escapeHtml(repo.split('/').pop())}</a></td>
            <td><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">#${escapeHtml(String(r.number))}</a></td>
            <td>${escapeHtml(r.title)}</td>
            <td>${escapeHtml(String(author))}</td>
            <td>${ciBadge}</td>
            <td>${updated}</td>
            <td>${fixBtn}</td>
        </tr>`;
    }).join('');

    if (total > limit) {
        moreDiv.hidden = false;
        moreDiv.innerHTML = `<button class="btn btn-small btn-secondary" onclick="renderAgentPRs(${total})">Show ${total - limit} more</button>`;
    } else {
        moreDiv.hidden = true;
    }
}

document.getElementById('agent-prs-filter')?.addEventListener('input', () => renderAgentPRs());
document.getElementById('agent-prs-refresh')?.addEventListener('click', () => { agentPRsData = []; loadAgentPRs(); });

// ── Agent Open Issues ─────────────────────────────────────────────────────────

async function loadAgentIssues() {
    if (!agentIssuesData.length) {
        const tbody = document.getElementById('agent-issues-body');
        tbody.innerHTML = `<tr><td colspan="7" class="loading">Fetching issues…</td></tr>`;
        try {
            const res  = await fetch(`${API}/api/sync/issues`);
            const data = await res.json();
            if (data.error) {
                tbody.innerHTML = `<tr><td colspan="7" class="loading" style="color:var(--red)">Error: ${escapeHtml(data.error)}</td></tr>`;
                return;
            }
            agentIssuesData = Array.isArray(data.rows) ? data.rows : [];
        } catch (e) {
            document.getElementById('agent-issues-body').innerHTML =
                `<tr><td colspan="7" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
            return;
        }
    }
    renderAgentIssues();
}

function renderAgentIssues(limit = 10) {
    const tbody   = document.getElementById('agent-issues-body');
    const moreDiv = document.getElementById('agent-issues-more');
    const filter  = (document.getElementById('agent-issues-filter')?.value || '').toLowerCase();

    let items = agentIssuesData;
    if (filter) items = items.filter(r => {
        const title = (r.title || '').toLowerCase();
        const repo  = (r.repository?.nameWithOwner || r.repository?.name || '').toLowerCase();
        return title.includes(filter) || repo.includes(filter);
    });

    const total = items.length;
    if (!total) {
        tbody.innerHTML = `<tr><td colspan="7" class="loading">No open issues</td></tr>`;
        moreDiv.hidden = true;
        return;
    }

    const isSergioUser = authState.user === 'sergiohinojosa';
    tbody.innerHTML = items.slice(0, limit).map(r => {
        const repo    = r.repository?.nameWithOwner || r.repository?.name || '—';
        const author  = r.author?.login || r.author || '—';
        const labels  = (r.labels || []).map(l => `<span class="label-chip">${escapeHtml(l.name || l)}</span>`).join(' ');
        const updated = formatTime(r.updatedAt || r.updated_at);
        const fixBtn  = isSergioUser
            ? `<button class="btn btn-small btn-agent fix-issue-btn" data-action
                   data-repo="${escapeHtml(repo)}"
                   data-issue="${escapeHtml(String(r.number))}"
                   data-title="${escapeHtml(r.title)}"
                   title="Let AI analyze and fix this issue">Fix with AI</button>`
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

    if (total > limit) {
        moreDiv.hidden = false;
        moreDiv.innerHTML = `<button class="btn btn-small btn-secondary" onclick="renderAgentIssues(${total})">Show ${total - limit} more</button>`;
    } else {
        moreDiv.hidden = true;
    }
}

document.getElementById('agent-issues-filter')?.addEventListener('input', () => renderAgentIssues());
document.getElementById('agent-issues-refresh')?.addEventListener('click', () => { agentIssuesData = []; loadAgentIssues(); });

function triggerAgentFixCI(failedJobId, repo, branch, arch, failedStep, btnEl) {
    if (!isWriter()) { alert('Sign in as a writer to trigger agent runs.'); return; }
    openFixWithAI('ci', { jobId: failedJobId, repo, branch, arch, failedStep, _btn: btnEl });
}

// ── Init ────────────────────────────────────────────────────────────────────

(async () => {
    await loadAuthState();   // resolves signedIn + role before fleet renders
    checkHealth();
    loadFleet();
    loadFleetTriggerPanel();
    loadWorkers();
    loadNightly();
    loadNightlyRuns();
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

// ── Help modal ───────────────────────────────────────────────────────────────

function openHelp() {
    document.getElementById('help-modal').hidden = false;
}

function closeHelp() {
    document.getElementById('help-modal').hidden = true;
}

document.getElementById('help-btn').addEventListener('click', openHelp);
document.getElementById('help-close').addEventListener('click', closeHelp);

// Close on backdrop click
document.getElementById('help-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('help-modal')) closeHelp();
});

// Keyboard: ? opens, Esc closes
document.addEventListener('keydown', e => {
    const inInput = ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName);
    if (!inInput && e.key === '?' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        document.getElementById('help-modal').hidden
            ? openHelp()
            : closeHelp();
        return;
    }
    if (e.key === 'Escape' && !document.getElementById('help-modal').hidden) {
        closeHelp();
    }
});

// ── Content Service tab ──────────────────────────────────────────────────────
const csState = { profiles: [], map: { defaults: {}, tenants: {} }, domains: [], catalog: [] };
let csWired = false;

const csProfileOpts = (sel) =>
    csState.profiles.map(p => `<option ${p.profileId === sel ? 'selected' : ''}>${escapeHtml(p.profileId)}</option>`).join('');

function wireContent() {
    if (csWired) return; csWired = true;
    document.querySelectorAll('.content-tab').forEach(t => t.addEventListener('click', () => {
        document.querySelectorAll('.content-tab').forEach(x => x.classList.toggle('active', x === t));
        document.querySelectorAll('.content-subview').forEach(v => { v.hidden = (v.id !== 'content-view-' + t.dataset.contentView); });
        if (t.dataset.contentView === 'trainings') csLoadSources();
    }));
    document.getElementById('cs-save-profile').addEventListener('click', csSaveProfile);
    document.getElementById('cs-clear-profile').addEventListener('click', () => csEditProfile(null));
    document.getElementById('cs-resolve').addEventListener('click', csResolve);
    document.getElementById('cs-add-tenant').addEventListener('click', csAddTenant);
    document.getElementById('cs-save-delivery').addEventListener('click', csSaveDelivery);
    document.getElementById('cs-src-validate').addEventListener('click', () => csSource('validate'));
    document.getElementById('cs-src-add').addEventListener('click', () => csSource('add'));
    document.querySelector('#cs-sources tbody').addEventListener('click', (e) => {
        const rm = e.target.closest('[data-cs-srcdel]'); if (rm) csRemoveSource(rm.dataset.csSrcdel);
    });
    document.getElementById('content-profiles').addEventListener('click', (e) => {
        const ed = e.target.closest('[data-cs-edit]'); if (ed) { csEditProfile(ed.dataset.csEdit); return; }
        const dl = e.target.closest('[data-cs-del]'); if (dl) { csDeleteProfile(dl.dataset.csDel); }
    });
}

function csRenderProfiles() {
    const used = (pid) => [
        ...csState.domains.filter(d => (csState.map.defaults || {})[d] === pid).map(d => d + ' default'),
        ...Object.entries(csState.map.tenants || {}).filter(([, v]) => v === pid).map(([t]) => t),
    ];
    document.getElementById('content-profiles').innerHTML = csState.profiles.map(p => {
        const u = used(p.profileId);
        const locked = p.profileId === 'all' || p.profileId === 'core';
        return `<div class="content-profile-row"><div class="pr-head">
            <strong>${escapeHtml(p.profileId)}</strong>
            <span class="content-hint" style="margin:0">${escapeHtml(p.description || '')}</span>
            <span class="pr-actions">
              <button class="btn btn-small btn-secondary" data-cs-edit="${escapeHtml(p.profileId)}">edit</button>
              ${locked ? '' : `<button class="btn btn-small btn-danger-muted" data-cs-del="${escapeHtml(p.profileId)}" data-action>delete</button>`}
            </span></div>
            <div style="margin-top:8px">${(p.sources || []).map(s => `<span class="content-chip">${escapeHtml(s.repo.split('/').pop())}</span>`).join('')}</div>
            ${u.length ? `<div class="content-hint" style="margin:6px 0 0">used by: ${escapeHtml(u.join(', '))}</div>` : ''}
        </div>`;
    }).join('') || '<p class="content-hint">No profiles yet.</p>';
}

function csRenderRepoPicker(selected) {
    document.getElementById('cs-pfrepos').innerHTML = csState.catalog.map(c =>
        `<label><input type="checkbox" data-repo="${escapeHtml(c.repo)}" data-cat="${escapeHtml(c.category)}" data-label="${escapeHtml(c.categoryLabel)}" data-branch="${escapeHtml(c.branch)}" ${selected.includes(c.repo) ? 'checked' : ''}> ${escapeHtml(c.repo.split('/').pop())} <span class="content-hint" style="margin:0">(${escapeHtml(c.category)})</span></label>`
    ).join('') || '<span class="content-hint">No repos in the catalog yet.</span>';
}

function csEditProfile(id) {
    const p = id ? csState.profiles.find(x => x.profileId === id) : null;
    document.getElementById('cs-pfid').value = p ? p.profileId : '';
    document.getElementById('cs-pfdesc').value = p ? (p.description || '') : '';
    csRenderRepoPicker(p ? (p.sources || []).map(s => s.repo) : []);
    document.getElementById('cs-fmsg').textContent = '';
}

function csRenderDelivery() {
    document.getElementById('cs-defaults').innerHTML = '<thead><tr><th>Domain</th><th>Default profile</th></tr></thead><tbody>' +
        csState.domains.map(d => `<tr><td>${escapeHtml(d)}</td><td><select id="cs-d-${escapeHtml(d)}">${csProfileOpts((csState.map.defaults || {})[d])}</select></td></tr>`).join('') + '</tbody>';
    const tb = document.querySelector('#cs-tenants tbody'); tb.innerHTML = '';
    Object.entries(csState.map.tenants || {}).forEach(([t, p]) => tb.appendChild(csTenantRow(t, p)));
    document.getElementById('cs-newtp').innerHTML = csProfileOpts(csState.profiles[0] && csState.profiles[0].profileId);
}

// Derive the deployment stage from a tenant id/URL: *.apps.dynatrace.com = production,
// *.sprint.apps.dynatracelabs.com = sprint, *.dev.apps.dynatracelabs.com = dev. A bare
// id with no domain hint defaults to production (the common case).
function stageOf(idOrUrl) {
    const s = String(idOrUrl || '');
    if (/\.sprint\./.test(s)) return 'sprint';
    if (/\.dev\./.test(s)) return 'dev';
    return 'production';
}
function stageBadge(stage) {
    // production = green, sprint = yellow, dev = teal.
    const bg = stage === 'production' ? '#1e3a1e' : stage === 'sprint' ? '#3a3a1e' : '#1e3a3a';
    const fg = stage === 'production' ? '#7dd67d' : stage === 'sprint' ? '#e0d77d' : '#7dd6e0';
    return `<span style="font-size:0.62rem;margin-left:6px;padding:0 5px;border-radius:3px;background:${bg};color:${fg}" title="Deployment stage">${escapeHtml(stage)}</span>`;
}

function csTenantRow(tid, pid) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><code>${escapeHtml(tid)}</code>${stageBadge(stageOf(tid))}</td><td><select>${csProfileOpts(pid)}</select></td><td><button class="btn btn-small btn-secondary" type="button">remove</button></td>`;
    tr.dataset.tid = tid;
    tr.querySelector('button').addEventListener('click', () => tr.remove());
    return tr;
}

function csAddTenant() {
    const t = document.getElementById('cs-newtid').value.trim(); if (!t) return;
    document.querySelector('#cs-tenants tbody').appendChild(csTenantRow(t, document.getElementById('cs-newtp').value));
    document.getElementById('cs-newtid').value = '';
}

async function csSaveDelivery() {
    const defaults = {}; csState.domains.forEach(d => defaults[d] = document.getElementById('cs-d-' + d).value);
    const tenants = {}; document.querySelectorAll('#cs-tenants tbody tr').forEach(tr => tenants[tr.dataset.tid] = tr.querySelector('select').value);
    const r = await fetch('/api/content/admin/tenant-map', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ defaults, tenants }) });
    const j = await r.json().catch(() => ({}));
    document.getElementById('cs-dmsg').textContent = r.ok ? `✓ saved (${j.tenants} override(s))` : ('✗ ' + (j.detail || 'error'));
    if (r.ok) loadContent();
}

async function csSaveProfile() {
    const id = document.getElementById('cs-pfid').value.trim(), desc = document.getElementById('cs-pfdesc').value.trim();
    const msg = document.getElementById('cs-fmsg');
    if (!id) { msg.textContent = 'profile id required'; return; }
    const sources = [...document.querySelectorAll('#cs-pfrepos input:checked')].map(c => ({ repo: c.dataset.repo, category: c.dataset.cat, categoryLabel: c.dataset.label, branch: c.dataset.branch }));
    if (!sources.length) { msg.textContent = 'pick at least one repo'; return; }
    const r = await fetch('/api/content/admin/profiles/' + encodeURIComponent(id), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ description: desc, sources }) });
    const j = await r.json().catch(() => ({}));
    msg.textContent = r.ok ? `✓ saved (${j.sources} repos)` : ('✗ ' + (j.detail || 'error'));
    if (r.ok) { csEditProfile(null); loadContent(); }
}

async function csDeleteProfile(id) {
    if (!confirm('Delete profile ' + id + '?')) return;
    const r = await fetch('/api/content/admin/profiles/' + encodeURIComponent(id), { method: 'DELETE', credentials: 'same-origin' });
    if (r.ok) loadContent(); else document.getElementById('cs-fmsg').textContent = '✗ delete failed';
}

async function csResolve() {
    const t = document.getElementById('cs-ptenant').value.trim(); if (!t) return;
    document.getElementById('cs-pmsg').textContent = 'resolving…'; document.getElementById('cs-presult').innerHTML = '';
    const r = await fetch('/api/content/manifest?tenant=' + encodeURIComponent(t));
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { document.getElementById('cs-pmsg').textContent = '✗ ' + (j.detail || 'error'); return; }
    document.getElementById('cs-pmsg').textContent = '';
    document.getElementById('cs-presult').innerHTML = `<div style="margin-top:10px">tenant <strong>${escapeHtml(j.tenant)}</strong> · domain <strong>${escapeHtml(j.domain)}</strong> · profile <strong>${escapeHtml(j.profileId)}</strong> · ${j.sources.length} repo(s)</div>
        <table style="margin-top:8px"><thead><tr><th>repo</th><th>category</th><th>sha</th></tr></thead><tbody>${j.sources.map(s => `<tr><td>${escapeHtml(s.repo)}</td><td>${escapeHtml(s.category || '')}</td><td><code>${escapeHtml((s.version || '?').slice(0, 8))}</code></td></tr>`).join('')}</tbody></table>`;
}

// ── Trainings tab: managed training-source catalog ──────────────────────────────
const CS_CAT_LABEL = { 'hands-on': 'Hands-On', 'learning-byte': 'Learning Bytes', 'onboarding': 'SE Onboarding', 'custom': 'Custom' };

async function csLoadSources() {
    const tb = document.querySelector('#cs-sources tbody');
    tb.innerHTML = '<tr><td colspan="4" class="loading">Loading…</td></tr>';
    try {
        const r = await fetch('/api/content/admin/sources', { credentials: 'same-origin' });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { tb.innerHTML = `<tr><td colspan="4" class="content-hint">${escapeHtml(j.detail || 'Sign in as an org member.')}</td></tr>`; return; }
        const rows = j.sources || [];
        tb.innerHTML = rows.length ? rows.map(s => `<tr>
            <td><code>${escapeHtml(s.repo)}</code>${s.private ? ' <span style="font-size:0.62rem;color:#e0d77d" title="Private repo">🔒</span>' : ''}</td>
            <td>${escapeHtml(CS_CAT_LABEL[s.category] || s.category || '')}</td>
            <td><span style="font-size:0.72rem;color:var(--text-2)">${escapeHtml(s.delivery || '')}</span></td>
            <td><button class="btn btn-small btn-secondary" type="button" data-cs-srcdel="${escapeHtml(s.repo)}">remove</button></td>
        </tr>`).join('') : '<tr><td colspan="4" class="content-hint">No managed training sources yet — add one above.</td></tr>';
    } catch (e) { tb.innerHTML = `<tr><td colspan="4" class="content-hint">Error: ${escapeHtml(String(e))}</td></tr>`; }
}

async function csSource(action) {
    const url = document.getElementById('cs-src-url').value.trim();
    const category = document.getElementById('cs-src-cat').value;
    const msg = document.getElementById('cs-src-msg');
    if (!url) { msg.textContent = 'Enter a GitHub repo URL.'; return; }
    msg.textContent = action === 'validate' ? 'Validating…' : 'Adding…';
    const ep = action === 'validate' ? '/api/content/admin/validate-repo' : '/api/content/admin/sources';
    try {
        const r = await fetch(ep, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin', body: JSON.stringify({ repo: url, category }) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { msg.textContent = '✗ ' + (j.detail || 'error'); return; }
        if (action === 'validate') {
            msg.textContent = j.valid ? `✓ valid (${j.delivery})` : `✗ ${j.reason}`;
        } else {
            msg.textContent = `✓ added ${j.source.repo} (${j.source.delivery})`;
            document.getElementById('cs-src-url').value = '';
            csLoadSources();
        }
    } catch (e) { msg.textContent = '✗ ' + String(e); }
}

async function csRemoveSource(repo) {
    if (!confirm(`Remove ${repo} from managed sources?`)) return;
    const r = await fetch('/api/content/admin/sources/' + repo, { method: 'DELETE', credentials: 'same-origin' });
    if (r.ok) csLoadSources();
    else { const j = await r.json().catch(() => ({})); document.getElementById('cs-src-msg').textContent = '✗ ' + (j.detail || 'remove failed'); }
}

async function loadContent() {
    wireContent();
    try {
        const r = await fetch('/api/content/admin/overview', { credentials: 'same-origin' });
        if (!r.ok) { document.getElementById('content-profiles').innerHTML = '<p class="content-hint">Sign in as an org member to manage content.</p>'; return; }
        Object.assign(csState, await r.json());
        csRenderProfiles();
        csRenderDelivery();
    } catch (e) {
        document.getElementById('content-profiles').innerHTML = '<p class="content-hint">Failed to load content overview.</p>';
    }
}

// ── Register Tenant tab (app deploy via platform token / COE auto) ────────────
let regWired = false;

function wireRegister() {
    if (regWired) return; regWired = true;
    document.getElementById('reg-deploy').addEventListener('click', () => goRegister('deploy'));
    document.getElementById('reg-undeploy').addEventListener('click', () => goRegister('undeploy'));
}

function setRegBusy(b) {
    document.getElementById('reg-spin').classList.toggle('busy', b);
    document.getElementById('reg-bar').hidden = !b;
    document.getElementById('reg-deploy').disabled = b;
    document.getElementById('reg-undeploy').disabled = b;
}

async function goRegister(action) {
    const t = document.getElementById('reg-tenant').value.trim();
    const k = document.getElementById('reg-token').value.trim();
    const m = document.getElementById('reg-msg');
    if (!t) { m.textContent = 'tenant required'; return; }
    m.textContent = ''; setRegBusy(true);
    try {
        const r = await fetch('/api/deploy/token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ action, tenant: t, token: k }) });
        const raw = await r.text();
        let j = {}; try { j = JSON.parse(raw); } catch (_) { /* non-JSON gateway page */ }
        if (r.ok) {
            document.getElementById('reg-token').value = '';
            if (action !== 'deploy') { m.textContent = '✓ undeployed ' + t; }
            else {
                const v = j.version || '?';
                const s = j.status === 'up-to-date' ? `already up-to-date (v${v})` : j.status === 'upgraded' ? `upgraded v${j.from} → v${v}` : `installed v${v}`;
                m.innerHTML = `✓ ${s} — <a href="${escapeHtml(j.url || '#')}" target="_blank">open app</a>` + (j.profile ? ` · content profile ${escapeHtml(j.profile)}` : '') + (j.allowlist ? `<br><span class="content-hint">outbound: ${escapeHtml(j.allowlist)}</span>` : '');
            }
            loadRegisterAudit();
        } else if (r.status === 401) {
            m.textContent = '✗ Sign in as a GitHub org member to deploy.';
        } else {
            m.textContent = '✗ ' + (j.detail || (`failed (HTTP ${r.status})` + (r.status >= 502 ? ' — the server may still be finishing; check the activity log' : '')));
            loadRegisterAudit();
        }
    } catch (e) {
        m.textContent = '✗ network error: ' + e;
    } finally {
        setRegBusy(false);
    }
}

async function loadRegisterAudit() {
    try {
        const r = await fetch('/api/deploy/audit?limit=30', { credentials: 'same-origin' });
        const data = r.ok ? await r.json() : { audit: [] };
        const audit = data.audit || [];
        const b = document.querySelector('#reg-audit tbody');
        b.innerHTML = audit.length
            ? audit.map(a => `<tr><td>${escapeHtml((a.ts || '').replace('T', ' ').slice(0, 19))}</td><td>${escapeHtml(a.user || '')}</td><td>${escapeHtml(a.tenant || '')}</td><td>${escapeHtml(a.action || '')}</td><td>${escapeHtml(a.result || '')}</td><td>${escapeHtml(a.to || a.version || '')}</td><td>${escapeHtml(a.via || '')}</td></tr>`).join('')
            : '<tr><td colspan="7" class="content-hint">none yet</td></tr>';
    } catch (e) { /* ignore */ }
}

function loadRegister() {
    wireRegister();
    loadRegisterAudit();
    loadMintClients();
}

async function loadMintClients() {
    const el = document.getElementById('reg-mint-clients');
    if (!el) return;
    try {
        const r = await fetch('/api/deploy/mint-clients', { credentials: 'same-origin' });
        if (!r.ok) { el.innerHTML = ''; return; }
        const j = await r.json();
        const rows = j.mintClients || [];
        el.innerHTML = rows.length
            ? '<h3 style="margin:24px 0 8px">Token-mint OAuth clients (gen3, read-only)</h3>'
              + '<p class="content-hint" style="margin:0 0 6px">Account OAuth clients used to mint platform tokens for trainings on gen3 tenants. Rotate in myaccount.dynatrace.com; the secret is never shown.</p>'
              + '<table><thead><tr><th>Domain</th><th>Client ID</th><th>Account</th></tr></thead><tbody>'
              + rows.map(c => `<tr><td>${escapeHtml(c.domain)}</td><td><code>${escapeHtml(c.clientId)}</code></td><td><code style="font-size:0.72rem">${escapeHtml(c.account)}</code></td></tr>`).join('')
              + '</tbody></table>'
            : '';
    } catch { el.innerHTML = ''; }
}
