// --- Init ---
//
// Render the selected day FIRST (using the bootstrap-supplied date /
// latest_date), then fetch /api/days + stats in the background so the
// day picker and stat cards populate without blocking first paint.
// /api/days and /api/stats currently rebuild the snapshot on cold hit
// and can take 10+ s on a busy device — making them blockers for the
// initial render was the #1 source of operator-reported "page is very
// slow" complaints.
(function initFromUrl() {
    const urlState = readUrlState();
    if (urlState && urlState.date) {
        currentDate = urlState.date;
    }
    const bootstrapDate = (BOOTSTRAP.view && (BOOTSTRAP.view.date || BOOTSTRAP.view.latest_date)) || '';
    if (!currentDate && bootstrapDate) {
        currentDate = bootstrapDate;
    }
    if (currentDate) {
        // Kick off the actual day render immediately. loadDays() runs
        // in parallel to populate the day picker; it will skip the
        // selection-change branch because ``currentDate`` already
        // matches the day we just rendered.
        loadDay(currentDate);
    }
    loadDays();
    loadStats();
})();

// Browser back/forward — re-derive state from the URL and dispatch
// without writing back to history (skipHistory). Guards against
// firing before the initial loadDays() resolved by waiting for
// allDays to be populated.
window.addEventListener('popstate', function () {
    const state = readUrlState();
    if (!allDays.length) return;
    const target = (state && state.date &&
                    allDays.some(d => d.date === state.date))
        ? state.date
        : allDays[0].date;
    loadDay(target, { skipHistory: true });
});

// Keyboard shortcuts: left/right arrows cycle days. Skip while the
// user is typing into a form control or has the video overlay open
// (it has its own clip-cycling behavior bound to the same keys).
document.addEventListener('keydown', function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const tag = (e.target && e.target.tagName) || '';
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable)) {
        return;
    }
    // If the video overlay is open, let it own the arrow keys.
    const overlay = document.getElementById('videoOverlay');
    if (overlay && overlay.style.display !== 'none' && overlay.offsetParent !== null) {
        return;
    }
    if (e.key === 'ArrowLeft') {
        e.preventDefault();
        cycleDay(-1);
    } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        cycleDay(1);
    }
});

// Indexing is owned by the Rust worker; nothing to poll here.
