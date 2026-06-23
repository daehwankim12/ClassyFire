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
