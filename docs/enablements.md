
## Enablement Repositories

All enablements are part of the Dynatrace Enablement Framework and browsable on [**The Hub**](https://dynatrace-wwse.github.io){:target="_blank"} — the central index of every managed lab, workshop, and demo.

The table below is generated live from the registry and always reflects the current set of managed repositories.

<div id="repos-container" markdown="0">
  <p style="color:var(--md-default-fg-color--light);padding:1rem 0">Loading repositories…</p>
</div>

<script>
(function () {
  var container = document.getElementById('repos-container');

  function tagSpan(t) {
    return '<span style="display:inline-block;padding:.1em .45em;margin:.1em .15em .1em 0;border-radius:999px;font-size:.65rem;font-weight:700;letter-spacing:.06em;background:rgba(0,180,222,.12);border:1px solid rgba(0,180,222,.3);color:#00b4de">' + t + '</span>';
  }

  fetch('https://dynatrace-wwse.github.io/repos.json')
    .then(function(r) { return r.json(); })
    .then(function(repos) {

      repos.sort(function(a, b) {
        var order = function(s) {
          if (s.startsWith('enablement')) return 0;
          if (s.startsWith('workshop'))   return 1;
          if (s.startsWith('demo'))       return 2;
          if (s === 'codespaces-framework') return 99;
          return 3;
        };
        var d = order(a.repo) - order(b.repo);
        return d !== 0 ? d : a.repo.localeCompare(b.repo);
      });

      var rows = repos.map(function(r) {
        if (r.repo === 'codespaces-framework') return '';
        var docsUrl = 'https://dynatrace-wwse.github.io/' + r.repo + '/';
        var ghUrl   = 'https://github.com/dynatrace-wwse/' + r.repo;
        var tags    = (r.tags || []).map(tagSpan).join('');
        var dur     = r.duration ? '<span style="font-size:.75rem;white-space:nowrap">' + r.duration + '</span>' : '—';
        return '<tr>'
          + '<td><a href="' + docsUrl + '" target="_blank" rel="noopener"><strong>' + (r.title || r.repo) + '</strong></a>'
          + '<br><a href="' + ghUrl + '" target="_blank" rel="noopener" style="font-size:.7rem;opacity:.7">' + r.repo + '</a></td>'
          + '<td style="font-size:.75rem">' + (r.desc || '') + '</td>'
          + '<td>' + tags + '</td>'
          + '<td style="text-align:center">' + dur + '</td>'
          + '</tr>';
      }).join('');

      container.innerHTML =
        '<table>'
        + '<thead><tr>'
        + '<th>Enablement</th>'
        + '<th>Description</th>'
        + '<th>Tags</th>'
        + '<th style="text-align:center">Duration</th>'
        + '</tr></thead>'
        + '<tbody>' + rows + '</tbody>'
        + '</table>';
    })
    .catch(function() {
      container.innerHTML = '<p>Could not load repository list — visit <a href="https://dynatrace-wwse.github.io" target="_blank">The Hub</a> directly.</p>';
    });
})();
</script>

---

!!! tip "Contribute or request a new enablement"
    Open an issue on [codespaces-framework](https://github.com/dynatrace-wwse/codespaces-framework/issues){:target="_blank"} or reach out to the COE team.

<div class="grid cards" markdown>
- [Continue to What's Next →](whats-next.md)
</div>
