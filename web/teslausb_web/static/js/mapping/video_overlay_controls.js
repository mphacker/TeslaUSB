function overlayFullscreen() {
    // OS-level fullscreen of the .video-overlay-stage wrapper, NOT the
    // <video> element directly. The stage contains both the video and
    // the .overlay-hud telemetry overlay, so the HUD stays visible in
    // fullscreen. (Fullscreening the bare <video> drops the HUD out of
    // the fullscreen layer.) The native <video> fullscreen button is
    // hidden via controlslist="nofullscreen" so this button is the
    // single entry point.
    //
    // iOS Safari fallback: webkitEnterFullscreen is only available on
    // <video> elements and iOS hands off to its native player which
    // cannot have HTML overlaid. On iOS the HUD will not be visible in
    // fullscreen — acceptable platform limitation. Use Maximize (the
    // adjacent button) on iOS to get an HUD-visible fill of the
    // browser viewport instead.
    //
    // Phase 4.9 (#101): show a one-time toast on first iOS fullscreen
    // tap so users know the HUD loss is a Safari limitation and that
    // "Maximize" is the HUD-preserving alternative. Persisted via
    // localStorage so the user is nagged at most once per browser.
    const stage = document.querySelector('.video-overlay-stage');
    const v = document.getElementById('overlayVideo');
    if (!v) return;

    if (isIos() && !hasShownIosFullscreenToast()) {
        // Phase 4.9 (#101) — first-tap behaviour on iOS Safari:
        // SHOW THE TOAST AND SKIP FULLSCREEN. If we entered fullscreen
        // immediately the toast would be hidden behind the OS player
        // and the user would never see the explanation. Skipping the
        // fullscreen on the very first tap is acceptable because the
        // user's stated intent is "give me a bigger view" and the
        // toast explicitly recommends Maximize, which delivers that
        // intent without losing the HUD.
        //
        // Subsequent taps (after the localStorage flag is set) proceed
        // to webkitEnterFullscreen normally for users who deliberately
        // want OS fullscreen and accept the HUD loss.
        showIosFullscreenToast();
        markIosFullscreenToastShown();
        return;
    }

    if (stage && stage.requestFullscreen) {
        stage.requestFullscreen().catch(() => {});
    } else if (stage && stage.webkitRequestFullscreen) {
        stage.webkitRequestFullscreen();
    } else if (v.webkitEnterFullscreen) {
        v.webkitEnterFullscreen();
    }
}

// Phase 4.9 (#101) — iOS Safari detection.
// iPad on iOS 13+ identifies its UA as "Macintosh" but exposes the
// touch-event API, so we combine the two signals. Returns true on
// iPhone, iPad, and iPod across all modern iOS versions.
// iOS detection: returns true for any iOS browser (iPhone, iPad,
// iPod, iPad-on-iOS-13+ in desktop-mode masquerade). Apple policy
// requires every iOS browser to use WebKit, so the toast applies
// equally to Safari, Chrome, Firefox, etc. on iOS — the function
// is intentionally named for iOS, not Safari specifically.
function isIos() {
    if (typeof navigator === 'undefined') return false;
    const ua = navigator.userAgent || '';
    if (/iPhone|iPad|iPod/i.test(ua)) return true;
    // iPadOS desktop-mode masquerade: Mac UA + touch support.
    const isMac = /Macintosh/i.test(ua);
    const hasTouch = (typeof navigator.maxTouchPoints === 'number' &&
                      navigator.maxTouchPoints > 1);
    return isMac && hasTouch;
}
// Backwards-compatible alias for any external caller (and a more
// discoverable name from the IDE). Both names point at the same
// function — never diverge.
const isIosSafari = isIos;

const IOS_FULLSCREEN_TOAST_KEY = 'iosFullscreenToastShown';

// In-memory fallback flag for browsers where localStorage throws on
// EVERY access (Safari Private Mode quota=0). Without this the early
// return in overlayFullscreen() would fire on every tap and the user
// could never reach webkitEnterFullscreen — softlocking the button
// despite the toast text saying "tap Fullscreen again to continue."
// Module-scoped so it survives across taps within the same page load
// (the only durability we need — Private Mode doesn't persist
// preferences across reloads anyway).
let _iosFullscreenToastShownInMemory = false;

function hasShownIosFullscreenToast() {
    if (_iosFullscreenToastShownInMemory) return true;
    try {
        return localStorage.getItem(IOS_FULLSCREEN_TOAST_KEY) === '1';
    } catch (e) {
        // Private browsing / disabled storage → fall back to the
        // in-memory flag (which mark…() also sets), so the second
        // tap proceeds to fullscreen normally.
        return false;
    }
}

function markIosFullscreenToastShown() {
    _iosFullscreenToastShownInMemory = true;
    try {
        localStorage.setItem(IOS_FULLSCREEN_TOAST_KEY, '1');
    } catch (e) { /* storage disabled — in-memory flag is enough */ }
}

// Polite, single-line toast pointing iOS users at the Maximize
// button. Uses the existing showToast() infra (info severity) so
// dark/light mode + dismiss timing are inherited. Falls back to a
// console.log if showToast isn't available — never blocks the
// fullscreen action.
function showIosFullscreenToast() {
    const msg = ('iOS Safari hides the HUD in fullscreen. ' +
                 'Tap Maximize for an HUD-visible view, ' +
                 'or tap Fullscreen again to continue without HUD.');
    if (typeof showToast === 'function') {
        showToast(msg, 'info');
    } else {
        // Defensive — base.html owns showToast. If a future refactor
        // moves it, surface a developer-visible warning rather than
        // crashing the fullscreen path.
        try { console.log('TeslaUSB iOS fullscreen toast:', msg); }
        catch (e) {}
    }
}

function updateOverlayMaximizeButton(isMax) {
    const btn = document.getElementById('overlayMaximizeBtn');
    if (!btn) return;
    btn.title = isMax ? 'Restore' : 'Maximize';
    btn.setAttribute('aria-label', isMax ? 'Restore video overlay' : 'Maximize video overlay');
    const use = btn.querySelector('use');
    if (use) use.setAttribute('href', isMax ? OVERLAY_ICON_MIN : OVERLAY_ICON_MAX);
}

function toggleOverlayMaximize() {
    const overlay = document.getElementById('videoOverlay');
    if (!overlay) return;
    const isMax = overlay.classList.toggle('maximized');
    updateOverlayMaximizeButton(isMax);
    // Reset header cursor when leaving maximized so drag indicator returns.
    const header = overlay.querySelector('.video-overlay-header');
    if (header) header.style.cursor = isMax ? 'default' : 'grab';
}

// Single global ESC handler: restores from maximized state. Attached once
// at module load — does NOT accumulate per overlay open. Does not close
// the overlay on ESC (the X button is the explicit close affordance) and
// does not interfere with the browser's native ESC-exits-fullscreen
// behavior because that only fires while the video is in OS fullscreen.
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const overlay = document.getElementById('videoOverlay');
    if (!overlay || !overlay.classList.contains('maximized')) return;
    overlay.classList.remove('maximized');
    updateOverlayMaximizeButton(false);
    const header = overlay.querySelector('.video-overlay-header');
    if (header) header.style.cursor = 'grab';
});

const VIDEO_PANEL_MOBILE_QUERY = '(max-width: 767px)';
const VIDEO_PANEL_FOCUSABLE = [
    'button:not([disabled])',
    'select:not([disabled])',
    'a[href]',
    'video[controls]',
    '[tabindex]:not([tabindex="-1"])',
].join(',');
let videoPanelPreviousFocus = null;

function isVideoPanelMobile() {
    return window.matchMedia && window.matchMedia(VIDEO_PANEL_MOBILE_QUERY).matches;
}

function videoPanelIsOpen(panel) {
    return panel && panel.classList.contains('open');
}

function syncVideoPanelAccessibility() {
    const panel = document.getElementById('videoPanel');
    const button = document.getElementById('btnVideos');
    if (!panel || !button) return;
    const isOpen = videoPanelIsOpen(panel);
    const isMobileOpen = isOpen && isVideoPanelMobile();
    button.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    panel.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
    panel.setAttribute('aria-modal', isMobileOpen ? 'true' : 'false');
    document.body.classList.toggle('video-panel-mobile-open', isMobileOpen);
    manageVideoPanelFocus(panel, isOpen);
}

function manageVideoPanelFocus(panel, isOpen) {
    if (!isVideoPanelMobile()) return;
    if (isOpen) {
        videoPanelPreviousFocus = document.activeElement;
        const closeButton = panel.querySelector('.video-panel-header .close-btn');
        if (closeButton) closeButton.focus({ preventScroll: true });
        return;
    }
    if (videoPanelPreviousFocus && document.contains(videoPanelPreviousFocus)) {
        videoPanelPreviousFocus.focus({ preventScroll: true });
    }
    videoPanelPreviousFocus = null;
}

function videoPanelFocusableElements(panel) {
    return Array.from(panel.querySelectorAll(VIDEO_PANEL_FOCUSABLE))
        .filter(el => el.offsetParent !== null || el === document.activeElement);
}

function handleVideoPanelKeydown(e) {
    const panel = document.getElementById('videoPanel');
    if (!isVideoPanelMobile() || !videoPanelIsOpen(panel)) return;
    if (e.key === 'Escape' && typeof toggleVideoPanel === 'function') {
        e.preventDefault();
        toggleVideoPanel();
        return;
    }
    if (e.key !== 'Tab') return;
    const focusable = videoPanelFocusableElements(panel);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && (!panel.contains(document.activeElement) || document.activeElement === first)) {
        e.preventDefault();
        last.focus({ preventScroll: true });
    } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus({ preventScroll: true });
    }
}

function installVideoPanelAccessibility() {
    const panel = document.getElementById('videoPanel');
    const button = document.getElementById('btnVideos');
    if (!panel || !button) return;
    button.setAttribute('aria-controls', 'videoPanel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'false');
    new MutationObserver(syncVideoPanelAccessibility)
        .observe(panel, { attributes: true, attributeFilter: ['class'] });
    document.addEventListener('keydown', handleVideoPanelKeydown);
    if (window.matchMedia) {
        const mobileQuery = window.matchMedia(VIDEO_PANEL_MOBILE_QUERY);
        if (mobileQuery.addEventListener) {
            mobileQuery.addEventListener('change', syncVideoPanelAccessibility);
        } else if (mobileQuery.addListener) {
            mobileQuery.addListener(syncVideoPanelAccessibility);
        }
    }
    syncVideoPanelAccessibility();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installVideoPanelAccessibility, { once: true });
} else {
    installVideoPanelAccessibility();
}

function _interpolateWaypoint(a, b, alpha) {
    // Blend continuous fields linearly between two adjacent waypoints.
    // Stepwise fields (gear/blinker/AP) take the value of the earlier
    // waypoint so the HUD reads "the state the car was in at this moment"
    // rather than racing ahead to the next transition.
    const lerp = (x, y, t) => {
        const xn = (typeof x === 'number') ? x : null;
        const yn = (typeof y === 'number') ? y : null;
        if (xn == null && yn == null) return null;
        if (xn == null) return yn;
        if (yn == null) return xn;
        return xn + (yn - xn) * t;
    };
    return {
        lat: lerp(a.lat, b.lat, alpha),
        lon: lerp(a.lon, b.lon, alpha),
        speed_mps: lerp(a.speed_mps, b.speed_mps, alpha),
        steering_angle: lerp(a.steering_angle, b.steering_angle, alpha),
        acceleration_x: lerp(a.acceleration_x, b.acceleration_x, alpha),
        acceleration_y: lerp(a.acceleration_y, b.acceleration_y, alpha),
        acceleration_z: lerp(a.acceleration_z, b.acceleration_z, alpha),
        accelerator_pedal_position: lerp(a.accelerator_pedal_position, b.accelerator_pedal_position, alpha),
        gear: a.gear,
        brake_applied: a.brake_applied,
        blinker_on_left: a.blinker_on_left,
        blinker_on_right: a.blinker_on_right,
        autopilot_state: a.autopilot_state,
        video_path: a.video_path,
    };
}

function onOverlayTimeUpdate() {
    const video = document.getElementById('overlayVideo');
    if (!video) return;
    if (!Array.isArray(overlayWaypoints) || overlayWaypoints.length === 0) return;

    // Filter to this clip's waypoints (sorted by frame_offset, ascending).
    // V1 parity: match by *base path* (camera-angle-agnostic) because
    // waypoints in the DB are tagged on -front.mp4 only; switching to
    // -back.mp4 etc. should still drive the HUD from the front-camera
    // SEI for the same clip.
    //
    // IMPORTANT: cache references to the original wp objects (NOT copies)
    // because openVideoOverlay's lazy /api/trip/<id>/telemetry fetch
    // merges cold cols (gear/wheel/brake/blinker/AP/accel) into those
    // originals after this cache may already be built. We keep a parallel
    // _ts array for the binary search so the lookup stays cheap.
    if (onOverlayTimeUpdate._cachedBasePath !== overlayBasePath) {
        const refs = overlayWaypoints.filter(w =>
            w && w.video_path && getBasePath(w.video_path) === overlayBasePath
        );
        refs.sort((x, y) => ((x.frame_offset || 0) - (y.frame_offset || 0)));
        const ts = refs.map(w => (w.frame_offset || 0) / 36);
        onOverlayTimeUpdate._cachedBasePath = overlayBasePath;
        onOverlayTimeUpdate._cachedClipWps = refs;
        onOverlayTimeUpdate._cachedClipTs = ts;
    }
    const clipWps = onOverlayTimeUpdate._cachedClipWps;
    const clipTs = onOverlayTimeUpdate._cachedClipTs;
    if (!clipWps || !clipWps.length) return;

    const t = video.currentTime;
    // Binary-search for the segment containing t.
    let lo = 0, hi = clipWps.length - 1;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (clipTs[mid] <= t) lo = mid; else hi = mid - 1;
    }
    const a = clipWps[lo];
    const b = clipWps[lo + 1];
    const ta = clipTs[lo];
    const tb = (lo + 1 < clipTs.length) ? clipTs[lo + 1] : ta;
    if (!b || tb === ta) {
        updateOverlayTelemetry(a);
        return;
    }
    let alpha = (t - ta) / (tb - ta);
    if (alpha < 0) alpha = 0; else if (alpha > 1) alpha = 1;
    updateOverlayTelemetry(_interpolateWaypoint(a, b, alpha));
}

// Run the HUD loop on requestAnimationFrame while a video is playing.
// `timeupdate` fires only 4–60 Hz depending on browser; rAF gives us
// 60 Hz so the interpolated wheel/speed/throttle look buttery rather
// than stepped on lower-cadence builds.
let _hudRafHandle = null;
function _startHudRaf() {
    if (_hudRafHandle != null) return;
    const tick = () => {
        const video = document.getElementById('overlayVideo');
        if (!video) { _hudRafHandle = null; return; }
        onOverlayTimeUpdate();
        _hudRafHandle = requestAnimationFrame(tick);
    };
    _hudRafHandle = requestAnimationFrame(tick);
}
function _stopHudRaf() {
    if (_hudRafHandle != null) { cancelAnimationFrame(_hudRafHandle); _hudRafHandle = null; }
}

function switchCamera(angle) {
    const video = document.getElementById('overlayVideo');
    if (!video) return;

    const currentTime = video.currentTime;
    const wasPaused = video.paused;
    overlayCurrentAngle = angle;

    const newPath = `${overlayBasePath}-${angle}.mp4`;
    const newFilename = newPath.split('/').pop();

    document.querySelectorAll('#camSwitcher .cam-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.angle === angle);
    });

    const title = document.getElementById('overlayTitle');
    if (title) title.textContent = newFilename;

    video.src = BOOTSTRAP.view.video_stream_template.replace('__PATH__', newPath);
    wireVideoLoadingIndicator(video);
    video.addEventListener('loadedmetadata', function onLoad() {
        if (currentTime > 0 && currentTime < video.duration) {
            video.currentTime = currentTime;
        }
        if (!wasPaused) video.play().catch(() => {});
        video.removeEventListener('loadedmetadata', onLoad);
    }, { once: true });
}

function navigateClip(direction) {
    const newIdx = overlayClipIdx + direction;
    if (newIdx < 0 || newIdx >= overlayClips.length) return;

    overlayClipIdx = newIdx;
    const clip = overlayClips[newIdx];
    overlayBasePath = clip.basePath;

    const newPath = `${clip.basePath}-${overlayCurrentAngle}.mp4`;
    const newFilename = newPath.split('/').pop();

    const title = document.getElementById('overlayTitle');
    if (title) title.textContent = newFilename;

    const video = document.getElementById('overlayVideo');
    if (!video) return;

    wireVideoLoadingIndicator(video);

    // Stream the new clip directly. The HUD continues to drive from
    // overlayWaypoints (trip-wide) matched by video_path in
    // onOverlayTimeUpdate, so it updates as soon as playback reaches a
    // waypoint for this clip.
    video.src = BOOTSTRAP.view.video_stream_template.replace('__PATH__', newPath);
    video.addEventListener('loadedmetadata', function onLoad() {
        video.play().catch(() => {});
        video.removeEventListener('loadedmetadata', onLoad);
    }, { once: true });

    if (clip.firstWaypoint) {
        updateOverlayTelemetry(clip.firstWaypoint);
    }

    // Same dense-waypoint fetch as openVideoOverlay — the trip-wide
    // simplified waypoints in overlayWaypoints are also sparse for
    // adjacent clips, so the HUD would freeze on the last simplified
    // sample once playback ran past it.
    const frontPath = `${clip.basePath}-front.mp4`;
    const opSeq = overlayOpenSeq;
    fetch(`${BOOTSTRAP.api.waypoints_for_clip}?path=${encodeURIComponent(frontPath)}`)
        .then(r => (r && r.ok) ? r.json() : null)
        .then(data => {
            if (!data || !Array.isArray(data.waypoints) || data.waypoints.length === 0) return;
            if (opSeq !== overlayOpenSeq) return;
            _replaceClipWaypointsWithDense(clip.basePath, data.waypoints);
            if (overlayTelemetryCache) {
                _mergeColdTelemetry(overlayWaypoints, overlayTelemetryCache);
            }
            if (typeof onOverlayTimeUpdate === 'function') {
                try { onOverlayTimeUpdate(); } catch (e) { /* HUD optional */ }
            }
        })
        .catch(() => { /* HUD already falls back to simplified waypoints */ });
}

async function openEventVideo(tripId, videoPath, frameOffset, lat, lon) {
    // Close the Leaflet popup
    map.closePopup();

    const wp = {
        video_path: videoPath,
        frame_offset: frameOffset,
        speed_mps: 0,
        autopilot_state: 'N/A',
        lat: lat,
        lon: lon,
    };

    // Load trip route waypoints for telemetry + prev/next
    let wps = [wp];
    if (tripId) {
        try {
            const geojson = await loadTripRoute(tripId);
            if (geojson && geojson.properties && geojson.properties.waypoints) {
                wps = geojson.properties.waypoints;
            }
        } catch(e) {}
    }

    // Center the overlay on screen
    openVideoOverlay(wp, { x: window.innerWidth / 3, y: window.innerHeight / 4 }, wps, tripId);
}

// Event delegation for popup video links (avoids inline onclick XSS risk)
document.addEventListener('click', function(e) {
    const link = e.target.closest('.popup-video-link');
    if (link) {
        e.preventDefault();
        openEventVideo(
            parseInt(link.dataset.tripId) || 0,
            link.dataset.videoPath || '',
            parseInt(link.dataset.frameOffset) || 0,
            parseFloat(link.dataset.lat) || 0,
            parseFloat(link.dataset.lon) || 0
        );
        return;
    }
    const camBtn = e.target.closest('.cam-btn');
    if (camBtn && camBtn.dataset.angle) {
        switchCamera(camBtn.dataset.angle);
    }
});

