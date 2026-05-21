export function createSyncControlController({ bootstrap, fetchJson, postJson, formatBytes, formatDateTime, formatDuration, formatRelativeTime, notify, state, toTitleCase }) {
    const syncTitle = document.getElementById("cloudSyncStatusTitle");
    const syncLine = document.getElementById("cloudSyncStatusLine");
    const syncProviderBadge = document.getElementById("cloudSyncProviderBadge");
    const syncWorkerBadge = document.getElementById("cloudSyncWorkerBadge");
    const syncProgressBar = document.getElementById("cloudSyncProgressBar");
    const syncCurrentFile = document.getElementById("cloudSyncCurrentFile");
    const syncEta = document.getElementById("cloudSyncEta");
    const syncStatusCard = document.getElementById("cloudSyncStatusCard");
    const statSynced = document.getElementById("cloudStatSynced");
    const statPending = document.getElementById("cloudStatPending");
    const statFailed = document.getElementById("cloudStatFailed");
    const statBytes = document.getElementById("cloudStatBytes");
    const baselineHint = document.getElementById("cloudBaselineHint");
    const shadowAgreement = document.getElementById("cloudShadowAgreement");
    const shadowEnqueues = document.getElementById("cloudShadowEnqueues");
    const headerPending = document.getElementById("cloudHeaderPending");
    const syncNowButton = document.getElementById("cloudSyncNowButton");
    const wakeButton = document.getElementById("cloudWakeButton");
    const syncStopButton = document.getElementById("cloudSyncStopButton");
    const refreshButton = document.getElementById("cloudSyncRefreshButton");
    const resetStatsButton = document.getElementById("cloudResetStatsButton");
    const historyLimit = document.getElementById("cloudHistoryLimit");
    const historyRefreshButton = document.getElementById("cloudHistoryRefreshButton");
    const historyTableBody = document.getElementById("cloudHistoryTableBody");
    const historyCards = document.getElementById("cloudHistoryCards");
    const historyEmpty = document.getElementById("cloudHistoryEmpty");
    const historyShell = document.getElementById("cloudHistoryShell");

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

    function updateStats(stats, shadow) {
        state.syncStats = stats || {};
        if (statSynced) {
            statSynced.textContent = String(stats.total_synced || 0);
        }
        if (statPending) {
            statPending.textContent = String(stats.total_pending || 0);
        }
        if (statFailed) {
            statFailed.textContent = String(stats.total_failed || 0);
        }
        if (statBytes) {
            statBytes.textContent = formatBytes(stats.total_bytes || 0);
        }
        if (baselineHint) {
            baselineHint.textContent = stats.stats_baseline_at
                ? `Counters are relative to ${String(stats.stats_baseline_at).slice(0, 10)}.`
                : "Counters cover the full cloud archive history.";
        }
        if (shadowAgreement) {
            shadowAgreement.textContent = String(shadow?.agreement_count || 0);
        }
        if (shadowEnqueues) {
            shadowEnqueues.textContent = String(shadow?.pipeline_enqueue_count || 0);
        }
        if (headerPending?.lastElementChild) {
            headerPending.lastElementChild.textContent = `${stats.total_pending || 0} pending`;
        }
    }

    function updateStatus(status) {
        state.syncStatus = status || {};
        const percent = status.total_bytes > 0
            ? Math.min(100, Math.round(((status.bytes_transferred || 0) / status.total_bytes) * 100))
            : status.files_total > 0
                ? Math.min(100, Math.round(((status.files_done || 0) / status.files_total) * 100))
                : 0;
        if (syncProgressBar) {
            syncProgressBar.style.width = `${percent}%`;
        }
        if (syncProviderBadge) {
            syncProviderBadge.textContent = state.provider ? toTitleCase(state.provider) : "No provider";
        }
        if (status.running) {
            syncStatusCard?.classList.add("is-running");
            syncStatusCard?.classList.remove("is-error");
            if (syncTitle) {
                syncTitle.textContent = `Syncing to ${state.provider ? toTitleCase(state.provider) : "cloud"}`;
            }
            if (syncLine) {
                syncLine.textContent = `${status.files_done || 0} of ${status.files_total || 0} files · ${formatBytes(status.bytes_transferred || 0)} transferred`;
            }
            if (syncWorkerBadge) {
                syncWorkerBadge.className = "cloud-archive-badge is-warning";
                syncWorkerBadge.textContent = "Running";
            }
        } else if (status.error) {
            syncStatusCard?.classList.remove("is-running");
            syncStatusCard?.classList.add("is-error");
            if (syncTitle) {
                syncTitle.textContent = "Sync error";
            }
            if (syncLine) {
                syncLine.textContent = status.error;
            }
            if (syncWorkerBadge) {
                syncWorkerBadge.className = "cloud-archive-badge is-danger";
                syncWorkerBadge.textContent = "Attention";
            }
        } else {
            syncStatusCard?.classList.remove("is-running", "is-error");
            if (syncTitle) {
                syncTitle.textContent = state.providerConnected ? "Ready to upload" : "Choose a provider";
            }
            if (syncLine) {
                syncLine.textContent = status.last_run ? `Last sync ${formatRelativeTime(status.last_run)}` : "No sync sessions yet";
            }
            if (syncWorkerBadge) {
                syncWorkerBadge.className = "cloud-archive-badge is-success";
                syncWorkerBadge.textContent = "Idle";
            }
        }
        if (syncCurrentFile) {
            syncCurrentFile.textContent = status.current_file || "No current file";
        }
        if (syncEta) {
            syncEta.textContent = status.eta_seconds
                ? `${status.eta_seconds}s remaining`
                : status.throughput_bps
                    ? `${formatBytes(status.throughput_bps)}/s`
                    : "Waiting";
        }
    }

    function renderHistory(history) {
        state.syncHistory = Array.isArray(history) ? history : [];
        if (historyTableBody) {
            historyTableBody.innerHTML = state.syncHistory.map((row) => {
                const duration = row.started_at && row.ended_at ? formatDuration(row.started_at, row.ended_at) : "—";
                const tone = row.status === "completed"
                    ? "is-success"
                    : ["failed", "dead_letter"].includes(row.status)
                        ? "is-danger"
                        : ["running", "interrupted"].includes(row.status)
                            ? "is-warning"
                            : "is-info";
                return `
                    <tr>
                        <td>${formatDateTime(row.started_at)}</td>
                        <td>${row.trigger || "auto"}</td>
                        <td>${row.files_synced || 0}</td>
                        <td>${formatBytes(row.bytes_transferred || 0)}</td>
                        <td>${duration}</td>
                        <td><span class="cloud-archive-history-badge ${tone}">${row.status || "unknown"}</span></td>
                    </tr>`;
            }).join("");
        }
        if (historyCards) {
            historyCards.innerHTML = state.syncHistory.map((row) => `
                <article class="cloud-archive-history-card">
                    <div class="cloud-archive-panel-head">
                        <div>
                            <h3 class="cloud-archive-list-title">${formatDateTime(row.started_at)}</h3>
                            <p class="cloud-archive-panel-copy">${row.trigger || "auto"} · ${row.files_synced || 0} files · ${formatBytes(row.bytes_transferred || 0)}</p>
                        </div>
                        <span class="cloud-archive-history-badge ${row.status === "completed" ? "is-success" : row.status === "failed" ? "is-danger" : "is-info"}">${row.status || "unknown"}</span>
                    </div>
                </article>`).join("");
        }
        const hasRows = state.syncHistory.length > 0;
        historyEmpty?.toggleAttribute("hidden", hasRows);
        historyShell?.classList.toggle("is-populated", hasRows);
    }

    async function refreshStatus() {
        try {
            const payload = await fetchJson(bootstrap.urls.status);
            updateStatus(payload.status || {});
            updateStats(payload.stats || {}, payload.shadow || {});
            state.provider = state.provider || bootstrap.provider || "";
        } catch (error) {
            notify(error.message, "warning");
        }
    }

    async function refreshHistory() {
        const limit = historyLimit instanceof HTMLSelectElement ? historyLimit.value : "20";
        setBusy(historyRefreshButton, true, "Loading");
        try {
            const payload = await fetchJson(`${bootstrap.urls.history}?limit=${encodeURIComponent(limit)}`);
            renderHistory(payload.history || []);
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(historyRefreshButton, false);
        }
    }

    async function startSync() {
        setBusy(syncNowButton, true, "Starting");
        try {
            const payload = await postJson(bootstrap.urls.syncNow, {});
            notify(payload.message || "Sync requested.", payload.success ? "success" : "warning");
            await refreshStatus();
            await refreshHistory();
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(syncNowButton, false);
        }
    }

    async function wakeWorker() {
        setBusy(wakeButton, true, "Waking");
        try {
            const payload = await postJson(bootstrap.urls.wake, {});
            notify(`Wake count ${payload.wake_count || 0}.`, "info");
            await refreshStatus();
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(wakeButton, false);
        }
    }

    async function stopSync() {
        const message = syncStopButton?.dataset.confirm || "Stop the active sync?";
        if (!window.confirm(message)) {
            return;
        }
        setBusy(syncStopButton, true, "Stopping");
        try {
            const payload = await postJson(bootstrap.urls.syncStop, {});
            notify(payload.message || "Stop request sent.", payload.success ? "success" : "warning");
            await refreshStatus();
            await refreshHistory();
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(syncStopButton, false);
        }
    }

    async function resetStats() {
        const message = resetStatsButton?.dataset.confirm || "Reset counters?";
        if (!window.confirm(message)) {
            return;
        }
        setBusy(resetStatsButton, true, "Resetting");
        try {
            const payload = await postJson(bootstrap.urls.resetStats, {});
            notify(payload.success ? "Counters reset." : payload.message || "Reset failed.", payload.success ? "success" : "warning");
            await refreshStatus();
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(resetStatsButton, false);
        }
    }

    return {
        init() {
            updateStatus(state.syncStatus);
            updateStats(state.syncStats, { agreement_count: 0, pipeline_enqueue_count: 0 });
            renderHistory(state.syncHistory);
            syncNowButton?.addEventListener("click", () => {
                void startSync();
            });
            wakeButton?.addEventListener("click", () => {
                void wakeWorker();
            });
            syncStopButton?.addEventListener("click", () => {
                void stopSync();
            });
            refreshButton?.addEventListener("click", () => {
                void refreshStatus();
            });
            resetStatsButton?.addEventListener("click", () => {
                void resetStats();
            });
            historyRefreshButton?.addEventListener("click", () => {
                void refreshHistory();
            });
            historyLimit?.addEventListener("change", () => {
                void refreshHistory();
            });
            window.setInterval(() => {
                void refreshStatus();
            }, 10000);
        },
    };
}
