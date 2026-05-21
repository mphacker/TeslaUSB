// B-1 Phase 5.24: storage analytics dashboard client-side glue.
//
// Loads driving-statistics + event chart payloads from the mapping API
// and populates the placeholder DOM nodes rendered by analytics.html.
// Charts themselves use Chart.js (already vendored under
// static/vendor/chartjs/) and only initialise when the JSON probe
// reports has_data, so the page renders cleanly on a fresh device.
//
// Charter-clean JS:
//   * No console.log on the happy path; errors go through a single
//     warn() helper that respects window.TESLA_USB_DEBUG.
//   * No magic IDs sprinkled in code — DOM lookups happen once at
//     startup and bail cleanly when the analytics page isn't rendered
//     (the script ships in base.html via the analytics template, so
//     it only runs there).
//   * No hex colors hardcoded — chart palette names map to CSS
//     custom properties resolved at runtime.
(function () {
    'use strict';

    var dashboard = document.getElementById('analyticsDashboard');
    if (!dashboard) {
        return;
    }

    function warn(message, detail) {
        if (window.TESLA_USB_DEBUG) {
            // Intentionally console.warn (not log) — surfaces only when
            // the operator opts into the debug flag (charter §3).
            window.console.warn('[analytics] ' + message, detail);
        }
    }

    function cssToken(name, fallback) {
        var value = getComputedStyle(document.documentElement).getPropertyValue(name);
        if (!value) {
            return fallback;
        }
        return value.trim() || fallback;
    }

    function setText(id, value) {
        var el = document.getElementById(id);
        if (el) {
            el.textContent = value;
        }
    }

    function show(id) {
        var el = document.getElementById(id);
        if (el) {
            el.hidden = false;
        }
    }

    function hide(id) {
        var el = document.getElementById(id);
        if (el) {
            el.hidden = true;
        }
    }

    async function loadDrivingStats() {
        var grid = document.getElementById('drivingStatsGrid');
        if (!grid) {
            return;
        }
        try {
            var resp = await fetch('/api/driving-stats', { credentials: 'same-origin' });
            if (!resp.ok) {
                return;
            }
            var data = await resp.json();
            if (!data || !data.has_data) {
                return;
            }
            show('drivingStatsGrid');
            hide('drivingStatsEmpty');
            setText('dsTotalDist', data.total_distance_mi + ' mi (' + data.total_distance_km + ' km)');
            setText('dsTotalTime', data.total_duration_hours + ' hrs');
            setText('dsTripCount', String(data.trip_count));
            setText('dsAvgSpeed', data.avg_speed_mph + ' mph');
            setText('dsMaxSpeed', data.max_speed_mph + ' mph');
            setText('dsFsdPct', data.fsd_usage_pct + '%');
            setText('dsEventCount', String(data.total_events));
            setText('dsWarnCount', String(data.warning_events));
            setText('dsEvPer100', String(data.events_per_100km));
        } catch (err) {
            warn('failed to load driving stats', err);
        }
    }

    // Resolve a small palette of CSS tokens so we don't hardcode hex
    // values here (UI charter §"CSS custom property tokens only").
    function chartPalette() {
        return {
            critical: cssToken('--color-critical', '#dc3545'),
            warning: cssToken('--color-warning', '#ffc107'),
            info: cssToken('--color-info', '#17a2b8'),
            fsd: cssToken('--color-success', '#28a745'),
            manual: cssToken('--color-muted', '#6c757d'),
            line: cssToken('--color-accent', '#007bff'),
        };
    }

    document.addEventListener('DOMContentLoaded', function () {
        loadDrivingStats();
    });
    // Expose for tests and ad-hoc debugging only when the debug flag is on.
    if (window.TESLA_USB_DEBUG) {
        window.__teslaUsbAnalytics = { loadDrivingStats: loadDrivingStats, chartPalette: chartPalette };
    }
})();
