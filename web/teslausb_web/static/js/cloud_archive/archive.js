export function createArchiveController({ bootstrap, fetchJson, postJson, formatDateTime, notify }) {
    const archiveForm = document.getElementById("cloudArchiveForm");
    const archiveFolder = document.getElementById("cloudArchiveFolder");
    const archiveEvent = document.getElementById("cloudArchiveEvent");
    const archiveRefreshButton = document.getElementById("cloudArchiveRefreshButton");
    const archiveCancelButton = document.getElementById("cloudArchiveCancelButton");
    const archiveTitle = document.getElementById("cloudArchiveTransferTitle");
    const archiveLine = document.getElementById("cloudArchiveTransferLine");
    const archiveBadge = document.getElementById("cloudArchiveTransferBadge");
    const archiveProgressBar = document.getElementById("cloudArchiveProgressBar");
    const archiveProgressMeta = document.getElementById("cloudArchiveProgressMeta");
    const cleanupButton = document.getElementById("cloudArchiveCleanupButton");
    const cleanupStatus = document.getElementById("cloudArchiveCleanupStatus");
    const bandwidthConfigured = document.getElementById("cloudBandwidthConfigured");
    const bandwidthSuggested = document.getElementById("cloudBandwidthSuggested");
    const bandwidthStatusValue = document.getElementById("cloudBandwidthStatusValue");
    const bandwidthStatus = document.getElementById("cloudBandwidthStatus");
    const bandwidthRunButton = document.getElementById("cloudBandwidthRunButton");
    const bandwidthApplyButton = document.getElementById("cloudBandwidthApplyButton");

    function setBusy(button, busy, busyText) {
        if (!(button instanceof HTMLButtonElement)) {
            return;
        }
        if (busy) {
            button.dataset.originalText = button.innerHTML;
            button.disabled = true;
            if (busyText) {
                button.textContent = busyText;
            }
            return;
        }
        button.disabled = false;
        if (button.dataset.originalText) {
            button.innerHTML = button.dataset.originalText;
        }
    }

    function renderArchiveStatus(payload) {
        const progress = payload.progress || null;
        if (!payload.running || !progress) {
            if (archiveTitle) {
                archiveTitle.textContent = "No archive in progress";
            }
            if (archiveLine) {
                archiveLine.textContent = "Start an archive or poll the route to see live rclone progress.";
            }
            if (archiveBadge) {
                archiveBadge.className = "cloud-archive-badge is-info";
                archiveBadge.textContent = "Idle";
            }
            if (archiveProgressBar) {
                archiveProgressBar.style.width = "0%";
            }
            if (archiveProgressMeta) {
                archiveProgressMeta.textContent = "Waiting for archive activity.";
            }
            return;
        }
        if (archiveTitle) {
            archiveTitle.textContent = "Archive transfer in progress";
        }
        if (archiveLine) {
            archiveLine.textContent = progress.summary || progress.raw_line || "Working";
        }
        if (archiveBadge) {
            archiveBadge.className = "cloud-archive-badge is-warning";
            archiveBadge.textContent = "Running";
        }
        if (archiveProgressBar) {
            archiveProgressBar.style.width = `${Math.min(100, Math.max(0, Math.round(progress.percent || 0)))}%`;
        }
        if (archiveProgressMeta) {
            const parts = [progress.transferred, progress.total, progress.speed, progress.eta].filter(Boolean);
            archiveProgressMeta.textContent = parts.length ? parts.join(" · ") : progress.raw_line || "Working";
        }
    }

    async function refreshArchiveStatus() {
        try {
            const payload = await fetchJson(bootstrap.urls.archiveStatus);
            renderArchiveStatus(payload);
        } catch (error) {
            notify(error.message, "warning");
        }
    }

    async function archiveEventNow(event) {
        event.preventDefault();
        const folder = archiveFolder instanceof HTMLSelectElement ? archiveFolder.value : "SentryClips";
        const eventName = archiveEvent instanceof HTMLInputElement ? archiveEvent.value.trim() : "";
        if (!eventName) {
            notify("Enter an event directory before starting an archive.", "warning");
            return;
        }
        try {
            const payload = await postJson(bootstrap.urls.archiveFile, { folder, event: eventName });
            notify(payload.message || "Archive requested.", payload.success ? "success" : "warning");
            await refreshArchiveStatus();
        } catch (error) {
            notify(error.message, "error");
        }
    }

    async function cancelArchive() {
        const message = archiveCancelButton?.dataset.confirm || "Cancel the active archive transfer?";
        if (!window.confirm(message)) {
            return;
        }
        setBusy(archiveCancelButton, true, "Cancelling");
        try {
            const payload = await postJson(bootstrap.urls.archiveCancel, {});
            notify(payload.message || "Archive cancel request sent.", payload.success ? "success" : "warning");
            await refreshArchiveStatus();
        } catch (error) {
            notify(error.message, "warning");
        } finally {
            setBusy(archiveCancelButton, false);
        }
    }

    async function runCleanup() {
        setBusy(cleanupButton, true, "Running");
        try {
            const payload = await postJson(bootstrap.urls.archiveCleanup, {});
            cleanupStatus.textContent = payload.message || "Cleanup request finished.";
            notify(cleanupStatus.textContent, payload.success ? "success" : "warning");
        } catch (error) {
            cleanupStatus.textContent = error.message;
            notify(error.message, "warning");
        } finally {
            setBusy(cleanupButton, false);
        }
    }

    async function refreshBandwidthStatus() {
        try {
            const payload = await fetchJson(bootstrap.urls.bandwidthTestStatus);
            if (bandwidthConfigured) {
                bandwidthConfigured.textContent = `${bootstrap.maxUploadMbps || 0} Mbps`;
            }
            if (bandwidthSuggested) {
                bandwidthSuggested.textContent = payload.recommended_mbps ? `${payload.recommended_mbps} Mbps` : "Unavailable";
            }
            if (bandwidthStatusValue) {
                bandwidthStatusValue.textContent = payload.running ? "Running" : payload.supported ? "Ready" : "Unsupported";
            }
            if (bandwidthStatus) {
                bandwidthStatus.textContent = payload.progress || (payload.supported ? `Last checked ${formatDateTime(new Date().toISOString())}.` : "Bandwidth routes report unsupported in Phase 5.14d.");
            }
        } catch (error) {
            notify(error.message, "warning");
        }
    }

    async function runBandwidthTest() {
        setBusy(bandwidthRunButton, true, "Running");
        try {
            const payload = await postJson(bootstrap.urls.bandwidthTest, {});
            notify(payload.message || "Bandwidth test started.", payload.success ? "success" : "warning");
            await refreshBandwidthStatus();
        } catch (error) {
            notify(error.message, "warning");
        } finally {
            setBusy(bandwidthRunButton, false);
        }
    }

    async function applyBandwidthPreset() {
        setBusy(bandwidthApplyButton, true, "Applying");
        try {
            const payload = await postJson(bootstrap.urls.bandwidthTestApply, {});
            notify(payload.message || "Bandwidth preset applied.", payload.success ? "success" : "warning");
            await refreshBandwidthStatus();
        } catch (error) {
            notify(error.message, "warning");
        } finally {
            setBusy(bandwidthApplyButton, false);
        }
    }

    return {
        init() {
            archiveForm?.addEventListener("submit", (event) => {
                void archiveEventNow(event);
            });
            archiveRefreshButton?.addEventListener("click", () => {
                void refreshArchiveStatus();
            });
            archiveCancelButton?.addEventListener("click", () => {
                void cancelArchive();
            });
            cleanupButton?.addEventListener("click", () => {
                void runCleanup();
            });
            bandwidthRunButton?.addEventListener("click", () => {
                void runBandwidthTest();
            });
            bandwidthApplyButton?.addEventListener("click", () => {
                void applyBandwidthPreset();
            });
            void refreshArchiveStatus();
            void refreshBandwidthStatus();
            window.setInterval(() => {
                void refreshArchiveStatus();
            }, 4000);
        },
    };
}
