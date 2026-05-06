/* Enablement Ops Dashboard — Client */

const API = '';

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

async function loadFleet() {
    const res = await fetch(`${API}/api/repos`);
    const data = await res.json();
    const tbody = document.getElementById('fleet-body');

    tbody.innerHTML = data.repos.map(repo => {
        const arm = repo.builds.arm64;
        const amd = repo.builds.amd64;
        return `<tr>
            <td><strong>${repo.name}</strong><br><span style="color:var(--text-muted);font-size:0.75rem">${repo.repo}</span></td>
            <td><span class="arch-badge">${repo.arch}</span></td>
            <td>${buildCell(arm)}</td>
            <td>${buildCell(amd)}</td>
            <td>${repo.duration}</td>
            <td>
                <button class="btn btn-small" onclick="triggerBuild('${repo.repo}', 'both')">Build All</button>
                <button class="btn btn-small" onclick="triggerBuild('${repo.repo}', 'arm64')">ARM</button>
                <button class="btn btn-small" onclick="triggerBuild('${repo.repo}', 'amd64')">AMD</button>
            </td>
        </tr>`;
    }).join('');

    // Filter handlers
    document.getElementById('repo-filter').addEventListener('input', e => {
        const filter = e.target.value.toLowerCase();
        tbody.querySelectorAll('tr').forEach(row => {
            row.style.display = row.textContent.toLowerCase().includes(filter) ? '' : 'none';
        });
    });
}

function buildCell(build) {
    if (!build) return '<span class="status-none">—</span>';
    const cls = build.passed ? 'status-pass' : 'status-fail';
    const icon = build.passed ? 'PASS' : 'FAIL';
    const time = build.duration ? `${build.duration}s` : '';
    return `<span class="${cls}">${icon}</span> <span style="color:var(--text-muted);font-size:0.75rem">${time}</span>`;
}

async function triggerBuild(repo, arch) {
    await fetch(`${API}/api/builds/trigger`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo, arch, requested_by: 'dashboard' }),
    });
    // Refresh queue status
    loadWorkers();
}

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
        const status = r.passed ? 'PASS' : 'FAIL';
        return `<tr>
            <td>${job.repo.split('/').pop()}</td>
            <td><span class="arch-badge">${r.arch || job.worker_arch || '?'}</span></td>
            <td><span class="${cls}">${status}</span></td>
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

checkHealth();
loadFleet();
loadWorkers();
loadNightly();

// Auto-refresh every 30s
setInterval(() => {
    checkHealth();
    loadWorkers();
}, 30000);

// Refresh fleet every 2min
setInterval(loadFleet, 120000);
