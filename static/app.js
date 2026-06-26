(function() {
  const INTERVALS = [5000, 15000, 60000];
  let intervalIdx = 0;
  let pollTimer = null;

  function shouldPoll() {
    return document.visibilityState !== 'hidden';
  }

  function schedulePoll(tableEl) {
    if (pollTimer) clearTimeout(pollTimer);
    const delay = INTERVALS[Math.min(intervalIdx, INTERVALS.length - 1)];
    pollTimer = setTimeout(function() {
      if (shouldPoll()) {
        htmx.trigger(tableEl, 'poll');
        intervalIdx = Math.min(intervalIdx + 1, INTERVALS.length - 1);
        schedulePoll(tableEl);
      }
    }, delay);
  }

  document.addEventListener('DOMContentLoaded', function() {
    const tableEl = document.getElementById('job-items-table');
    if (!tableEl) return;
    const jobComplete = tableEl.dataset.jobComplete === 'true';
    if (!jobComplete) {
      schedulePoll(tableEl);
    }
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible' && !jobComplete) {
        intervalIdx = 0;
        schedulePoll(tableEl);
      } else if (document.visibilityState === 'hidden') {
        if (pollTimer) clearTimeout(pollTimer);
      }
    });
  });
})();

(function() {
  // Self-contained progress-bar auto-refresh. Re-finds #job-progress every tick
  // (the element is replaced on each refresh, so a captured reference would go
  // stale) and stops once it reports data-complete="true". Independent of htmx,
  // so it is robust regardless of how the item-table poller behaves.
  const INTERVAL_MS = 2000;

  function scheduleNext() {
    setTimeout(refresh, INTERVAL_MS);
  }

  function refresh() {
    const el = document.getElementById('job-progress');
    if (!el || el.dataset.complete === 'true') return;
    if (document.visibilityState === 'hidden') {
      scheduleNext();
      return;
    }
    const url = el.dataset.progressUrl;
    if (!url) return;
    fetch(url, { cache: 'no-store', credentials: 'same-origin' })
      .then(function(r) { return r.ok ? r.text() : null; })
      .then(function(html) {
        const cur = document.getElementById('job-progress');
        if (html && cur) {
          // Parse the same-origin server fragment without executing scripts.
          const fresh = new DOMParser()
            .parseFromString(html, 'text/html')
            .getElementById('job-progress');
          if (fresh) cur.replaceWith(document.importNode(fresh, true));
        }
        scheduleNext();
      })
      .catch(scheduleNext);
  }

  document.addEventListener('DOMContentLoaded', function() {
    const el = document.getElementById('job-progress');
    if (el && el.dataset.complete !== 'true') {
      scheduleNext();
    }
  });
})();
