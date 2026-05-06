/* Enablement Ops Dashboard — Client */

const API = '';

// Auth state — checked on load via oauth2-proxy /oauth2/userinfo.
// authState.signedIn determines whether trigger buttons are enabled.
const authState = { signedIn: false, user: null };

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
    renderAuthHeader();
}

function renderAuthHeader() {
    const signInBtn = document.getElementById('sign-in-btn');
    const userInfo = document.getElementById('user-info');
    const userName = document.getElementById('user-name');
    if (authState.signedIn) {
        signInBtn.hidden = true;
        userInfo.hidden = false;
        userName.textContent = authState.user;
    } else {
        signInBtn.hidden = false;
        userInfo.hidden = true;
    }
}

// ── Tab Navigation ──────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`view-${tab.dataset.view}`).classList.add('active');
    });
});

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
                let spinner = cell.querySelector('.spinner');
                if (isRunning && !spinner) {
                    const r = runningMap[`${repo}|${arch}`];
                    cell.insertAdjacentHTML('afterbegin',
                        `<a class="spinner log-link" title="View live log" href="#"
                            data-job-id="${r.job_id}" data-arch="${arch}">⟳</a> `);
                } else if (!isRunning && spinner) {
                    spinner.remove();
                }
            }
        });
    } catch {}
}

async function loadFleet() {
    const res = await fetch(`${API}/api/repos`);
    const data = await res.json();
    const tbody = document.getElementById('fleet-body');

    const disabled = authState.signedIn ? '' : 'disabled title="Sign in with GitHub to trigger builds"';
    tbody.innerHTML = data.repos.map(repo => {
        const arm = repo.builds.arm64;
        const amd = repo.builds.amd64;
        const safeRepo = repo.repo.replace(/[^a-z0-9-]/gi, '_');
        return `<tr data-repo="${repo.repo}">
            <td><strong>${repo.name}</strong><br><span style="color:var(--text-muted);font-size:0.75rem">${repo.repo}</span></td>
            <td><span class="arch-badge">${repo.arch}</span></td>
            <td data-arch="arm64">${buildCell(arm)}</td>
            <td data-arch="amd64">${buildCell(amd)}</td>
            <td>
                <input class="branch-input" id="branch-${safeRepo}"
                       type="text" value="main" placeholder="main"
                       size="10" autocomplete="off">
            </td>
            <td>
                <select class="arch-select" id="arch-${safeRepo}" ${disabled}>
                    <option value="both">both</option>
                    <option value="arm64">arm64</option>
                    <option value="amd64">amd64</option>
                </select>
                <button class="btn btn-small" ${disabled}
                        onclick="triggerBuildFromRow('${repo.repo}', '${safeRepo}', this)">
                    Trigger
                </button>
            </td>
        </tr>`;
    }).join('');

    // Filter handlers
    const filt = document.getElementById('repo-filter');
    filt.oninput = e => {
        const filter = e.target.value.toLowerCase();
        tbody.querySelectorAll('tr').forEach(row => {
            row.style.display = row.textContent.toLowerCase().includes(filter) ? '' : 'none';
        });
    };

    // Wire spinners that already exist on first paint
    await loadRunning();
}

function buildCell(build) {
    if (!build) return '<span class="status-none">—</span>';
    const cls = build.passed ? 'status-pass' : 'status-fail';
    const icon = build.passed ? 'PASS' : 'FAIL';
    let status;
    if (build.job_id) {
        status = `<a href="/api/jobs/${build.job_id}/log" target="_blank" class="${cls} log-link" title="View worker log">${icon}</a>`;
    } else if (build.run_url) {
        status = `<a href="${build.run_url}" target="_blank" rel="noopener" class="${cls} log-link" title="View run on GitHub Actions">${icon}</a>`;
    } else {
        status = `<span class="${cls}">${icon}</span>`;
    }
    return `${status}`;
}

async function triggerBuildFromRow(repo, safeRepo, btn) {
    if (!authState.signedIn) {
        window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.pathname);
        return;
    }
    const branch = document.getElementById(`branch-${safeRepo}`).value.trim() || 'main';
    const arch = document.getElementById(`arch-${safeRepo}`).value;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`${API}/api/builds/trigger`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ repo, arch, ref: branch, requested_by: 'dashboard' }),
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

// ── Live log modal ──────────────────────────────────────────────────────────

let livelogPoll = null;

function openLiveLog(jobId, title) {
    document.getElementById('livelog-title').textContent = title;
    const pre = document.getElementById('livelog-pre');
    pre.textContent = 'Loading…';
    document.getElementById('livelog-modal').hidden = false;
    if (livelogPoll) clearInterval(livelogPoll);
    const fetchOnce = async () => {
        try {
            // Try livelog first (running). 404 → fall back to final log.
            let res = await fetch(`/api/jobs/${jobId}/livelog`);
            if (res.status === 404) {
                res = await fetch(`/api/jobs/${jobId}/log`);
                if (livelogPoll) { clearInterval(livelogPoll); livelogPoll = null; }
            }
            if (res.ok) {
                pre.textContent = await res.text();
                pre.scrollTop = pre.scrollHeight;
            }
        } catch {}
    };
    fetchOnce();
    livelogPoll = setInterval(fetchOnce, 2000);
}

function closeLiveLog() {
    document.getElementById('livelog-modal').hidden = true;
    if (livelogPoll) { clearInterval(livelogPoll); livelogPoll = null; }
}

document.addEventListener('click', e => {
    if (e.target.id === 'livelog-close') closeLiveLog();
    const link = e.target.closest('a.spinner');
    if (link && link.dataset.jobId) {
        e.preventDefault();
        const row = link.closest('tr');
        const repo = row ? row.dataset.repo : '';
        openLiveLog(link.dataset.jobId, `${repo} (${link.dataset.arch})`);
    }
});

// ── Workers View ────────────────────────────────────────────────────────────

async function loadWorkers() {
    const [workersRes, buildsRes] = await Promise.all([
        fetch(`${API}/api/workers`),
        fetch(`${API}/api/builds/running`),
    ]);
    const workersData = await workersRes.json();
    const buildsData = await buildsRes.json();

    const grid = document.getElementById('worker-grid');
    if (workersData.workers.length === 0) {
        grid.innerHTML = '<p class="loading">No workers registered</p>';
    } else {
        grid.innerHTML = workersData.workers.map(w => `
            <div class="worker-card ${w.status}">
                <h4>${w.worker_id}</h4>
                <div class="meta">
                    <div>Arch: <strong>${w.arch}</strong></div>
                    <div>Active: ${w.active_jobs} / ${w.capacity}</div>
                    <div>Status: ${w.status}</div>
                    <div>Last heartbeat: ${formatTime(w.last_heartbeat)}</div>
                </div>
            </div>
        `).join('');
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
        const cls = r.passed ? 'status-pass' : 'status-fail';
        const label = r.passed ? 'PASS' : 'FAIL';
        const status = job.job_id
            ? `<a href="/api/jobs/${job.job_id}/log" target="_blank" class="${cls} log-link">${label}</a>`
            : `<span class="${cls}">${label}</span>`;
        return `<tr>
            <td>${job.repo.split('/').pop()}</td>
            <td><span class="arch-badge">${r.arch || job.worker_arch || '?'}</span></td>
            <td>${status}</td>
            <td>${r.duration_seconds || 0}s</td>
            <td>${formatTime(job.finished_at)}</td>
        </tr>`;
    }).join('');
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function formatTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// ── Init ────────────────────────────────────────────────────────────────────

(async () => {
    await loadAuthState();   // sets authState.signedIn before fleet renders
    checkHealth();
    loadFleet();
    loadWorkers();
    loadNightly();
})();

// Auto-refresh
setInterval(() => { checkHealth(); loadWorkers(); }, 30000);
setInterval(loadRunning, 5000);    // spinner liveness
setInterval(loadFleet, 120000);
