async function loadVideoList(append) {
    if (vpLoading) return;
    const folder = document.getElementById('vpFolder').value;
    const list = document.getElementById('vpList');
    const loadSeq = vpLoadSeq;
    if (!append) {
        list.innerHTML = '<div class="vp-loading">Loading\u2026</div>';
    }
    vpLoading = true;

    try {
        const resp = await fetch('/videos/?folder=' + encodeURIComponent(folder) + '&page=' + vpPage, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (loadSeq !== vpLoadSeq || vpCurrentTab !== 'clips') return;
        vpHasNext = data.has_next;
        vpFolderStructure = data.folder_structure || 'events';

        var existingSentinel = list.querySelector('.vp-sentinel');
        if (existingSentinel) existingSentinel.remove();

        if (!append) list.innerHTML = '';
        if (!data.events || data.events.length === 0) {
            if (!append) list.innerHTML = '<div class="vp-empty">No videos in this folder</div>';
            return;
        }

        // Show summary count on first page load
        if (!append && data.total_count) {
            const totalVids = data.total_video_count || 0;
            const summary = document.createElement('div');
            summary.className = 'st-summary';
            summary.innerHTML = '<strong>' + data.total_count + ' Clip' + (data.total_count !== 1 ? 's' : '') + '</strong>' + (totalVids > 0 ? ' \u00b7 <span>' + totalVids + ' video' + (totalVids !== 1 ? 's' : '') + '</span>' : '');
            list.appendChild(summary);
        }

        data.events.forEach(function(ev) {
            const card = document.createElement('div');
            card.className = 'vp-clip';

            const name = ev.name || 'Unknown';
            const safeName = name.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            const dateStr = name.replace(/_/g, ' ').substring(0, 16);
            const safeDateStr = dateStr.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            const sizeMb = ev.size_mb || 0;
            const cameras = Object.keys(ev.camera_videos || {}).length;
            const reason = ev.reason || '';
            const safeReason = reason.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            const hasCoords = (typeof ev.lat === 'number' && isFinite(ev.lat) &&
                               typeof ev.lon === 'number' && isFinite(ev.lon));

            card.innerHTML =
                '<div class="vp-clip-info">' +
                    '<div class="vp-clip-date">' + safeDateStr + '</div>' +
                    '<div class="vp-clip-meta">' + cameras + ' cam\u00b7 ' + sizeMb.toFixed(0) + ' MB</div>' +
                    (reason ? '<div class="vp-clip-reason">' + safeReason + '</div>' : '') +
                '</div>' +
                '<div class="vp-actions">' +
                    '<button class="vp-btn vp-btn-play" type="button" title="Play" aria-label="Play clip">' + ICON_PLAY + '</button>' +
                    (hasCoords
                        ? '<button class="vp-btn vp-btn-map" type="button" title="Show on Map" aria-label="Show clip location on map">' + ICON_MAP_PIN + '</button>'
                        : '') +
                    '<button class="vp-btn vp-btn-dl" type="button" title="Download ZIP" aria-label="Download clip ZIP">' + ICON_DOWNLOAD + '</button>' +
                    (CLOUD_ARCHIVE_ENABLED
                        ? '<button class="vp-btn vp-btn-archive" type="button" title="Archive to Cloud" aria-label="Archive clip to cloud">' + ICON_CLOUD + '</button>'
                        : '') +
                    '<button class="vp-btn vp-btn-danger vp-btn-del" type="button" title="Delete" aria-label="Delete clip">' + ICON_TRASH + '</button>' +
                '</div>';

            // Store data attributes for event delegation (safe from XSS)
            card.dataset.folder = folder;
            card.dataset.name = name;
            card.dataset.structure = vpFolderStructure;
            if (hasCoords) {
                card.dataset.lat = String(ev.lat);
                card.dataset.lon = String(ev.lon);
            }
            list.appendChild(card);
        });

        appendVpScrollSentinel(list, function() { vpPage++; loadVideoList(true); });
    } catch(e) {
        console.error('Failed to load videos:', e);
        if (!append) list.innerHTML = '<div class="vp-empty">Failed to load videos</div>';
    } finally {
        if (loadSeq === vpLoadSeq) vpLoading = false;
    }
}

function vpChangeFolder() {
    // Switching folders must restart paging: the new folder's first
    // page is page 1. Reusing a stale vpPage (e.g. left at 2+ after
    // scrolling another folder) skips early items — a SavedClips folder
    // with a single event would then render empty. Mirrors vpShowClips().
    resetVpPaging();
    loadVideoList();
}

function vpPlay(folder, name, structure) {
    openVideoFromPanel(folder, name);
}

// Transient marker dropped by vpLocate so the operator can see exactly
// where an All-Clips item was recorded. Kept module-level so each locate
// replaces the previous one and a day render can clear it.
let vpLocateMarker = null;

function clearVpLocateMarker() {
    if (vpLocateMarker) {
        try { map.removeLayer(vpLocateMarker); } catch (e) { /* already gone */ }
        vpLocateMarker = null;
    }
}

function vpLocate(clip) {
    const lat = parseFloat(clip.dataset.lat);
    const lon = parseFloat(clip.dataset.lon);
    if (isNaN(lat) || isNaN(lon)) return;
    const name = clip.dataset.name || '';

    // Best-effort: switch to the clip's own day so the surrounding
    // route/events render, then centre + drop the locate marker.
    const dayMatch = name.match(/^(\d{4}-\d{2}-\d{2})/);
    const targetDay = dayMatch ? dayMatch[1] : null;

    const focus = function () {
        if (videoPanelOpen) toggleVideoPanel();
        clearVpLocateMarker();
        map.setView([lat, lon], 16);
        vpLocateMarker = L.marker([lat, lon], { icon: makeEventIcon('saved') }).addTo(map);
        const label = name.replace(/_/g, ' ').substring(0, 19) || 'Clip location';
        vpLocateMarker.bindPopup(
            '<div class="event-popup"><strong>' +
            label.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
            '</strong></div>'
        ).openPopup();
    };

    if (targetDay && targetDay !== currentDate) {
        Promise.resolve(loadDay(targetDay)).then(focus).catch(focus);
    } else {
        focus();
    }
}

async function openVideoFromPanel(folder, eventName) {
    // Close the video panel to see the map
    if (videoPanelOpen) toggleVideoPanel();

    try {
        // Fetch actual clip paths from the server
        const resp = await fetch(BOOTSTRAP.api.event_clips_template.replace('__FOLDER__', encodeURIComponent(folder)).replace('__EVENT__', encodeURIComponent(eventName)));
        const data = await resp.json();
        if (data.error) throw new Error(data.error);

        const frontClips = data.front_clips || [];
        if (frontClips.length === 0) {
            if (typeof showToast === 'function') showToast('No video clips found', 'warning');
            return;
        }

        // The trigger moment (from event.json) usually lands in the LAST
        // clip, not the first — the server resolves which clip + offset so
        // playback starts at the honk/event rather than minutes before it.
        let clipIdx = data.event_clip_index || 0;
        if (clipIdx < 0 || clipIdx >= frontClips.length) clipIdx = 0;
        const targetPath = frontClips[clipIdx];
        const seekSeconds = data.event_seek_seconds || 0;

        // Look up waypoints for the target clip directly from the DB
        let waypoints = [];
        try {
            const wpResp = await fetch(`${BOOTSTRAP.api.waypoints_for_clip}?path=${encodeURIComponent(targetPath)}`);
            const wpData = await wpResp.json();
            waypoints = wpData.waypoints || [];
        } catch(e) {
            console.warn('Waypoint lookup failed:', e);
        }

        // Open overlay with the target clip
        const wp = waypoints.length > 0
            ? (waypoints.find(w => w.video_path && targetPath.includes(getBasePath(w.video_path))) || waypoints[0])
            : { video_path: targetPath, frame_offset: 0 };
        // Ensure video_path points to the actual clip
        wp.video_path = targetPath;
        // frame_offset is expressed in frames (~36 fps); convert the
        // server-supplied seek seconds so the overlay seeks to the moment.
        if (seekSeconds > 0) wp.frame_offset = Math.round(seekSeconds * 36);

        const clickPoint = { x: window.innerWidth / 3, y: window.innerHeight / 4 };
        openVideoOverlay(wp, clickPoint, waypoints);

        // Store folder/event for download/delete
        const overlay = document.getElementById('videoOverlay');
        if (overlay) {
            overlay.dataset.folder = folder;
            overlay.dataset.event = eventName;
        }
    } catch(e) {
        console.error('Failed to open video:', e);
        if (typeof showToast === 'function') showToast('Failed to open video: ' + e.message, 'error');
    }
}

function vpDownload(folder, name) {
    window.location.href = '/videos/download_event/' + encodeURIComponent(folder) + '/' + encodeURIComponent(name);
}

async function vpDelete(folder, name) {
    if (!confirm('Delete "' + name + '" and all its camera angles?')) return;
    try {
        var resp = await fetch('/videos/delete_event/' + encodeURIComponent(folder) + '/' + encodeURIComponent(name), { method: 'POST' });
        var data = await resp.json();
        if (data.success) {
            if (typeof showToast === 'function') showToast('Deleted ' + data.deleted_count + ' files', 'success');
            loadVideoList();
        } else {
            if (typeof showToast === 'function') showToast(data.error || 'Delete failed', 'error');
        }
    } catch(e) {
        if (typeof showToast === 'function') showToast('Delete failed: ' + e.message, 'error');
    }
}

async function vpArchive(folder, name, btn) {
    if (btn) { btn.disabled = true; btn.innerHTML = ICON_SPINNING; }
    try {
        var resp = await fetch('/cloud/api/archive_file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ folder: folder, event: name })
        });
        var data = await resp.json();
        if (data.success) {
            if (typeof showToast === 'function') showToast(data.message, 'success');
            _vpArchivePoll(btn);
        } else {
            if (typeof showToast === 'function') showToast(data.message || 'Archive failed', 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = ICON_CLOUD; }
        }
    } catch(e) {
        if (typeof showToast === 'function') showToast('Archive failed: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = ICON_CLOUD; }
    }
}

function _vpArchivePoll(btn) {
    var timer = setInterval(async function() {
        try {
            var resp = await fetch('/cloud/api/archive_status', { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
            var st = await resp.json();
            if (st.completed) {
                clearInterval(timer);
                if (btn) { btn.innerHTML = ICON_CHECK; btn.classList.add('is-complete'); btn.title = 'Archived to cloud'; btn.setAttribute('aria-label', 'Archived to cloud'); }
                if (typeof showToast === 'function') showToast('Archived to cloud!', 'success');
                setTimeout(function() { if (btn) { btn.disabled = false; btn.classList.remove('is-complete'); btn.innerHTML = ICON_CLOUD; btn.title = 'Archive to Cloud'; btn.setAttribute('aria-label', 'Archive to cloud'); } }, 4000);
            } else if (st.error) {
                clearInterval(timer);
                if (typeof showToast === 'function') showToast(st.error, 'error');
                if (btn) { btn.disabled = false; btn.classList.remove('is-complete'); btn.innerHTML = ICON_CLOUD; btn.title = 'Archive to Cloud'; btn.setAttribute('aria-label', 'Archive to cloud'); }
            } else if (st.running && btn) {
                var pct = st.total_size > 0 ? Math.min(99, Math.round((st.bytes_transferred || 0) / st.total_size * 100)) : 0;
                btn.textContent = pct + '%';
            }
        } catch(e) { /* keep polling */ }
    }, 2000);
}

async function vpQueueForSync(folder, name, btn) {
    if (btn) { btn.disabled = true; }
    try {
        var resp = await fetch('/cloud/api/queue_event', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify({ folder: folder, event: name })
        });
        var data = await resp.json();
        if (data.success) {
            if (typeof showToast === 'function') showToast(data.message, 'success');
            if (btn) { btn.innerHTML = ICON_CHECK; btn.title = 'Queued for cloud sync'; btn.setAttribute('aria-label', 'Queued for cloud sync'); }
        } else {
            if (typeof showToast === 'function') showToast(data.message || 'Queue failed', 'error');
        }
    } catch(e) {
        if (typeof showToast === 'function') showToast('Queue failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; }
    }
}

// Event delegation for video panel buttons (prevents XSS from inline onclick)
document.getElementById('vpList').addEventListener('click', function(e) {
    var btn = e.target.closest('.vp-btn');
    if (!btn) return;

    // Sentry timeline event buttons
    var stEvent = btn.closest('.st-event');
    if (stEvent) {
        var folder = stEvent.dataset.folder;
        var name = stEvent.dataset.name;
        var videoPath = stEvent.dataset.videoPath;
        var evType = stEvent.dataset.eventType;
        var isFolderEvent = (evType === 'sentry' || evType === 'saved');

        if (btn.classList.contains('st-btn-play')) {
            if (isFolderEvent && name) {
                openVideoFromPanel(folder, name);
            } else if (videoPath) {
                // Driving event — open overlay with the video directly
                var lat = parseFloat(stEvent.dataset.lat);
                var lon = parseFloat(stEvent.dataset.lon);
                var frameOffset = parseInt(stEvent.dataset.frameOffset) || 0;
                openVideoOverlay({ video_path: videoPath, frame_offset: frameOffset, lat: lat, lon: lon });
                if (!isNaN(lat) && !isNaN(lon)) map.setView([lat, lon], 16);
            }
        } else if (btn.classList.contains('st-btn-dl')) {
            window.location.href = '/videos/download_event/' + encodeURIComponent(folder) + '/' + encodeURIComponent(name);
        } else if (btn.classList.contains('st-btn-map')) {
            var lat = parseFloat(btn.dataset.lat);
            var lon = parseFloat(btn.dataset.lon);
            if (!isNaN(lat) && !isNaN(lon)) {
                map.setView([lat, lon], 16);
                toggleVideoPanel();
            }
        } else if (btn.classList.contains('st-btn-del')) {
            vpDelete(folder, name).then(function() {
                if (vpCurrentTab === 'sentry') loadSentryTimeline();
            });
        }
        return;
    }

    // Clips tab buttons
    var clip = btn.closest('.vp-clip');
    if (!clip) return;
    var folder = clip.dataset.folder;
    var name = clip.dataset.name;
    var structure = clip.dataset.structure;
    if (btn.classList.contains('vp-btn-play')) vpPlay(folder, name, structure);
    else if (btn.classList.contains('vp-btn-map')) vpLocate(clip);
    else if (btn.classList.contains('vp-btn-dl')) vpDownload(folder, name);
    else if (btn.classList.contains('vp-btn-archive')) vpArchive(folder, name, btn);
    else if (btn.classList.contains('vp-btn-del')) vpDelete(folder, name);
});

