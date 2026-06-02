
function getBasePath(videoPath) {
    // "RecentClips/2026-04-02_18-00-10-front.mp4" → "RecentClips/2026-04-02_18-00-10"
    const angle = CAMERA_ANGLES.find(a => videoPath.includes('-' + a + '.'));
    return angle ? videoPath.replace(`-${angle}.mp4`, '') : videoPath.replace('.mp4', '');
}

// True when a clip belongs to a multi-camera family (filename ends in
// "-<angle>.mp4" for one of CAMERA_ANGLES). Single-file clips such as a
// Sentry/Saved grid-view "event.mp4" have no camera-angle suffix and no
// sibling angles to switch between, so the overlay must play them as-is
// rather than synthesising a non-existent "-front.mp4" variant.
function isMultiCameraClip(videoPath) {
    if (!videoPath) return false;
    const filename = videoPath.split('/').pop();
    return CAMERA_ANGLES.some(a => filename.includes('-' + a + '.'));
}

// Issue #184 Wave 3 — Phase D. Monotonic sequence tagged on each
// overlay open. Used by the lazy telemetry fetch to discard a
// stale response when the user opens a different clip before the
// fetch returns.
let overlayOpenSeq = 0;

// Cached cold-column telemetry response for the current overlay open.
// Used so dense waypoints arriving after the telemetry response can
// still be enriched with gear/brake/steering/blinker/accel/AP without
// a second fetch. Cleared on every openVideoOverlay and closeVideoOverlay.
let overlayTelemetryCache = null;

function updateOverlayTelemetry(wp) {
    const coords = document.getElementById('olCoords');
    if (coords) coords.textContent = `Location ${(wp.lat || 0).toFixed(4)}, ${(wp.lon || 0).toFixed(4)}`;

    const speedVal = document.getElementById('olSpeedVal');
    if (speedVal) speedVal.textContent = formatDisplaySpeed(wp.speed_mps || 0);
    const speedUnit = document.getElementById('olSpeedUnit');
    if (speedUnit) speedUnit.textContent = speedUnitLabel();

    const gear = document.getElementById('olGear');
    let gearLetter = 'P';
    if (gear) {
        const g = (wp.gear || 'PARK').replace('GEAR_', '');
        gearLetter = g.charAt(0);
        gear.textContent = gearLetter;
    }

    const wheel = document.getElementById('olWheel');
    if (wheel) wheel.style.setProperty('--oh-wheel', `${wp.steering_angle || 0}deg`);

    // Throttle/brake fall back to longitudinal accelerometer when Tesla
    // didn't record driver pedal input — which is most of the time on
    // AP/FSD, where the motor handles both regen-braking and torque
    // and the pedal fields stay 0. accel_x in m/s² (Tesla axis: + =
    // forward thrust, − = deceleration); a full bar maps to ±3 m/s².
    const accelX = (typeof wp.acceleration_x === 'number') ? wp.acceleration_x : null;

    const brake = document.getElementById('olBrake');
    if (brake) {
        let pct;
        if (wp.brake_applied) {
            pct = 100;
        } else if (accelX != null && accelX < -0.3) {
            pct = Math.min(100, (-accelX) * 33);
        } else {
            pct = 0;
        }
        brake.style.setProperty('--oh-fill', `${pct}%`);
    }

    const throttle = document.getElementById('olThrottle');
    if (throttle) {
        let pct;
        if (wp.accelerator_pedal_position != null && wp.accelerator_pedal_position > 0.01) {
            let v = wp.accelerator_pedal_position;
            if (v <= 1.2) v *= 100;
            pct = Math.max(0, Math.min(100, v));
        } else if (accelX != null && accelX > 0.3) {
            pct = Math.min(100, accelX * 33);
        } else {
            pct = 0;
        }
        throttle.style.setProperty('--oh-fill', `${pct}%`);
    }

    const bL = document.getElementById('olBlinkerL');
    const bR = document.getElementById('olBlinkerR');
    if (bL) bL.classList.toggle('active', !!wp.blinker_on_left);
    if (bR) bR.classList.toggle('active', !!wp.blinker_on_right);

    const ap = document.getElementById('olAP2');
    if (ap) {
        const state = wp.autopilot_state || 'NONE';
        // Suppress AP indicator when parked — Tesla's autopilotState
        // can read "SELF_DRIVING" for a couple of seconds after the
        // driver shifts to P, which looks wrong in the HUD.
        const engaged = ['SELF_DRIVING', 'AUTOSTEER'].includes(state) && gearLetter !== 'P';
        ap.textContent = engaged ? state.replace('_', ' ') : '';
        ap.classList.toggle('active', engaged);
    }
}

// Show a loading spinner inside the overlay stage while the video
// element is buffering. Cleared on `playing` or `error`.
function setOverlayLoading(isLoading, label) {
    const stage = document.querySelector('#videoOverlay .video-overlay-stage');
    if (!stage) return;
    let spinner = stage.querySelector('.video-overlay-spinner');
    if (isLoading) {
        if (!spinner) {
            spinner = document.createElement('div');
            spinner.className = 'video-overlay-spinner';
            spinner.innerHTML = '<div class="vol-spinner-ring" aria-label="Loading video"></div>' +
                '<div class="vol-spinner-label" style="color:#fff;margin-top:8px;font-size:13px;text-align:center;text-shadow:0 1px 2px rgba(0,0,0,0.8);"></div>';
            stage.appendChild(spinner);
        }
        spinner.style.display = '';
        const lbl = spinner.querySelector('.vol-spinner-label');
        if (lbl) lbl.textContent = label || '';
    } else if (spinner) {
        spinner.remove();
    }
}

function wireVideoLoadingIndicator(video) {
    setOverlayLoading(true);
    const clear = () => setOverlayLoading(false);
    const show = () => setOverlayLoading(true);
    video.addEventListener('playing', clear);
    video.addEventListener('canplay', clear);
    video.addEventListener('error', clear, { once: true });
    video.addEventListener('waiting', show);
    video.addEventListener('stalled', show);
}

// Prefer the front camera variant of a clip when one is available.
// The disambiguation flow picks the geographically-nearest waypoint,
// which may reference any camera angle (left_repeater, back, etc.);
// the user almost always wants front first and can switch from there.
function preferFrontVariant(waypoint, allWaypoints) {
    if (!waypoint || !waypoint.video_path) return waypoint;
    // Standalone single-file clips (e.g. a Sentry/Saved grid-view
    // "event.mp4") have no per-camera variants; never rewrite them to a
    // "-front.mp4" path that doesn't exist on disk.
    if (!isMultiCameraClip(waypoint.video_path)) return waypoint;
    const base = getBasePath(waypoint.video_path);
    if (waypoint.video_path.endsWith(`${base.split('/').pop()}-front.mp4`) ||
        waypoint.video_path.endsWith('-front.mp4')) {
        return waypoint;
    }
    const frontMatch = (allWaypoints || []).find(w =>
        w && w.video_path && getBasePath(w.video_path) === base && w.video_path.endsWith('-front.mp4'));
    if (frontMatch) {
        return { ...waypoint, video_path: frontMatch.video_path };
    }
    // Synthesise the front path even if no waypoint references it;
    // the video stream endpoint will 404 cleanly if the file is missing
    // and the existing error handler shows the right toast.
    return { ...waypoint, video_path: `${base}-front.mp4` };
}

// Merge cold-column telemetry (gear/steering/brake/blinker/accel/AP)
// from the /api/trip/<id>/telemetry response into the supplied
// waypoint refs by matching on wp.id. Only fills fields the caller
// hasn't already populated, so a partial waypoint shape from any
// endpoint (route, day-routes, waypoints-for-clip) becomes complete
// without clobbering already-known values.
function _mergeColdTelemetry(waypoints, telem) {
    if (!telem || !Array.isArray(waypoints)) return;
    for (const wp of waypoints) {
        if (!wp || wp.id == null) continue;
        const t = telem[String(wp.id)];
        if (!t) continue;
        if (wp.acceleration_x === undefined) wp.acceleration_x = t.acceleration_x;
        if (wp.acceleration_y === undefined) wp.acceleration_y = t.acceleration_y;
        if (wp.acceleration_z === undefined) wp.acceleration_z = t.acceleration_z;
        if (wp.gear === undefined) wp.gear = t.gear;
        if (wp.steering_angle === undefined) wp.steering_angle = t.steering_angle;
        if (wp.brake_applied === undefined) wp.brake_applied = t.brake_applied;
        if (wp.blinker_on_left === undefined) wp.blinker_on_left = t.blinker_on_left;
        if (wp.blinker_on_right === undefined) wp.blinker_on_right = t.blinker_on_right;
        if (wp.autopilot_state === undefined) wp.autopilot_state = t.autopilot_state;
    }
}

// Replace any waypoints in overlayWaypoints that share the supplied
// clip's base path with the dense set returned by
// /api/waypoints-for-clip. The map polyline endpoints simplify
// waypoints with RDP for rendering bandwidth (often <10 points per
// clip), which is fatal for HUD interpolation — once video.currentTime
// passes the last simplified sample the HUD freezes on stale values.
// The dense set carries every indexed SEI sample (~1 Hz on Tesla
// footage, ~60 per minute) so the HUD stays current to the end.
function _replaceClipWaypointsWithDense(basePath, denseWps) {
    if (!basePath || !Array.isArray(denseWps) || denseWps.length === 0) return;
    overlayWaypoints = overlayWaypoints.filter(w =>
        !w || !w.video_path || getBasePath(w.video_path) !== basePath
    );
    overlayWaypoints.push(...denseWps);
    overlayWaypoints.sort((a, b) => {
        const ta = a && a.timestamp ? Date.parse(a.timestamp) : 0;
        const tb = b && b.timestamp ? Date.parse(b.timestamp) : 0;
        return ta - tb;
    });
    // Invalidate the per-clip waypoint cache in onOverlayTimeUpdate
    // so the next tick rebuilds it from the dense set.
    onOverlayTimeUpdate._cachedBasePath = null;
}

function openVideoOverlay(waypoint, clickPoint, routeWps, tripId) {
    closeDisambigPopup();
    closeVideoOverlay();

    overlayWaypoints = routeWps || [];

    // Operator preference: always open on the front camera first.
    waypoint = preferFrontVariant(waypoint, overlayWaypoints);

    // Issue #184 Wave 3 — Phase D + HUD-freeze fix. Two parallel fetches:
    //
    //   1. /api/trip/<id>/telemetry → cold cols (gear, steering,
    //      brake, blinker, accel, AP) keyed by waypoint id.
    //   2. /api/waypoints-for-clip?path=<videoPath> → DENSE waypoints
    //      for THIS clip. The map polyline endpoints simplify with RDP
    //      down to a handful of waypoints per clip; the HUD needs every
    //      SEI sample (~1 Hz) to keep interpolating past the last
    //      simplified point. Without this, video.currentTime > last
    //      simplified t froze the HUD on stale values for the rest of
    //      the clip (e.g. showing 16 mph while the car was at 41 mph).
    //
    // We cache whichever response arrives first so the second one can
    // re-merge against it — the two are order-independent.
    const opSeq = ++overlayOpenSeq;
    overlayTelemetryCache = null;
    if (tripId && Array.isArray(overlayWaypoints)) {
        fetch(BOOTSTRAP.api.trip_telemetry_template.replace('__TRIP_ID__', encodeURIComponent(String(tripId))))
            .then(r => (r && r.ok) ? r.json() : null)
            .then(data => {
                if (!data || !data.telemetry) return;
                if (opSeq !== overlayOpenSeq) return;
                overlayTelemetryCache = data.telemetry;
                _mergeColdTelemetry(overlayWaypoints, overlayTelemetryCache);
                if (typeof onOverlayTimeUpdate === 'function') {
                    try { onOverlayTimeUpdate(); } catch (e) { /* HUD optional */ }
                }
            })
            .catch(() => { /* HUD already falls back to neutral defaults */ });
    }
    if (waypoint && waypoint.video_path) {
        const denseBase = getBasePath(waypoint.video_path);
        fetch(`${BOOTSTRAP.api.waypoints_for_clip}?path=${encodeURIComponent(waypoint.video_path)}`)
            .then(r => (r && r.ok) ? r.json() : null)
            .then(data => {
                if (!data || !Array.isArray(data.waypoints) || data.waypoints.length === 0) return;
                if (opSeq !== overlayOpenSeq) return;
                _replaceClipWaypointsWithDense(denseBase, data.waypoints);
                // If trip-telemetry already arrived, re-merge against the
                // freshly inserted dense waypoints (whose cold cols are
                // still undefined).
                if (overlayTelemetryCache) {
                    _mergeColdTelemetry(overlayWaypoints, overlayTelemetryCache);
                }
                if (typeof onOverlayTimeUpdate === 'function') {
                    try { onOverlayTimeUpdate(); } catch (e) { /* HUD optional */ }
                }
            })
            .catch(() => { /* HUD already falls back to simplified waypoints */ });
    }

    const filename = waypoint.video_path.split('/').pop();
    overlayCurrentAngle = CAMERA_ANGLES.find(a => filename.includes('-' + a + '.')) || 'front';
    overlayBasePath = getBasePath(waypoint.video_path);

    // Build clip list from overlayWaypoints (set by route click handler)
    overlayClips = [];
    const seen = new Set();
    overlayWaypoints.forEach(wp => {
        if (!wp.video_path) return;
        const bp = getBasePath(wp.video_path);
        if (!seen.has(bp)) {
            seen.add(bp);
            overlayClips.push({ basePath: bp, firstWaypoint: wp });
        }
    });
    overlayClipIdx = Math.max(0, overlayClips.findIndex(c => c.basePath === overlayBasePath));

    // Position overlay
    const mapEl = document.getElementById('map');
    const mapRect = mapEl.getBoundingClientRect();
    let left = mapRect.left + clickPoint.x + 15;
    let top = mapRect.top + clickPoint.y - 100;
    if (left + 490 > window.innerWidth) left = window.innerWidth - 500;
    if (top + 350 > window.innerHeight) top = window.innerHeight - 360;
    if (left < 10) left = 10;
    if (top < 10) top = 10;

    const overlay = document.createElement('div');
    overlay.className = 'video-overlay';
    overlay.id = 'videoOverlay';
    overlay.style.left = left + 'px';
    overlay.style.top = top + 'px';

    // Single-file clips (e.g. a Sentry/Saved grid-view "event.mp4") have no
    // per-camera variants, so there is nothing to switch between — omit the
    // camera switcher entirely rather than render dead buttons that 404.
    let camSwitcherHtml = '';
    if (isMultiCameraClip(waypoint.video_path)) {
        const cameraBtns = CAMERA_ANGLES.map(a => {
            const active = a === overlayCurrentAngle ? ' active' : '';
            const lbl = CAMERA_LABELS[a];
            const icon = CAMERA_ICONS[a];
            return `<button class="cam-btn${active}" data-angle="${a}" title="${lbl}" aria-label="${lbl}">`
                 + `<svg class="cam-icon"><use href="${LUCIDE_SPRITE}#${icon}"></use></svg>`
                 + `<span class="cam-label">${lbl}</span></button>`;
        }).join('');
        camSwitcherHtml = `<div class="cam-switcher" id="camSwitcher">${cameraBtns}</div>`;
    }

    overlay.innerHTML = `
        <div class="video-overlay-header">
            <span id="overlayTitle"></span>
            <button class="close-btn" onclick="closeVideoOverlay()" aria-label="Close video overlay">${ICON_CLOSE}</button>
        </div>
        ${camSwitcherHtml}
        <div class="video-overlay-stage">
            <video id="overlayVideo" controls autoplay muted controlslist="nofullscreen" disablepictureinpicture></video>
            <div class="overlay-hud" id="overlayHud">
                <div class="oh-gear" id="olGear">P</div>
                <div class="oh-pedal oh-brake" id="olBrake"><span class="oh-fill"><i></i></span><span class="oh-lbl">B</span></div>
                <span class="oh-blinker left" id="olBlinkerL">◀</span>
                <div class="oh-speed"><span class="oh-speed-val" id="olSpeedVal">0</span><span class="oh-speed-unit" id="olSpeedUnit">${speedUnitLabel()}</span></div>
                <span class="oh-blinker right" id="olBlinkerR">▶</span>
                <div class="oh-wheel" id="olWheel"><svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8" stroke="#fff" stroke-width="1.4"/><path d="M6.8 9.8H17.2" stroke="#fff" stroke-width="2" stroke-linecap="round"/><path d="M12 9.8V16.8" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="1.8" stroke="#fff" stroke-width="1.4"/></svg></div>
                <div class="oh-pedal oh-throttle" id="olThrottle"><span class="oh-fill"><i></i></span><span class="oh-lbl">A</span></div>
                <div class="oh-ap" id="olAP2"></div>
            </div>
        </div>
        <div class="video-overlay-info" id="overlayInfo">
            <span id="olCoords">Coordinates -</span>
            <div style="margin-left:auto; display:flex; gap:4px;">
                <button class="nav-btn" onclick="navigateClip(-1)" title="Previous clip" aria-label="Previous clip">${ICON_CHEVRON_LEFT}</button>
                <button class="nav-btn" onclick="navigateClip(1)" title="Next clip" aria-label="Next clip">${ICON_CHEVRON_RIGHT}</button>
                <button class="nav-btn" onclick="overlayDownload()" title="Download ZIP" aria-label="Download ZIP">${ICON_DOWNLOAD}</button>
                ${CLOUD_ARCHIVE_ENABLED ? `<button class="nav-btn nav-btn-cloud" id="archiveNavBtn" onclick="overlayArchive()" title="Archive to Cloud" aria-label="Archive to cloud">${ICON_CLOUD}</button>` : ''}
                <button class="nav-btn" onclick="overlayFullscreen()" title="Fullscreen video" aria-label="Fullscreen video"><svg class="nav-icon"><use href="${LUCIDE_SPRITE}#icon-maximize"></use></svg></button>
                <button class="nav-btn" id="overlayMaximizeBtn" onclick="toggleOverlayMaximize()" title="Maximize" aria-label="Maximize video overlay"><svg class="nav-icon"><use href="${LUCIDE_SPRITE}#icon-maximize-2"></use></svg></button>
                <button class="nav-btn nav-btn-danger" onclick="overlayDelete()" title="Delete" aria-label="Delete event">${ICON_TRASH}</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
    document.getElementById('overlayTitle').textContent = filename;

    // V1 model: stream the video directly for instant playback. The HUD
    // is driven from the worker-indexed `waypoints` table (which already
    // has all SEI cold telemetry — gear, steering_angle, brake_applied,
    // blinker_on_*, autopilot_state, acceleration_*) via overlayWaypoints
    // matched to video.currentTime in onOverlayTimeUpdate. Worker indexes
    // SEI at ~1 Hz (sample_rate=30 on a 30 fps clip), which is the same
    // resolution v1 shipped — no client-side MP4 parsing needed.
    const video = document.getElementById('overlayVideo');
    const streamUrl = BOOTSTRAP.view.video_stream_template.replace('__PATH__', waypoint.video_path);
    video.src = streamUrl;
    wireVideoLoadingIndicator(video);

    // Seek to the event time once the browser has enough metadata.
    video.addEventListener('loadedmetadata', function onLoad() {
        const seek = Math.max(0, (waypoint.frame_offset || 0) / 36 - 2);
        if (seek > 0 && seek < video.duration) {
            video.currentTime = seek;
        }
        video.removeEventListener('loadedmetadata', onLoad);
    });

    // Handle video load error — file may no longer exist on disk, OR the
    // browser aborted/timed-out a Range request mid-stream (common on
    // flaky Wi-Fi to the Pi). We try a HEAD probe to disambiguate; if the
    // server can still serve the file we silently reassign video.src once
    // before bothering the operator with a toast — most transient stream
    // errors recover cleanly on a fresh request.
    //
    // NOTE: check the raw src ATTRIBUTE, not the .src property. The property
    // resolves an empty/missing attribute against the document base URL
    // (e.g. https://host/mapping), which is non-empty and would slip past a
    // .src-based guard, surfacing a false "video not found" toast when the
    // overlay is closed or the element is detached mid-load.
    let videoErrorRetries = 0;
    const VIDEO_ERROR_MAX_RETRIES = 1;
    function onVideoError() {
        const rawSrc = video.getAttribute('src');
        if (!rawSrc) return;
        // HEAD-probe the URL so we can tell "file truly gone" (404) from
        // a transient load failure (5xx, network blip, abort). Showing
        // "Tesla may have overwritten it" on every error is misleading.
        fetch(rawSrc, { method: 'HEAD', cache: 'no-store' })
            .then(function (resp) {
                if (resp.ok && videoErrorRetries < VIDEO_ERROR_MAX_RETRIES) {
                    // Server can still serve it — the browser just aborted
                    // a Range request mid-stream. Retry silently once by
                    // forcing a fresh load on the same URL.
                    videoErrorRetries++;
                    video.addEventListener('error', onVideoError, { once: true });
                    try { video.pause(); } catch (e) { /* ignore */ }
                    video.removeAttribute('src');
                    video.load();
                    // Small backoff so the failed TCP connection is fully
                    // torn down before we reissue the same Range.
                    setTimeout(function () {
                        if (video.isConnected) {
                            video.src = rawSrc;
                            video.load();
                        }
                    }, 250);
                    return;
                }
                if (typeof showToast !== 'function') return;
                if (resp.status === 404) {
                    showToast('Video file not found — Tesla may have overwritten it.', 'warning');
                } else if (resp.status >= 500) {
                    showToast('Server error loading video (HTTP ' + resp.status + '). Try again.', 'warning');
                } else if (!resp.ok) {
                    showToast('Could not load video (HTTP ' + resp.status + ').', 'warning');
                } else {
                    showToast('Video failed to play. Try clicking again.', 'warning');
                }
            })
            .catch(function () {
                if (typeof showToast === 'function') {
                    showToast('Network error loading video. Check the connection and try again.', 'warning');
                }
            });
    }
    video.addEventListener('error', onVideoError, { once: true });

    // Live telemetry: timeupdate fires when scrubbed/paused; rAF drives
    // smooth interpolation while playing.
    video.addEventListener('timeupdate', onOverlayTimeUpdate);
    video.addEventListener('play', _startHudRaf);
    video.addEventListener('playing', _startHudRaf);
    video.addEventListener('pause', _stopHudRaf);
    video.addEventListener('ended', _stopHudRaf);
    video.addEventListener('seeked', onOverlayTimeUpdate);

    const hud = document.getElementById('overlayHud');
    if (hud) hud.style.display = overlayWaypoints.length > 0 ? '' : 'none';

    // Seed HUD immediately from the clicked waypoint so the user sees
    // sensible numbers without waiting for the first timeupdate event.
    // onOverlayTimeUpdate() refines this in real time as playback moves.
    const coords = document.getElementById('olCoords');
    if (coords) coords.textContent = `Location ${(waypoint.lat || 0).toFixed(4)}, ${(waypoint.lon || 0).toFixed(4)}`;
    try { updateOverlayTelemetry(waypoint); } catch (e) { /* HUD optional */ }

    // Draggable. Use an AbortController so we can remove all document-level
    // listeners in one shot when the overlay closes — previously these
    // accumulated across opens, causing stale closures and a slow leak.
    let isDragging = false, dragX, dragY;
    const header = overlay.querySelector('.video-overlay-header');
    header.style.cursor = 'grab';
    overlayDragController = new AbortController();
    const sig = overlayDragController.signal;
    header.addEventListener('mousedown', (e) => {
        if (overlay.classList.contains('maximized')) return;
        if (e.target.classList.contains('close-btn')) return;
        isDragging = true;
        dragX = e.clientX - overlay.offsetLeft;
        dragY = e.clientY - overlay.offsetTop;
        header.style.cursor = 'grabbing';
    }, { signal: sig });
    document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        overlay.style.left = (e.clientX - dragX) + 'px';
        overlay.style.top = (e.clientY - dragY) + 'px';
    }, { signal: sig });
    document.addEventListener('mouseup', () => {
        if (!isDragging) return;
        isDragging = false;
        if (header && !overlay.classList.contains('maximized')) header.style.cursor = 'grab';
    }, { signal: sig });
}

// Module-level so closeVideoOverlay can abort drag listeners attached
// during openVideoOverlay. Reset on each open/close cycle.
let overlayDragController = null;

const OVERLAY_ICON_MAX = LUCIDE_SPRITE + "#icon-maximize-2";
const OVERLAY_ICON_MIN = LUCIDE_SPRITE + "#icon-minimize-2";

