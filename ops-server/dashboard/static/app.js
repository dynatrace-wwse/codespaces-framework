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
    if (view === 'workshops') loadWorkshops();
    if (view === 'register') loadRegister();
}

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => activateTab(tab.dataset.view));
});

// NOTE: tab restore from the URL hash happens in the init IIFE at the bottom of
// this file — NOT here at parse time. Running activateTab() this early reaches
// state declared later (e.g. `let regWired`) while it is still in the temporal
// dead zone, which throws and aborts the rest of top-level init (loadAuthState
// never runs → header stuck on "checking…", sign-in button never appears).

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
                    ${repo.training_test ? '<option value="training-test">Training test</option>' : ''}
                    <option value="deploy-ghpages">Deploy pages</option>
                    <option value="daemon">Training</option>
                </select>
                <select class="arch-select" id="arch-${safeRepo}" data-action>
                    <option value="both">both</option>
                    <option value="arm64">arm64</option>
                    <option value="amd64">amd64</option>
                </select>
                <button class="btn btn-small" data-action
                        onclick="triggerBuildFromRow('${escapeJsAttr(repo.repo)}', '${safeRepo}', this)">
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
            window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
                window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
                window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
    // deploy-ghpages is arch-less; training-test always runs its arena session
    // on amd64 (pinned by /api/arena/provision) — hide the selector for both.
    if (archSel) archSel.hidden = action === 'deploy-ghpages' || action === 'training-test';
}

async function triggerBuildFromRow(repo, safeRepo, btn) {
    if (!authState.signedIn) {
        window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
                window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
                window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
                return;
            }
            const data = await res.json().catch(() => ({}));
            const repoShort = repo.split('/').pop();
            if (res.status === 409 || data.status === 'already-queued') {
                // Dedupe: a training-test for this repo+ref is already queued
                // or running (it may be waiting behind the training semaphore
                // with no running row yet). No duplicate was enqueued.
                showToast(`⏳ Already queued/running: ${repoShort} @ ${branch} — not duplicated.`, 5000);
                btn.textContent = '⏳ Queued';
                setTimeout(() => { btn.textContent = 'Trigger'; }, 2500);
            } else if (!res.ok) {
                alert('Trigger failed: HTTP ' + res.status);
                btn.textContent = 'Trigger';
            } else {
                // The trigger response already says status:queued — surface it
                // so the click never looks dead (esp. for training-test, which
                // shows no running row until it clears the semaphore).
                showToast(`✓ Queued ${formatJobType(action)} · ${repoShort} @ ${branch}`, 4000);
                btn.textContent = '✓ Queued';
                setTimeout(() => { btn.textContent = 'Trigger'; }, 2500);
            }
        } finally {
            btn.disabled = false;
            if (btn.textContent === '…') btn.textContent = 'Trigger';  // e.g. network error
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

// Every dynamic value in this dashboard goes through here before it reaches
// innerHTML. There used to be TWO declarations of this function — this one,
// escaping `& < >`, and a second one further down escaping `& < > "`. The
// later declaration silently won at runtime, so the weaker one at the top of
// the file was dead code that read like the authoritative sanitizer. Whoever
// checked "do we escape quotes?" got a different answer depending on which
// one they found. There is now exactly one.
//
// It escapes quotes because most values no longer land in element TEXT: they
// land inside quoted attributes (`value="…"`, `title="…"`, `data-ws-open="…"`)
// and a workshop title or a trainer note is free text somebody else typed.
// A `"` closes the attribute early and the rest is parsed as markup — XSS with
// no `<` in sight. `'` is escaped for the same reason in `'`-quoted attributes.
function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// For values interpolated into a JS string literal inside an inline handler
// (`onclick="fn('${…}')"`). escapeHtml is NOT enough there: the HTML parser
// decodes `&#39;` back to `'` before the JS parser ever sees it, so the quote
// still breaks out of the string. Escape for JS first, then for HTML — and
// only for `&"<>`, so the JS backslashes survive decoding intact.
function escapeJsAttr(s) {
    return String(s ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
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
let livelogMissCount = 0;
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
    livelogMissCount = 0;
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
                    // Both 404. For a genuinely-starting job keep polling a while,
                    // but don't sit on "Initializing…" forever — after ~30 s of
                    // double-404 the job either never ran (deferred/cancelled) or
                    // its log expired. Say so instead of an eternal placeholder.
                    livelogMissCount = (livelogMissCount || 0) + 1;
                    if (livelogMissCount >= 15) {
                        if (livelogPoll) { clearInterval(livelogPoll); livelogPoll = null; }
                        pre.innerHTML = `<em style="color:var(--text-muted)">No log available for this job — it may never have started (deferred/cancelled), or its log has expired.</em>`;
                    }
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
    livelogMissCount = 0;
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
//
// Lanes. A worker serves exactly one queue topology and that decides who can
// ever land on it: `daily` takes self-service work, anything else is a workshop
// pool that self-service traffic cannot reach. Until this was rendered the two
// were indistinguishable in the UI, so an operator could not tell which
// machines were safe to cordon — and the planner had the same blind spot.

const DAILY_POOL = 'daily';

/** Lane of a worker hash. A missing pool means daily, matching every consumer. */
function laneOf(w) {
    return (w.pool || '').trim() || DAILY_POOL;
}

function laneClass(w) {
    if (w.role === 'master') return '';
    return laneOf(w) === DAILY_POOL ? 'lane-daily' : 'lane-workshop';
}

function laneBadge(w) {
    const lane = laneOf(w);
    if (!w.pool) {
        // Genuinely unknown rather than daily-by-default: a worker that has
        // registered but not yet heartbeat, or a hand-built box. Worth seeing.
        return '<span class="lane-badge unassigned" title="No pool published yet — '
             + 'registered but not yet heartbeat, or a hand-built worker. '
             + 'Treated as daily until it reports one.">unassigned</span>';
    }
    if (lane === DAILY_POOL) {
        return '<span class="lane-badge daily" title="Self-service lane — '
             + 'takes learner sessions off the shared queue">self-service</span>';
    }
    const label = lane === 'workshop' ? 'workshop' : `workshop · ${lane}`;
    return `<span class="lane-badge workshop" title="Workshop lane (${escapeHtml(lane)}) — `
         + `self-service work can never be scheduled here">${escapeHtml(label)}</span>`;
}

/**
 * Volume throughput and IOPS.
 *
 * Both matter and they bind in DIFFERENT phases of an install — bandwidth while
 * images are pulled and extracted, IOPS while the ActiveGate JVM starts — so
 * showing only one would hide half the failures. The thresholds are the
 * provisioned figures for a launched worker (500 MB/s, 6,000 IOPS); at 80% the
 * volume is close enough to its ceiling to be the reason installs are slow.
 */
function diskIoLine(w) {
    if (w.disk_iops == null || w.disk_iops === '') return '';   // no baseline yet
    const iops = parseInt(w.disk_iops, 10) || 0;
    const read = parseFloat(w.disk_read_mbps || '0') || 0;
    const write = parseFloat(w.disk_write_mbps || '0') || 0;
    const mbps = read + write;
    const hot = iops >= 4800 || mbps >= 400;
    const colour = hot ? 'var(--red)' : 'var(--text-muted)';
    return `<div style="font-size:0.72rem;color:${colour};margin-top:4px"
                 title="Read ${read} + write ${write} MB/s. Provisioned: 500 MB/s, 6000 IOPS.
Bandwidth binds during image pull; IOPS binds while the ActiveGate JVM boots.">
        Disk I/O: ${mbps.toFixed(1)} MB/s · ${iops.toLocaleString()} IOPS${hot ? ' ⚠' : ''}
    </div>`;
}

/** "lending 3/10 to self-service" for the standing workshop box. */
function lendingLine(w) {
    const cap = parseInt(w.borrow_capacity || '0', 10);
    if (!cap || !w.borrow_pool) return '';
    const inFlight = parseInt(w.borrow_in_flight || '0', 10);
    const free = parseInt(w.borrow_free || '0', 10);
    return `<div title="Half this box is lent to ${escapeHtml(w.borrow_pool)}; the rest `
         + `is reserved so a workshop can start with no notice">`
         + `Lending: <strong>${inFlight}/${cap}</strong> to ${escapeHtml(w.borrow_pool)}`
         + ` <span class="muted">(${free} free to lend)</span></div>`;
}

/**
 * The lanes strip: one row per lane, so "how many seats can self-service
 * actually get" and "how many are held for workshops" are answerable at a
 * glance instead of by adding up worker cards.
 */
function renderLanes(workers) {
    const el = document.getElementById('lane-strip');
    if (!el) return;
    const lanes = {};
    for (const w of workers) {
        if (w.role === 'master') continue;
        const lane = laneOf(w);
        const l = lanes[lane] || (lanes[lane] = {
            workers: 0, ready: 0, free: 0, active: 0, lent: 0, lendable: 0,
        });
        l.workers += 1;
        if (w.status === 'ready') l.ready += 1;
        l.free += parseInt(w.slots_free || '0', 10) || 0;
        l.active += parseInt(w.active_jobs || '0', 10) || 0;
        l.lent += parseInt(w.borrow_in_flight || '0', 10) || 0;
        l.lendable += parseInt(w.borrow_capacity || '0', 10) || 0;
    }
    const order = Object.keys(lanes).sort((a, b) =>
        (a === DAILY_POOL ? -1 : b === DAILY_POOL ? 1 : a.localeCompare(b)));
    if (!order.length) { el.innerHTML = ''; return; }

    el.innerHTML = order.map(lane => {
        const l = lanes[lane];
        const isDaily = lane === DAILY_POOL;
        const name = isDaily ? 'Self-service' : (lane === 'workshop' ? 'Workshops' : `Workshop · ${lane}`);
        const reserved = l.lendable ? ` · <b>${l.lendable}</b> lendable` : '';
        const lent = l.lent ? ` · <b>${l.lent}</b> lent out` : '';
        return `
            <div class="lane-card ${isDaily ? 'lane-daily' : 'lane-workshop'}">
                <span class="lane-name">${escapeHtml(name)}</span>
                <span class="lane-stats">
                    <b>${l.free}</b> free · <b>${l.active}</b> in use ·
                    <b>${l.ready}/${l.workers}</b> workers ready${reserved}${lent}
                </span>
            </div>`;
    }).join('');
}

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
                diskIoLine(w),
            ].filter(Boolean).join('');
            const lanePill = isMaster ? '' : laneBadge(w);
            const lendLine = lendingLine(w);
            return `
                <div class="worker-card ${isMaster ? 'is-master' : ''} ${stale ? 'offline' : ''} ${laneClass(w)}">
                    <h4>${escapeHtml(w.worker_id)} ${badge} ${lanePill} ${statusPill}</h4>
                    <div class="meta">
                        <div>Arch: <strong>${escapeHtml(w.arch || '')}</strong></div>
                        <div>Active: ${escapeHtml(String(w.active_jobs || '0'))} / ${escapeHtml(String(w.capacity || '?'))}</div>
                        ${lendLine}
                        <div>Last heartbeat: ${formatTime(w.last_heartbeat)}</div>
                        ${masterExtras}
                    </div>
                    ${metricsHtml}
                </div>
            `;
        }).join('');
    }

    renderLanes(workersData.workers);

    const queueGrid = document.getElementById('queue-status');
    queueGrid.innerHTML = Object.entries(buildsData.queues).map(([name, count]) => `
        <div class="queue-item">
            <div class="count">${count}</div>
            <div class="label">${name}</div>
        </div>
    `).join('');

    loadAutoscale();
}

// ── Fleet autoscaling (Workers tab) ─────────────────────────────────────────
// The panel is rendered only when the server says the signed-in user is a fleet
// owner. That is a UI convenience, NOT the security boundary — every mutating
// endpoint re-checks server-side, because hiding a button protects nobody.

let _fleetState = null;
let _fleetPlanned = null;

function fleetUi() {
    return {
        panel:   document.getElementById('autoscale-panel'),
        summary: document.getElementById('autoscale-summary'),
        seats:   document.getElementById('fleet-seats'),
        region:  document.getElementById('fleet-region'),
        inst:    document.getElementById('fleet-instance'),
        out:     document.getElementById('autoscale-plan'),
        creds:   document.getElementById('autoscale-creds'),
        apply:   document.getElementById('fleet-apply-btn'),
        freeze:  document.getElementById('fleet-freeze-btn'),
        pill:    document.getElementById('freeze-pill'),
    };
}

async function loadAutoscale() {
    const ui = fleetUi();
    if (!ui.panel) return;
    const instanceType = ui.inst && ui.inst.value ? ui.inst.value : '';
    const region = ui.region && ui.region.value ? ui.region.value : '';
    const qs = new URLSearchParams();
    if (instanceType) qs.set('instanceType', instanceType);
    if (region) qs.set('region', region);

    let data;
    try {
        const res = await fetch(`${API}/api/fleet/autoscale/status?${qs}`);
        if (!res.ok) { ui.panel.hidden = true; return; }
        data = await res.json();
    } catch { ui.panel.hidden = true; return; }

    _fleetState = data;
    // Non-owners never see the controls at all — the fleet is not their tool.
    ui.panel.hidden = !data.isOwner;
    if (!data.isOwner) return;

    if (ui.region && !ui.region.options.length) {
        // Grouped by continent so the choice reads as "near which audience",
        // which is the only thing that actually matters here.
        const areas = [];
        (data.regions || []).forEach(r => {
            if (!areas.includes(r.area)) areas.push(r.area);
        });
        ui.region.innerHTML = areas.map(area => `
            <optgroup label="${escapeHtml(area)}">
                ${(data.regions || []).filter(r => r.area === area).map(r =>
                    `<option value="${escapeHtml(r.id)}"${r.id === data.homeRegion ? ' selected' : ''}>${escapeHtml(r.label)} · ${escapeHtml(r.id)}</option>`
                ).join('')}
            </optgroup>`).join('');
    }
    if (ui.inst && !ui.inst.options.length) {
        const choices = data.instanceTypes || [];
        // Group by AWS family so the trade-off is visible in the list itself
        // rather than something you have to already know.
        const families = [];
        choices.forEach(t => { if (!families.includes(t.family)) families.push(t.family); });
        ui.inst.innerHTML = families.map(fam => `
            <optgroup label="${escapeHtml(fam)}">
                ${choices.filter(t => t.family === fam).map(t => {
                    const price = t.usd_per_session_hour != null
                        ? ` · $${t.usd_per_session_hour.toFixed(4)}/session-h` : '';
                    const star = t.recommended ? ' ★' : '';
                    return `<option value="${escapeHtml(t.type)}"${t.type === data.instanceType ? ' selected' : ''}>`
                         + `${escapeHtml(t.type)} · ${t.slots} slots${price}${star}</option>`;
                }).join('')}
            </optgroup>`).join('');
        window.__fleetChoices = choices;
        renderInstanceNote();
    }

    renderInstanceNote();

    const s = data.state || {};
    ui.summary.innerHTML = `
        <span class="autoscale-metrics">
            <span><b>${s.free_ready ?? 0}</b>free slots</span>
            <span><b>${s.slots_active ?? 0}</b>in use</span>
            <span><b>${s.workers_usable ?? 0}</b>workers</span>
            ${s.workers_warming ? `<span><b class="warn">${s.workers_warming}</b>warming</span>` : ''}
            ${s.workers_draining ? `<span><b class="warn">${s.workers_draining}</b>cordoned</span>` : ''}
            ${s.inflight_launches ? `<span><b class="warn">${s.inflight_launches}</b>booting</span>` : ''}
            ${s.workers_stale ? `<span><b class="bad">${s.workers_stale}</b>stale</span>` : ''}
        </span>`;

    ui.pill.hidden = !data.frozen;
    ui.freeze.textContent = data.frozen ? 'Unfreeze' : 'Freeze';
}

function renderFleetPlan(data) {
    const ui = fleetUi();
    ui.out.hidden = false;
    if (!data.ok) {
        ui.out.innerHTML = `<span class="bad">✗ ${escapeHtml(data.refused || 'refused')}</span>`;
        ui.apply.disabled = true;
        return;
    }
    const p = data.plan, cost = data.cost || {}, region = data.region || {};
    const parts = [];

    if (!region.ready) {
        // Named missing pieces, because "not ready" without the reason is the
        // kind of message that costs an hour on the day.
        parts.push(`<div class="bad">✗ ${escapeHtml(region.region)} is not launch-ready:
            <ul>${(region.missing || []).map(m => `<li>${escapeHtml(m)}</li>`).join('')}</ul></div>`);
    }

    if (p.action === 'none') {
        parts.push(`<div class="ok">✓ ${escapeHtml(p.reason)}</div>`);
    } else {
        parts.push(`<div><b>Plan:</b> ${escapeHtml(p.reason)}</div>`);
        if (p.undrain && p.undrain.length) {
            parts.push(`<div class="dim">Un-cordon first (instant, free): ${p.undrain.map(escapeHtml).join(', ')}</div>`);
        }
        if (p.launch > 0 && cost.known) {
            parts.push(`<div class="dim">New instances: <b>$${cost.hourly_usd}/h</b> · <b>$${cost.total_usd}</b> for the window · <b>$${cost.daily_usd}</b> if left a full day</div>`);
        } else if (p.launch > 0) {
            parts.push(`<div class="warn">No price on file for this type — cost unknown</div>`);
        }
        if (p.capped_by) {
            parts.push(`<div class="warn">⚠ capped by ${escapeHtml(p.capped_by)}</div>`);
        }
    }

    const down = data.scaleDown || {};
    if (down.action === 'cordon') {
        parts.push(`<div class="dim">Surplus: ${escapeHtml(down.reason)}</div>`);
    }
    ui.out.innerHTML = parts.join('');
    ui.apply.disabled = !(p.action === 'launch' || p.action === 'undrain') || !region.ready;
    _fleetPlanned = { seats: Number(ui.seats.value), region: ui.region.value, instanceType: ui.inst.value };
}

async function fleetPost(path, body) {
    const res = await fetch(`${API}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        const detail = data.detail || data;
        throw new Error(typeof detail === 'string' ? detail : (detail.reason || JSON.stringify(detail)));
    }
    return data;
}

function fleetArgs() {
    const ui = fleetUi();
    return {
        seats: Number(ui.seats.value) || 0,
        region: ui.region.value,
        instanceType: ui.inst.value,
    };
}

/* Explain the selected shape in words next to the picker. "30 slots" alone
   does not tell you WHY, and the why is what stops someone buying RAM the
   cores cannot feed. */
function renderInstanceNote() {
    const note = document.getElementById('fleet-instance-note');
    const sel = document.getElementById('fleet-instance');
    if (!note || !sel) return;
    const choice = (window.__fleetChoices || []).find(t => t.type === sel.value);
    if (!choice) { note.innerHTML = ''; return; }
    const limit = choice.limited_by === 'cpu'
        ? 'CPU runs out first' : 'memory runs out first';
    note.innerHTML = `
        <span class="inst-family">${escapeHtml(choice.family)}${choice.recommended ? ' · recommended' : ''}</span>
        <span class="inst-best">${escapeHtml(choice.best_for)}</span>
        <span class="inst-why">${escapeHtml(choice.why)}</span>
        <span class="inst-limit">${choice.slots} sessions per instance — ${limit}.</span>`;
}

function initAutoscaleControls() {
    const ui = fleetUi();
    if (!ui.panel) return;

    document.getElementById('fleet-instance')?.addEventListener('change', renderInstanceNote);

    document.getElementById('fleet-plan-btn')?.addEventListener('click', async () => {
        try {
            renderFleetPlan(await fleetPost('/api/fleet/autoscale/plan', fleetArgs()));
        } catch (e) { showToast(`Plan failed: ${e.message}`); }
    });

    document.getElementById('fleet-apply-btn')?.addEventListener('click', async () => {
        const a = fleetArgs();
        // Launching costs money and takes minutes to undo — make the user say so.
        if (!confirm(`Scale the fleet to secure ${a.seats} seats in ${a.region} using ${a.instanceType}?\n\nThis launches real EC2 instances.`)) return;
        try {
            const r = await fleetPost('/api/fleet/autoscale/apply', a);
            showToast(`Launched ${r.launched.length}, un-cordoned ${r.undrained.length}`);
            ui.apply.disabled = true;
            loadWorkers();
        } catch (e) { showToast(`Apply failed: ${e.message}`); }
    });

    document.getElementById('fleet-scaledown-btn')?.addEventListener('click', async () => {
        try {
            const r = await fleetPost('/api/fleet/autoscale/scale-down', fleetArgs());
            showToast(r.cordon.length
                ? `Cordoned ${r.cordon.length} worker(s) — they terminate once empty`
                : `No change: ${r.reason}`);
            loadWorkers();
        } catch (e) { showToast(`Cordon failed: ${e.message}`); }
    });

    document.getElementById('fleet-reap-btn')?.addEventListener('click', async () => {
        if (!confirm('Terminate every cordoned worker that has no sessions left?')) return;
        try {
            const r = await fleetPost('/api/fleet/autoscale/reap', {});
            showToast(`Terminated ${r.terminated.length}, skipped ${r.skipped.length}`);
            loadWorkers();
        } catch (e) { showToast(`Reap failed: ${e.message}`); }
    });

    document.getElementById('fleet-freeze-btn')?.addEventListener('click', async () => {
        const turningOn = !(_fleetState && _fleetState.frozen);
        try {
            await fleetPost('/api/fleet/freeze', { frozen: turningOn });
            showToast(turningOn ? 'Fleet FROZEN — scale-ups refused' : 'Freeze cleared');
            loadAutoscale();
        } catch (e) { showToast(`Freeze failed: ${e.message}`); }
    });

    document.getElementById('fleet-creds-btn')?.addEventListener('click', async () => {
        const box = document.getElementById('autoscale-creds');
        box.hidden = false;
        box.innerHTML = '<span class="dim">Checking…</span>';
        try {
            const q = new URLSearchParams({ region: ui.region.value });
            const res = await fetch(`${API}/api/fleet/credentials?${q}`);
            const c = await res.json();
            if (!res.ok) throw new Error(c.detail?.reason || 'check failed');
            if (!c.ok) {
                // The whole point of this button: say plainly that there are
                // none, so they can be pasted over SSH.
                box.innerHTML = `<div class="bad">✗ No usable AWS credentials</div>
                    <div class="dim">${escapeHtml(c.error || '')}</div>
                    <div class="dim">Paste fresh credentials into <code>~/.aws/credentials</code> on the master, then re-check. Nothing needs restarting.</div>`;
                return;
            }
            const quotaBits = Object.entries(c.quota || {}).map(([k, v]) =>
                v.vcpus != null
                    ? `${k}: <b>${v.vcpus}</b> vCPU`
                    : `${k}: <span class="warn">unreadable</span>`
            ).join(' · ');
            const rr = c.regionReady || {};
            box.innerHTML = `
                <div class="ok">✓ Credentials valid</div>
                <div class="dim">${escapeHtml(c.identity || '')} · account ${escapeHtml(c.account || '')}</div>
                <div class="dim">vCPU quota — ${quotaBits || 'not reported'}</div>
                <div class="${rr.ready ? 'dim' : 'warn'}">${escapeHtml(c.region)} ${rr.ready
                    ? 'is launch-ready'
                    : 'cannot launch yet: ' + (rr.missing || []).map(escapeHtml).join('; ')}</div>`;
        } catch (e) {
            box.innerHTML = `<span class="bad">✗ ${escapeHtml(e.message)}</span>`;
        }
    });

    // Changing the shape invalidates a plan computed for the old one.
    ['fleet-seats', 'fleet-region', 'fleet-instance'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', () => {
            ui.apply.disabled = true;
            document.getElementById('autoscale-plan').hidden = true;
            if (id !== 'fleet-seats') loadAutoscale();
        });
    });
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

    const integ = data.integration || { total: data.total, passed: data.passed, failed: data.failed };
    const train = data.training || { total: 0, passed: 0, failed: 0 };
    summary.innerHTML = `
        <div class="stat"><div class="value">${data.total}</div><div class="label">Total</div></div>
        <div class="stat"><div class="value" style="color:var(--green)">${data.passed}</div><div class="label">Passed</div></div>
        <div class="stat"><div class="value" style="color:var(--red)">${data.failed}</div><div class="label">Failed</div></div>
        <div class="stat"><div class="value">${integ.passed}/${integ.total}</div><div class="label">Integration</div></div>
        <div class="stat"><div class="value">${train.passed}/${train.total}</div><div class="label">Training</div></div>
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

    const nightlyRow = job => {
        const r = job.result || {};
        const arch = job.arch || r.arch || job.worker_arch || '?';
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
    };

    // Two sections: integration (devcontainer CI) vs training (full learner
    // flow via the arena/exec API). Server tags each row with `category`;
    // fall back on type for records that predate the split.
    const isTraining = j => (j.category || (j.type === 'integration-test' ? 'integration' : 'training')) === 'training';
    const integration = filtered.filter(j => !isTraining(j));
    const training = filtered.filter(isTraining);

    const tbody = document.getElementById('nightly-body');
    tbody.innerHTML = integration.length
        ? integration.map(nightlyRow).join('')
        : `<tr><td colspan="6" class="loading">No integration results match filter</td></tr>`;

    const trainingBody = document.getElementById('nightly-training-body');
    if (trainingBody) {
        trainingBody.innerHTML = training.length
            ? training.map(nightlyRow).join('')
            : `<tr><td colspan="6" class="loading">No training-test results match filter</td></tr>`;
    }
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

// Nightly sub-tab switching (Training tests first — reuses the sync-tab styling)
document.addEventListener('click', e => {
    const ntab = e.target.closest('[data-nightly-view]');
    if (!ntab) return;
    const view = ntab.dataset.nightlyView;
    document.querySelectorAll('[data-nightly-view]').forEach(t => t.classList.toggle('active', t === ntab));
    document.querySelectorAll('.nightly-subview').forEach(sv => sv.hidden = true);
    const target = document.getElementById(`nightly-view-${view}`);
    if (target) target.hidden = false;
});

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

// Automation grade per repo, keyed by short repo name. Fetched alongside the
// drift table because "which version is pinned" and "can this training actually
// run itself" are the two things you want to see about a repo at the same time.
async function fetchCoverage() {
    try {
        const res = await fetch(`${API}/api/fleet/coverage`);
        const data = await res.json();
        return data.trainings || {};
    } catch (e) {
        return {};
    }
}

function automationBadge(cov) {
    if (!cov) return '<span class="auto-badge auto-unknown" title="Never scanned">—</span>';
    const ratio = cov.owed ? `${cov.covered}/${cov.owed} sections` : 'no hands-on sections';
    const gaps = (cov.gaps && cov.gaps.length) ? ` · missing: ${cov.gaps.join(', ')}` : '';
    const when = cov.verifiedAt || cov.scannedAt || '';
    const tip = escapeHtml(`${ratio}${cov.exempt ? ` · ${cov.exempt} exempt` : ''}${gaps}${when ? ` · ${when}` : ''}`);
    const map = {
        verified: ['auto-verified', 'E2E verified'],
        complete: ['auto-complete', 'complete'],
        partial:  ['auto-partial',  'partial'],
        none:     ['auto-none',     'no automation'],
    };
    const [cls, label] = map[cov.grade] || ['auto-unknown', 'unknown'];
    return `<span class="auto-badge ${cls}" title="${tip}">${label}</span>`;
}

async function loadSyncStatus(force = false) {
    const tbody = document.getElementById('sync-status-body');
    tbody.innerHTML = '<tr><td colspan="6" class="loading">Running sync status…</td></tr>';
    try {
        const url = force ? `${API}/api/sync/status-summary?bust=${Date.now()}` : `${API}/api/sync/status-summary`;
        const [res, coverage] = await Promise.all([fetch(url), fetchCoverage()]);
        const data = await res.json();
        if (data.error) {
            tbody.innerHTML = `<tr><td colspan="6" class="loading" style="color:var(--red)">Error: ${escapeHtml(data.error)}</td></tr>`;
            return;
        }
        const rows = Array.isArray(data.rows) ? data.rows : [];
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="loading">No status data returned.</td></tr>';
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
                <td>${automationBadge(coverage[(r.repo || r.name || '').split('/').pop()])}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="loading">Error: ${escapeHtml(String(e))}</td></tr>`;
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
            window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
            window.location.href = '/oauth2/start?rd=' + encodeURIComponent(window.location.pathname);
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
                onclick="triggerAgentFixCI('${escapeJsAttr(r.job_id || '')}','${escapeJsAttr(r.repo)}','${escapeJsAttr(r.branch || '')}','${escapeJsAttr(r.arch || '')}','${escapeJsAttr(failedStep)}',this)"
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
    initAutoscaleControls();  // bind before the first loadWorkers() render
    loadWorkers();
    loadNightly();
    loadNightlyRuns();
})();

// Restore the active tab from the URL hash. Deferred with setTimeout(0) so it runs
// AFTER this entire script has finished executing — tab handlers read module state
// declared lower in the file (e.g. `let regWired` in the Register section, `const
// csState` in Content). Calling activateTab() during top-level/init execution hits
// those bindings in their temporal dead zone, throwing ReferenceError and (before
// this fix) aborting init so loadAuthState() never ran (header stuck on "checking…",
// no sign-in button). The macrotask guarantees every declaration is initialized.
setTimeout(() => {
    try {
        const hash = location.hash.replace('#', '');
        if (hash && document.querySelector(`.tab[data-view="${hash}"]`)) activateTab(hash);
    } catch (e) { console.error('tab restore failed', e); }
}, 0);

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
    // Scoped to #view-content on purpose. The Workshops & Delivery tab reuses
    // the same .content-tab/.content-subview classes for their styling, so an
    // unscoped querySelectorAll here would toggle that tab's sub-views too and
    // hide every panel on the page.
    const root = document.getElementById('view-content');
    root.querySelectorAll('.content-tab').forEach(t => t.addEventListener('click', () => {
        root.querySelectorAll('.content-tab').forEach(x => x.classList.toggle('active', x === t));
        root.querySelectorAll('.content-subview').forEach(v => { v.hidden = (v.id !== 'content-view-' + t.dataset.contentView); });
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

// Known fleet tenants keyed by bare id → stage. A bare id (e.g. "ydi9582h") carries no
// domain hint, so the suffix regex below can't classify it; this map keeps those correct.
// (ydi9582h is SPRINT, not production.)
const KNOWN_TENANT_STAGE = {
    geu80787: 'production',   // COE
    sro97894: 'production',   // SRO
    ydi9582h: 'sprint',       // sprint env
};
// Derive the deployment stage from a tenant id/URL: *.apps.dynatrace.com = production,
// *.sprint.apps.dynatracelabs.com = sprint, *.dev.apps.dynatracelabs.com = dev. A full URL
// is classified by suffix; a bare id is looked up in KNOWN_TENANT_STAGE, else defaults to
// production (the common case for prod tenants, which use bare ids).
function stageOf(idOrUrl) {
    const s = String(idOrUrl || '');
    if (/\.sprint\./.test(s)) return 'sprint';
    if (/\.dev\./.test(s)) return 'dev';
    const id = s.replace(/^https?:\/\//, '').split('.')[0].split('/')[0];
    if (KNOWN_TENANT_STAGE[id]) return KNOWN_TENANT_STAGE[id];
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
    if (r.ok) { loadContent(); return; }
    const j = await r.json().catch(() => ({}));
    document.getElementById('cs-fmsg').textContent = '✗ ' + (j.detail || 'delete failed');
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
    tb.innerHTML = '<tr><td colspan="5" class="loading">Loading…</td></tr>';
    try {
        const r = await fetch('/api/content/admin/sources', { credentials: 'same-origin' });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { tb.innerHTML = `<tr><td colspan="5" class="content-hint">${escapeHtml(j.detail || 'Sign in as an org member.')}</td></tr>`; return; }
        const rows = j.sources || [];
        tb.innerHTML = rows.length ? rows.map(s => `<tr>
            <td><code>${escapeHtml(s.repo)}</code>${s.private ? ' <span style="font-size:0.62rem;color:#e0d77d" title="Private repo">🔒</span>' : ''}</td>
            <td>${(s.branch && s.branch !== 'main') ? `<code style="color:#e0d77d" title="Delivered from a non-main branch">${escapeHtml(s.branch)}</code>` : `<span style="font-size:0.72rem;color:var(--text-2)">${escapeHtml(s.branch || 'main')}</span>`}</td>
            <td>${escapeHtml(CS_CAT_LABEL[s.category] || s.category || '')}</td>
            <td><span style="font-size:0.72rem;color:var(--text-2)">${escapeHtml(s.delivery || '')}</span></td>
            <td><button class="btn btn-small btn-secondary" type="button" data-cs-srcdel="${escapeHtml(s.repo)}">remove</button></td>
        </tr>`).join('') : '<tr><td colspan="5" class="content-hint">No managed training sources yet — add one above.</td></tr>';
    } catch (e) { tb.innerHTML = `<tr><td colspan="5" class="content-hint">Error: ${escapeHtml(String(e))}</td></tr>`; }
}

async function csSource(action) {
    const url = document.getElementById('cs-src-url').value.trim();
    const branch = (document.getElementById('cs-src-branch')?.value || '').trim();
    const category = document.getElementById('cs-src-cat').value;
    const msg = document.getElementById('cs-src-msg');
    if (!url) { msg.textContent = 'Enter a GitHub repo URL.'; return; }
    msg.textContent = action === 'validate' ? 'Validating…' : 'Adding…';
    const ep = action === 'validate' ? '/api/content/admin/validate-repo' : '/api/content/admin/sources';
    try {
        const r = await fetch(ep, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin', body: JSON.stringify({ repo: url, category, branch }) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { msg.textContent = '✗ ' + (j.detail || 'error'); return; }
        if (action === 'validate') {
            msg.textContent = j.valid ? `✓ valid (${j.delivery}${j.branch && j.branch !== 'main' ? ` @ ${j.branch}` : ''})` : `✗ ${j.reason}`;
        } else {
            msg.textContent = j.branchSwitched
                ? `✓ ${j.source.repo} switched to branch ${j.source.branch}`
                : `✓ added ${j.source.repo} (${j.source.delivery}${j.source.branch && j.source.branch !== 'main' ? ` @ ${j.source.branch}` : ''})`;
            document.getElementById('cs-src-url').value = '';
            const bEl = document.getElementById('cs-src-branch'); if (bEl) bEl.value = '';
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

// ── Workshops & Delivery tab ─────────────────────────────────────────────────
// Orbital is the system of record for workshops (live:session:* in Redis); the
// enablement app is a proxy over the same /api/live/* routes. This tab is a
// cross-tenant admin surface over that existing store — the only NEW state is
// the trainer registry.
//
// Writer-gated twice: nginx auth_request on /api/workshops/admin/, and
// _require_writer in FastAPI. Hiding the tab is a convenience, not the boundary.

// Chips shown per calendar day before the rest fold behind a "+N more" toggle.
const WS_CAL_MAX_CHIPS = 3;
// `expandedDays` lives in state, not in the DOM, because wsRenderCalendar
// replaces the whole grid — a toggle held only in markup would collapse again
// on the next reload, filter change or month switch.
const wsState = { workshops: [], trainers: [], month: null, editing: null,
                  expandedDays: new Set() };
let wsWired = false;

function wireWorkshops() {
    if (wsWired) return; wsWired = true;
    // Scoped to #view-workshops — see the note in wireContent().
    const root = document.getElementById('view-workshops');
    root.querySelectorAll('.content-tab').forEach(t => t.addEventListener('click', () => {
        root.querySelectorAll('.content-tab').forEach(x => x.classList.toggle('active', x === t));
        root.querySelectorAll('.content-subview').forEach(v => { v.hidden = (v.id !== 'ws-view-' + t.dataset.wsView); });
        if (t.dataset.wsView === 'trainers') wsLoadTrainers();
    }));
    document.getElementById('ws-tr-add').addEventListener('click', wsAddTrainer);
    document.getElementById('ws-tr-email').addEventListener('keydown', (e) => { if (e.key === 'Enter') wsAddTrainer(); });
    document.querySelector('#ws-trainers tbody').addEventListener('click', (e) => {
        const rm = e.target.closest('[data-ws-trdel]');
        if (rm) wsRemoveTrainer(rm.dataset.wsTrdel);
    });
    document.getElementById('ws-reload').addEventListener('click', wsLoadSchedule);
    document.getElementById('ws-filter-state').addEventListener('change', wsLoadSchedule);
    // Text filters are debounced: every keystroke is a round trip that walks the
    // whole index doing per-row HGETALL/SCARD, so firing on each one would put
    // the operator's typing speed in front of Redis.
    WS_FILTER_IDS.forEach(id => {
        const el = document.getElementById(id);
        el.addEventListener('input', wsFilterChanged);
        el.addEventListener('keydown', (e) => { if (e.key === 'Enter') wsLoadSchedule(); });
    });
    document.getElementById('ws-filter-clear').addEventListener('click', () => {
        WS_FILTER_IDS.forEach(id => { document.getElementById(id).value = ''; });
        document.getElementById('ws-filter-state').value = '';
        wsLoadSchedule();
    });
    document.getElementById('ws-cal-prev').addEventListener('click', () => wsShiftMonth(-1));
    document.getElementById('ws-cal-next').addEventListener('click', () => wsShiftMonth(1));
    document.getElementById('ws-cal-today').addEventListener('click', () => { wsState.month = null; wsRenderCalendar(); });
    document.querySelector('#ws-table tbody').addEventListener('click', (e) => {
        const row = e.target.closest('[data-ws-open]');
        if (row) wsOpenEditor(row.dataset.wsOpen);
    });
    document.getElementById('ws-calendar').addEventListener('click', (e) => {
        // The fold toggle is checked FIRST. It sits inside the same day cell as
        // the chips, so testing for a workshop first would open one whenever the
        // two ever end up nested.
        const toggle = e.target.closest('[data-ws-day]');
        if (toggle) {
            const day = toggle.dataset.wsDay;
            if (wsState.expandedDays.has(day)) wsState.expandedDays.delete(day);
            else wsState.expandedDays.add(day);
            wsRenderCalendar();
            return;
        }
        const chip = e.target.closest('[data-ws-open]');
        if (chip) wsOpenEditor(chip.dataset.wsOpen);
    });
}

async function loadWorkshops() {
    wireWorkshops();
    await Promise.all([wsLoadSchedule(), wsLoadTrainers()]);
}

// ── Trainers ─────────────────────────────────────────────────────────────────

function wsTrainerMsg(text, ok) {
    const el = document.getElementById('ws-tr-msg');
    el.textContent = text || '';
    el.style.color = ok ? 'var(--ok, #4ade80)' : 'var(--danger, #f87171)';
}

async function wsLoadTrainers() {
    const tbody = document.querySelector('#ws-trainers tbody');
    try {
        const r = await fetch('/api/workshops/admin/trainers', { credentials: 'same-origin' });
        if (!r.ok) {
            tbody.innerHTML = '<tr><td colspan="6" class="content-hint">Sign in as an org member to manage trainers.</td></tr>';
            return;
        }
        const j = await r.json();
        wsState.trainers = j.trainers || [];
        document.getElementById('ws-tr-count').textContent = `(${wsState.trainers.length})`;
        tbody.innerHTML = wsState.trainers.length
            ? wsState.trainers.map(t => `<tr>
                <td>${escapeHtml(t.email || '')}</td>
                <td>${escapeHtml(t.name || '')}</td>
                <td>${escapeHtml(t.addedBy || '')}</td>
                <td>${escapeHtml((t.addedAt || '').slice(0, 10))}</td>
                <td>${escapeHtml(t.note || '')}</td>
                <td><button class="btn btn-small btn-danger" data-action data-ws-trdel="${escapeHtml(t.email || '')}">Remove</button></td>
              </tr>`).join('')
            : '<tr><td colspan="6" class="content-hint">No trainers yet. Nobody can schedule a workshop until one is added.</td></tr>';
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="6" class="content-hint">Failed to load trainers.</td></tr>';
    }
}

async function wsAddTrainer() {
    const email = document.getElementById('ws-tr-email').value.trim();
    if (!email) { wsTrainerMsg('An email is required.', false); return; }
    wsTrainerMsg('', true);
    const r = await fetch('/api/workshops/admin/trainers', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            email,
            name: document.getElementById('ws-tr-name').value.trim(),
            note: document.getElementById('ws-tr-note').value.trim(),
        }),
    });
    if (r.ok) {
        ['ws-tr-email', 'ws-tr-name', 'ws-tr-note'].forEach(id => { document.getElementById(id).value = ''; });
        wsTrainerMsg(`✓ ${email} can now schedule workshops.`, true);
        wsLoadTrainers();
    } else {
        const j = await r.json().catch(() => ({}));
        wsTrainerMsg('✗ ' + (j.detail || 'add failed'), false);
    }
}

async function wsRemoveTrainer(email) {
    if (!confirm(`Remove ${email}? They will no longer be able to schedule workshops. Existing workshops they run are unaffected.`)) return;
    const r = await fetch('/api/workshops/admin/trainers/' + encodeURIComponent(email), {
        method: 'DELETE', credentials: 'same-origin',
    });
    if (r.ok) { wsTrainerMsg(`✓ ${email} removed.`, true); wsLoadTrainers(); }
    else { const j = await r.json().catch(() => ({})); wsTrainerMsg('✗ ' + (j.detail || 'remove failed'), false); }
}

// ── Workshop schedule: calendar + table + editor ─────────────────────────────

function wsWhen(w) {
    if (!w.scheduledAt) return { day: (w.createdAt || '').slice(0, 10), label: '— not scheduled —' };
    const day = w.scheduledAt.slice(0, 10);
    const time = w.scheduledAt.slice(11, 16);
    const tz = w.timezone ? ` ${w.timezone}` : '';
    const dur = w.durationMinutes ? ` · ${w.durationMinutes}m` : '';
    return { day, label: `${day} ${time}${tz}${dur}` };
}

function wsSeats(w) {
    if (!w.maxSeats) return `${w.seatsTaken} / ∞`;
    return `${w.seatsTaken} / ${w.maxSeats} (${w.seatsOpen} open)`;
}

function wsTenantLabel(url) {
    if (!url) return '—';
    try { return new URL(url).hostname.split('.')[0]; } catch (e) { return url; }
}

// Filter inputs, in query-param order. One list so wiring, clearing and reading
// cannot drift apart — adding a filter here is the only edit needed.
const WS_FILTER_IDS = ['ws-filter-tenant', 'ws-filter-trainer', 'ws-filter-id',
                       'ws-filter-seats-min', 'ws-filter-seats-max'];
const WS_FILTER_PARAMS = { 'ws-filter-tenant': 'tenant', 'ws-filter-trainer': 'trainer',
                           'ws-filter-id': 'workshopId', 'ws-filter-seats-min': 'seatsMin',
                           'ws-filter-seats-max': 'seatsMax' };
let wsFilterTimer = null;

function wsFilterChanged() {
    clearTimeout(wsFilterTimer);
    wsFilterTimer = setTimeout(wsLoadSchedule, 300);
}

function wsFilterQuery() {
    const q = new URLSearchParams();
    const state = document.getElementById('ws-filter-state').value;
    if (state) q.set('state', state);
    WS_FILTER_IDS.forEach(id => {
        const v = (document.getElementById(id).value || '').trim();
        if (v) q.set(WS_FILTER_PARAMS[id], v);
    });
    return q;
}

async function wsLoadSchedule() {
    const cal = document.getElementById('ws-calendar');
    const tbody = document.querySelector('#ws-table tbody');
    const q = wsFilterQuery();
    const qs = q.toString();
    try {
        const r = await fetch('/api/workshops/admin/schedule' + (qs ? `?${qs}` : ''),
                              { credentials: 'same-origin' });
        if (!r.ok) {
            cal.innerHTML = '<p class="content-hint">Sign in as an org member to see workshops.</p>';
            tbody.innerHTML = '';
            return;
        }
        const j = await r.json();
        wsState.workshops = j.workshops || [];
        document.getElementById('ws-count').textContent =
            `(${j.count}${j.total > j.count ? ` of ${j.total}` : ''})`;
        // Say plainly that a filter is on. A page showing 1 of 46 with the
        // reason scrolled out of view reads as "everything is gone".
        const note = document.getElementById('ws-filter-note');
        note.textContent = qs
            ? `filtered — ${j.count} of ${j.total} workshop${j.total === 1 ? '' : 's'}`
            : '';
        wsRenderCalendar();
        wsRenderTable();
    } catch (e) {
        cal.innerHTML = '<p class="content-hint">Failed to load workshops.</p>';
    }
}

function wsRenderTable() {
    const tbody = document.querySelector('#ws-table tbody');
    if (!wsState.workshops.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="content-hint">No workshops match this filter.</td></tr>';
        return;
    }
    tbody.innerHTML = wsState.workshops.map(w => {
        const when = wsWhen(w);
        const co = w.coTrainers.length ? ` +${w.coTrainers.length}` : '';
        return `<tr data-ws-open="${escapeHtml(w.sessionId)}" style="cursor:pointer">
            <td>${escapeHtml(when.label)}</td>
            <td>${escapeHtml(w.title)}<br><span style="opacity:.55;font-size:.75rem">${escapeHtml(w.trainingId)}</span></td>
            <td>${escapeHtml(w.owner)}${co}</td>
            <td>${escapeHtml(wsTenantLabel(w.ownerTenant))}</td>
            <td>${escapeHtml(w.state)}</td>
            <td>${escapeHtml(wsSeats(w))}</td>
            <td>${w.registrants.length} reg · ${w.joinedCount} present</td>
            <td><button class="btn btn-small btn-secondary" data-action data-ws-open="${escapeHtml(w.sessionId)}">Manage</button></td>
        </tr>`;
    }).join('');
}

function wsShiftMonth(delta) {
    const base = wsState.month ? new Date(wsState.month + '-01T00:00:00Z') : new Date();
    base.setUTCMonth(base.getUTCMonth() + delta);
    wsState.month = `${base.getUTCFullYear()}-${String(base.getUTCMonth() + 1).padStart(2, '0')}`;
    wsRenderCalendar();
}

function wsRenderCalendar() {
    const cal = document.getElementById('ws-calendar');
    const now = new Date();
    const month = wsState.month
        || `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`;
    wsState.month = month;
    const [y, m] = month.split('-').map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1));
    const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
    // Monday-first grid: JS getUTCDay() is Sunday-first.
    const lead = (first.getUTCDay() + 6) % 7;
    document.getElementById('ws-cal-label').textContent =
        first.toLocaleString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' });

    const byDay = {};
    wsState.workshops.forEach(w => {
        const day = wsWhen(w).day;
        if (day) (byDay[day] = byDay[day] || []).push(w);
    });

    let cells = '';
    for (let i = 0; i < lead; i++) cells += '<td style="opacity:.25"></td>';
    for (let d = 1; d <= days; d++) {
        const iso = `${month}-${String(d).padStart(2, '0')}`;
        const all = byDay[iso] || [];
        // Fold past WS_CAL_MAX_CHIPS. Eight workshops on one day used to render
        // eight chips into a 64px cell and pull the whole month grid out of
        // shape, so the busiest days were the least readable ones.
        const open = wsState.expandedDays.has(iso);
        const shown = open ? all : all.slice(0, WS_CAL_MAX_CHIPS);
        const hidden = all.length - shown.length;
        const chips = shown.map(w =>
            `<div class="ws-cal-chip" data-ws-open="${escapeHtml(w.sessionId)}" title="${escapeHtml(w.title)} · ${escapeHtml(w.owner)} · ${escapeHtml(wsTenantLabel(w.ownerTenant))}">
                ${escapeHtml((w.scheduledAt || '').slice(11, 16) || '··')} ${escapeHtml(w.title)}
             </div>`).join('');
        let more = '';
        if (hidden > 0) {
            more = `<button class="ws-cal-more" data-ws-day="${iso}"
                        title="Show the other ${hidden} workshop${hidden === 1 ? '' : 's'} on this day">+${hidden} more</button>`;
        } else if (open && all.length > WS_CAL_MAX_CHIPS) {
            more = `<button class="ws-cal-more" data-ws-day="${iso}" title="Collapse this day">− fewer</button>`;
        }
        cells += `<td class="ws-cal-day"><div class="ws-cal-daynum">${d}</div>${chips}${more}</td>`;
        if ((lead + d) % 7 === 0) cells += '</tr><tr>';
    }
    cal.innerHTML = `<table style="width:100%;table-layout:fixed">
        <thead><tr>${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            .map(d => `<th style="font-size:.7rem;opacity:.6">${d}</th>`).join('')}</tr></thead>
        <tbody><tr>${cells}</tr></tbody></table>`;
}

// ── Editor ───────────────────────────────────────────────────────────────────
// Mutations reuse the EXISTING /api/live/* routes rather than adding admin
// twins. Those routes gate on `trainerEmail` matching the stored team, so the
// dashboard sends the workshop's own owner — honest about the fact that this
// seam still trusts a caller-supplied email (see workshop-provisioning-open-items).

function wsCloseEditor() {
    const el = document.getElementById('ws-editor');
    if (el) el.remove();
    wsState.editing = null;
}

async function wsLiveAction(path, body, method) {
    const r = await fetch('/api/live/sessions' + path, {
        method: method || 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || `${r.status}`);
    }
    return r.json().catch(() => ({}));
}

/** Local time for an ISO instant, or an em dash. Fleet times are UTC on the wire. */
function wsFleetTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
}

/**
 * Provisioning status for one workshop, from the public read-only fleet route.
 *
 * Rendered inline rather than only linked, because "are there machines for this
 * class" is the question the editor is opened to answer, and a JSON tab is not
 * an answer at a glance. The raw link stays for the cases the summary flattens.
 *
 * Branches on `standing`, never on `workers === 0` — a small workshop launches
 * nothing ON PURPOSE and reads identically to a failed launch otherwise. That
 * exact conflation shipped in the app for a version.
 */
async function wsLoadFleet(sessionId) {
    const box = document.getElementById('ws-ed-fleet');
    if (!box) return;
    try {
        const r = await fetch(`/api/workshops/${encodeURIComponent(sessionId)}/fleet`,
                              { credentials: 'same-origin' });
        if (!r.ok) { box.innerHTML = `<span style="opacity:.7">Fleet unavailable (HTTP ${r.status}).</span>`; return; }
        const f = await r.json();
        const rows = [];
        rows.push(`<div>Planned for <strong>${f.planned_seats ?? '—'}</strong> seats`
                + ` — booked capacity plus the trainer team, not registrations.</div>`);
        rows.push(`<div>Machines appear <strong>${wsFleetTime(f.prewarm_at)}</strong>`
                + ` (${f.lead_minutes ?? '—'} min before the start), held until`
                + ` <strong>${wsFleetTime(f.teardown_at)}</strong>.</div>`);
        if (!f.provisioned) {
            rows.push('<div style="opacity:.75">Not provisioned yet — nothing has been bought'
                    + ' for this workshop. It launches at the time above whether or not anyone'
                    + ' has registered.</div>');
        } else if (f.standing) {
            rows.push(`<div>On the <strong>standing</strong> lane — no dedicated machines, by design`
                    + ` (${f.standing_max_seats ?? '?'} seats or fewer). Raise the seat cap to get`
                    + ` its own.</div>`);
        } else {
            const ready = f.ready_workers ?? 0;
            rows.push(`<div><strong>${f.workers ?? 0}</strong> machine(s) ×`
                    + ` ${f.seats_per_worker ?? 0} seats · <strong>${ready}</strong> ready ·`
                    + ` state <strong>${escapeHtml(f.state || 'unknown')}</strong>`
                    + `${f.degraded ? ' <span style="color:var(--warn,#fbbf24)">(degraded — some slots came up short)</span>' : ''}</div>`);
            if ((f.instances || []).length) {
                rows.push(`<div style="opacity:.7">${f.instances.map(escapeHtml).join(', ')}</div>`);
            }
        }
        const strayed = (f.failedOpen ?? 0) + (f.unbound ?? 0);
        if (strayed > 0) {
            rows.push(`<div style="color:var(--warn,#fbbf24)">⚠ ${strayed} learner(s) were served`
                    + ` from the shared daily pool instead of this workshop's own machines.</div>`);
        }
        box.innerHTML = rows.join('');
    } catch (e) {
        box.innerHTML = '<span style="opacity:.7">Failed to load fleet.</span>';
    }
}

function wsOpenEditor(sessionId) {
    const w = wsState.workshops.find(x => x.sessionId === sessionId);
    if (!w) return;
    wsCloseEditor();
    wsState.editing = sessionId;
    const ro = !w.editable;
    const el = document.createElement('div');
    el.id = 'ws-editor';
    el.style = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:900;display:flex;align-items:center;justify-content:center;padding:20px';
    el.innerHTML = `<div class="content-card" style="max-width:720px;width:100%;max-height:88vh;overflow:auto;margin:0">
        <div style="display:flex;align-items:baseline;gap:10px">
            <h3 style="margin:0;flex:1">${escapeHtml(w.title)}</h3>
            <span style="opacity:.6;font-size:.75rem">${escapeHtml(w.sessionId)}</span>
            <button class="btn btn-small btn-secondary" id="ws-ed-close">Close</button>
        </div>
        <p class="content-hint" style="margin:8px 0 12px">
            ${escapeHtml(w.trainingId)} · ${escapeHtml(w.state)} · ${escapeHtml(wsTenantLabel(w.ownerTenant))}
            · owner ${escapeHtml(w.owner)}${w.coTrainers.length ? ` · co-trainers ${escapeHtml(w.coTrainers.join(', '))}` : ''}
            ${w.joinCode ? ` · code <code>${escapeHtml(w.joinCode)}</code>` : ''}
        </p>
        ${ro ? `<p class="content-hint" style="color:var(--warn,#fbbf24)">A ${escapeHtml(w.state)} workshop cannot be edited — the definition is frozen once it starts. Administration actions below still apply.</p>` : ''}
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:10px">
            <label style="font-size:.75rem;opacity:.75">Title
                <input type="text" id="ws-ed-title" value="${escapeHtml(w.title)}" ${ro ? 'disabled' : ''} style="width:100%"></label>
            <label style="font-size:.75rem;opacity:.75">Scheduled at (ISO8601)
                <input type="text" id="ws-ed-when" value="${escapeHtml(w.scheduledAt || '')}" placeholder="2026-09-01T09:00:00Z" ${ro ? 'disabled' : ''} style="width:100%"></label>
            <label style="font-size:.75rem;opacity:.75">Timezone (IANA)
                <input type="text" id="ws-ed-tz" value="${escapeHtml(w.timezone || '')}" placeholder="Europe/Berlin" ${ro ? 'disabled' : ''} style="width:100%"></label>
            <label style="font-size:.75rem;opacity:.75">Duration (minutes)
                <input type="number" id="ws-ed-dur" value="${escapeHtml(String(w.durationMinutes || ''))}" ${ro ? 'disabled' : ''} style="width:100%"></label>
            <label style="font-size:.75rem;opacity:.75">Max seats
                <input type="number" id="ws-ed-seats" value="${escapeHtml(String(w.maxSeats || ''))}" ${ro ? 'disabled' : ''} style="width:100%"></label>
            <label style="font-size:.75rem;opacity:.75">Trainers (comma separated, first is owner)
                <input type="text" id="ws-ed-trainers" value="${escapeHtml(w.trainers.join(', '))}" ${ro ? 'disabled' : ''} style="width:100%"></label>
        </div>
        <label style="font-size:.75rem;opacity:.75">Roster (${w.registrants.length} registered, ${w.joinedCount} present, ${w.boundCount} tenant-bound)
            <textarea id="ws-ed-roster" rows="5" ${ro ? 'disabled' : ''} style="width:100%">${escapeHtml(w.registrants.join('\n'))}</textarea></label>
        <!-- Provisioning. Public, read-only route (no credentials in the payload),
             so the raw JSON link is safe to hand to anyone already in here. -->
        <div style="margin-top:12px;border-top:1px solid var(--border,#243043);padding-top:10px">
            <div style="display:flex;align-items:center;gap:8px">
                <strong style="font-size:.8rem">Fleet</strong>
                <button class="btn btn-small btn-secondary" id="ws-ed-fleet-refresh" data-action>Refresh</button>
                <a class="btn btn-small btn-secondary" id="ws-ed-fleet-raw" target="_blank" rel="noopener"
                   href="/api/workshops/${encodeURIComponent(sessionId)}/fleet">Open API ↗</a>
            </div>
            <div id="ws-ed-fleet" style="font-size:.78rem;margin-top:8px"><span class="loading">Loading fleet…</span></div>
        </div>
        <div id="ws-ed-msg" style="font-size:12px;min-height:18px;margin:8px 0"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
            ${ro ? '' : '<button class="btn btn-small" id="ws-ed-save" data-action>Save changes</button>'}
            <span style="flex:1"></span>
            <button class="btn btn-small btn-secondary" id="ws-ed-start" data-action>Start</button>
            <button class="btn btn-small btn-secondary" id="ws-ed-end" data-action>End</button>
            <button class="btn btn-small btn-danger" id="ws-ed-cancel" data-action>Cancel workshop</button>
            <button class="btn btn-small btn-danger" id="ws-ed-delete" data-action>Delete</button>
        </div>
    </div>`;
    document.body.appendChild(el);
    el.addEventListener('click', (e) => { if (e.target === el) wsCloseEditor(); });
    document.getElementById('ws-ed-close').addEventListener('click', wsCloseEditor);
    document.getElementById('ws-ed-fleet-refresh').addEventListener('click', () => wsLoadFleet(sessionId));
    wsLoadFleet(sessionId);

    const msg = (text, ok) => {
        const m = document.getElementById('ws-ed-msg');
        m.textContent = text;
        m.style.color = ok ? 'var(--ok,#4ade80)' : 'var(--danger,#f87171)';
    };
    const run = async (fn, done) => {
        try { await fn(); msg(done, true); await wsLoadSchedule(); wsCloseEditor(); }
        catch (err) { msg('✗ ' + err.message, false); }
    };

    if (!ro) document.getElementById('ws-ed-save').addEventListener('click', () => run(async () => {
        await wsLiveAction(`/${sessionId}`, {
            trainerEmail: w.owner,
            title: document.getElementById('ws-ed-title').value.trim(),
            scheduledAt: document.getElementById('ws-ed-when').value.trim(),
            timezone: document.getElementById('ws-ed-tz').value.trim(),
            durationMinutes: document.getElementById('ws-ed-dur').value.trim(),
            maxSeats: document.getElementById('ws-ed-seats').value.trim(),
            trainers: document.getElementById('ws-ed-trainers').value.split(',').map(s => s.trim()).filter(Boolean),
            roster: document.getElementById('ws-ed-roster').value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean),
        }, 'PATCH');
    }, '✓ saved'));

    document.getElementById('ws-ed-start').addEventListener('click', () =>
        run(() => wsLiveAction(`/${sessionId}/start`, { trainerEmail: w.owner }), '✓ started'));
    document.getElementById('ws-ed-end').addEventListener('click', () => {
        if (!confirm(`End "${w.title}"? Learner environments are NOT terminated by this — use the app's board for that.`)) return;
        run(() => wsLiveAction(`/${sessionId}/end`, { trainerEmail: w.owner }), '✓ ended');
    });
    document.getElementById('ws-ed-cancel').addEventListener('click', () => {
        if (!confirm(`Cancel "${w.title}"? Registrants keep their registration but the workshop will not run.`)) return;
        run(() => wsLiveAction(`/${sessionId}/cancel`, { trainerEmail: w.owner }), '✓ cancelled');
    });
    document.getElementById('ws-ed-delete').addEventListener('click', () => {
        if (!confirm(`DELETE "${w.title}" permanently? This removes the workshop, its roster and its audit trail. This cannot be undone.`)) return;
        run(() => wsLiveAction(`/${sessionId}`, { trainerEmail: w.owner }, 'DELETE'), '✓ deleted');
    });
}

// ── Register Tenant tab (app deploy via platform token / COE auto) ────────────
let regWired = false;

function wireRegister() {
    if (regWired) return; regWired = true;
    document.getElementById('reg-oa-deploy').addEventListener('click', () => goRegisterOauth('deploy'));
    document.getElementById('reg-oa-undeploy').addEventListener('click', () => goRegisterOauth('undeploy'));
}

function setRegBusy(b) {
    document.getElementById('reg-spin').classList.toggle('busy', b);
    document.getElementById('reg-bar').hidden = !b;
    for (const id of ['reg-oa-deploy', 'reg-oa-undeploy']) {
        const el = document.getElementById(id);
        if (el) el.disabled = b;
    }
}

async function goRegisterOauth(action) {
    const t = document.getElementById('reg-oa-tenant').value.trim();
    const cid = document.getElementById('reg-oa-cid').value.trim();
    const sec = document.getElementById('reg-oa-secret').value.trim();
    const urn = document.getElementById('reg-oa-urn').value.trim();
    const email = (document.getElementById('reg-oa-email') || { value: '' }).value.trim();
    const friendly = (document.getElementById('reg-oa-name') || { value: '' }).value.trim();
    const m = document.getElementById('reg-oa-msg');
    if (!t) { m.textContent = 'tenant required'; return; }
    if (!cid || !sec) { m.textContent = 'client id + secret required'; return; }
    if (!urn.startsWith('urn:dtaccount:')) { m.textContent = 'account URN required (urn:dtaccount:<uuid>)'; return; }
    m.textContent = ''; setRegBusy(true);
    try {
        const r = await fetch('/api/deploy/oauth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ action, tenant: t, clientId: cid, clientSecret: sec, accountUrn: urn, deployerEmail: email, friendlyName: friendly }) });
        const raw = await r.text();
        let j = {}; try { j = JSON.parse(raw); } catch (_) { /* non-JSON gateway page */ }
        if (r.ok) {
            document.getElementById('reg-oa-secret').value = '';
            if (action !== 'deploy') { m.textContent = '✓ undeployed ' + t; }
            else {
                const v = j.version || '?';
                const s = j.status === 'up-to-date' ? `already up-to-date (v${v})` : j.status === 'upgraded' ? `upgraded v${j.from} → v${v}` : `installed v${v}`;
                // The single fact that decides whether this tenant is finished or not:
                // did the client land in the tenant's own settings? "mint scopes verified"
                // alone used to read as done when nothing had been configured at all.
                const stored = /^(stored|updated)/.test(j.mintClient || '');
                const mint = !j.mintReady
                    ? ' · <strong style="color:#d29922">mint scopes MISSING</strong> (grant platform-token:tokens:write + :manage on the account, then register again)'
                    : stored
                        ? ' · <strong style="color:#2da44e">token minting configured</strong> — this tenant now mints its own tokens and updates itself'
                        : ` · <strong style="color:#d29922">client NOT stored on the tenant</strong> (${escapeHtml(j.mintClient || 'unknown')}) — paste it in the app under Settings → Training Token Minting`;
                m.innerHTML = `✓ ${s} — <a href="${escapeHtml(j.url || '#')}" target="_blank">open app</a>` + mint
                    + (j.profile ? ` · the trainings delivered to this tenant match the profile <strong>"${escapeHtml(j.profile)}"</strong>` : '')
                    + ((j.warnings || []).length ? `<br><span class="content-hint">⚠ ${j.warnings.map(escapeHtml).join('<br>⚠ ')}</span>` : '');
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

// goRegister (token-paste deploy) removed with its UI card — /api/deploy/token remains
// for the in-app Admin "Update now" flow (stash + app-start).

async function loadRegisterAudit() {
    try {
        const r = await fetch('/api/deploy/audit?limit=30', { credentials: 'same-origin' });
        const data = r.ok ? await r.json() : { audit: [] };
        const audit = data.audit || [];
        const b = document.querySelector('#reg-audit tbody');
        b.innerHTML = audit.length
            ? audit.map(a => `<tr><td>${escapeHtml((a.ts || '').replace('T', ' ').slice(0, 19))}</td><td>${escapeHtml(a.user || '')}</td><td>${escapeHtml(a.tenant || '')}</td><td>${a.tenant ? stageBadge(stageOf(a.tenant)) : '<span class="content-hint">—</span>'}</td><td>${escapeHtml(a.action || '')}</td><td>${escapeHtml(a.result || '')}</td><td>${escapeHtml(a.to || a.version || '')}</td><td>${escapeHtml(a.via || '')}</td></tr>`).join('')
            : '<tr><td colspan="8" class="content-hint">none yet</td></tr>';
    } catch (e) { /* ignore */ }
}

// Tenant-attribution registry (EPIC-002 §9) — who deployed where. Writer-gated:
// anonymous callers get a 401 and see the sign-in hint instead of rows.
async function loadTenantRegistry() {
    const b = document.querySelector('#reg-tenants tbody');
    if (!b) return;
    try {
        const r = await fetch('/api/tenants/registry', { credentials: 'same-origin' });
        if (!r.ok) { b.innerHTML = '<tr><td colspan="9" class="content-hint">Sign in as an org member to view the tenant registry.</td></tr>'; return; }
        const rows = (await r.json()).tenants || [];
        const d = s => escapeHtml((s || '').replace('T', ' ').slice(0, 19));
        b.innerHTML = rows.length
            ? rows.map(t => `<tr><td><code>${escapeHtml(t.tenant || '')}</code></td><td>${escapeHtml(t.friendlyName || '')}</td><td>${escapeHtml(t.deployerEmail || '')}</td><td>${escapeHtml(t.identityName ? `${t.identityName} <${t.identityEmail || ''}>` : (t.identityEmail || ''))}</td><td><code>${escapeHtml(t.accountUrn || '')}</code></td><td>${escapeHtml(t.via || '')}</td><td>${escapeHtml(t.appVersion || '')}</td><td>${d(t.firstSeen)}</td><td>${d(t.lastDeploy)}</td></tr>`).join('')
            : '<tr><td colspan="9" class="content-hint">none yet</td></tr>';
    } catch (e) { /* ignore */ }
}

function loadRegister() {
    wireRegister();
    loadRegisterAudit();
    loadTenantRegistry();
}

