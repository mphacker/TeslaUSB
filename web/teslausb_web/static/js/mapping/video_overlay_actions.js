function closeVideoOverlay() {
    // Bump the open-sequence counter so any in-flight lazy-load
    // telemetry fetch (started by openVideoOverlay) discards its
    // response when it returns.
    ++overlayOpenSeq;
    overlayTelemetryCache = null;
    _stopHudRaf();
    onOverlayTimeUpdate._cachedBasePath = null;
    onOverlayTimeUpdate._cachedClipWps = null;
    onOverlayTimeUpdate._cachedClipTs = null;
    const existing = document.getElementById('videoOverlay');
    if (existing) {
        const video = existing.querySelector('video');
        if (video) {
            video.removeEventListener('timeupdate', onOverlayTimeUpdate);
            video.pause();
            const prevSrc = video.src;
            // Properly abort any pending load. Setting src='' resolves to
            // the document base URL (e.g. https://host/mapping) and the
            // browser then tries to load THAT as a video, fails, and fires
            // a spurious 'error' event that surfaced a false "video file
            // not found" toast every time the overlay was closed. The HTML5
            // standard reset is removeAttribute('src') + load().
            video.removeAttribute('src');
            video.load();
            if (prevSrc && prevSrc.startsWith('blob:')) {
                try { URL.revokeObjectURL(prevSrc); } catch (e) {}
            }
        }
        existing.remove();
    }
    if (overlayDragController) {
        overlayDragController.abort();
        overlayDragController = null;
    }
    overlayWaypoints = [];
    overlayClips = [];
}

function overlayDownload() {
    const overlay = document.getElementById('videoOverlay');
    if (!overlay) return;
    const folder = overlay.dataset.folder;
    const evt = overlay.dataset.event;
    if (folder && evt) {
        window.location.href = '/videos/download_event/' + encodeURIComponent(folder) + '/' + encodeURIComponent(evt);
    } else if (overlayBasePath) {
        // Fall back to downloading the current single file
        window.location.href = '/videos/download/' + encodeURIComponent(overlayBasePath + '-' + overlayCurrentAngle + '.mp4');
    }
}

async function overlayDelete() {
    const overlay = document.getElementById('videoOverlay');
    if (!overlay) return;
    const folder = overlay.dataset.folder;
    const evt = overlay.dataset.event;
    if (!folder || !evt) {
        if (typeof showToast === 'function') showToast('Cannot determine clip to delete', 'warning');
        return;
    }
    if (!confirm('Delete "' + evt + '" and all camera angles?')) return;
    try {
        const resp = await fetch('/videos/delete_event/' + encodeURIComponent(folder) + '/' + encodeURIComponent(evt), { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            closeVideoOverlay();
            if (typeof showToast === 'function') showToast('Deleted ' + data.deleted_count + ' files', 'success');
        } else {
            if (typeof showToast === 'function') showToast(data.error || 'Delete failed', 'error');
        }
    } catch(e) {
        if (typeof showToast === 'function') showToast('Delete failed: ' + e.message, 'error');
    }
}

let _archivePollTimer = null;

async function overlayArchive() {
    const overlay = document.getElementById('videoOverlay');
    if (!overlay) return;
    const folder = overlay.dataset.folder;
    const evt = overlay.dataset.event;
    if (!folder || !evt) {
        if (typeof showToast === 'function') showToast('Cannot determine clip to archive', 'warning');
        return;
    }
    const btn = document.getElementById('archiveNavBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = ICON_SPINNING; btn.title = 'Archiving...'; }

    try {
        const resp = await fetch('/cloud/api/archive_file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ folder: folder, event: evt })
        });
        const data = await resp.json();
        if (data.success) {
            if (typeof showToast === 'function') showToast(data.message, 'success');
            _startArchiveNavPoll(btn);
        } else {
            if (typeof showToast === 'function') showToast(data.message || 'Archive failed', 'error');
            _resetArchiveNavBtn(btn);
        }
    } catch(e) {
        if (typeof showToast === 'function') showToast('Archive failed: ' + e.message, 'error');
        _resetArchiveNavBtn(btn);
    }
}

function _startArchiveNavPoll(btn) {
    _archivePollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/cloud/api/archive_status', {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            });
            const st = await resp.json();
            if (st.completed) {
                clearInterval(_archivePollTimer); _archivePollTimer = null;
                if (btn) { btn.innerHTML = ICON_CHECK; btn.classList.add('is-complete'); btn.title = 'Archived!'; btn.setAttribute('aria-label', 'Archived to cloud'); }
                if (typeof showToast === 'function') showToast('Archived to cloud!', 'success');
                setTimeout(() => _resetArchiveNavBtn(btn), 4000);
            } else if (st.error) {
                clearInterval(_archivePollTimer); _archivePollTimer = null;
                if (typeof showToast === 'function') showToast(st.error, 'error');
                _resetArchiveNavBtn(btn);
            } else if (st.running && btn) {
                let pct = st.total_size > 0 ? Math.min(99, Math.round((st.bytes_transferred || 0) / st.total_size * 100)) : 0;
                btn.textContent = pct + '%';
                btn.title = 'Archiving ' + (st.file_count || '') + ' files...';
            }
        } catch(e) { /* keep polling */ }
    }, 2000);
}

function _resetArchiveNavBtn(btn) {
    if (btn) { btn.disabled = false; btn.classList.remove('is-complete'); btn.innerHTML = ICON_CLOUD; btn.title = 'Archive to Cloud'; btn.setAttribute('aria-label', 'Archive to cloud'); }
}
