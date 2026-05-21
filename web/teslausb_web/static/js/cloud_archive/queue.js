export function createQueueController({ bootstrap, fetchJson, postJson, formatBytes, notify }) {
    const queueForm = document.getElementById("cloudQueueForm");
    const queueFolder = document.getElementById("cloudQueueFolder");
    const queueEvent = document.getElementById("cloudQueueEvent");
    const queuePriority = document.getElementById("cloudQueuePriority");
    const queueStatus = document.getElementById("cloudQueueStatus");
    const queueList = document.getElementById("cloudQueueList");
    const queueRefreshButton = document.getElementById("cloudQueueRefreshButton");
    const queueClearButton = document.getElementById("cloudQueueClearButton");
    const batchInput = document.getElementById("cloudBatchEventsInput");
    const batchCheckButton = document.getElementById("cloudBatchCheckButton");
    const batchResults = document.getElementById("cloudBatchResults");
    const deadLetterStatus = document.getElementById("cloudDeadLetterStatus");
    const deadLetterList = document.getElementById("cloudDeadLetterList");
    const deadLetterRefreshButton = document.getElementById("cloudDeadLetterRefreshButton");
    const deadLetterRetryAllButton = document.getElementById("cloudDeadLetterRetryAllButton");
    const deadLetterDeleteAllButton = document.getElementById("cloudDeadLetterDeleteAllButton");

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

    function queuePayloadToItems(payload) {
        return Array.isArray(payload) ? payload : Array.isArray(payload.queue) ? payload.queue : [];
    }

    function renderQueue(items) {
        if (!queueList) {
            return;
        }
        if (items.length === 0) {
            queueList.innerHTML = `
                <section class="cloud-archive-empty">
                    <h3 class="cloud-archive-empty-title">Queue is empty</h3>
                    <p class="cloud-archive-empty-copy">Add an event above or wait for the worker to discover new clips.</p>
                </section>`;
            return;
        }
        queueList.innerHTML = items.map((item) => `
            <article class="cloud-archive-list-item">
                <div class="cloud-archive-list-copy">
                    <h3 class="cloud-archive-list-name">${item.file_path}</h3>
                    <p class="cloud-archive-list-path">${item.status || "queued"} · ${formatBytes(item.file_size || 0)}${item.retry_count ? ` · retries ${item.retry_count}` : ""}</p>
                </div>
                <button class="cloud-archive-secondary-btn" type="button" aria-label="Remove queued item" data-queue-remove="${encodeURIComponent(item.file_path)}">Remove</button>
            </article>`).join("");
    }

    function renderDeadLetters(entries, count) {
        if (!deadLetterList || !deadLetterStatus) {
            return;
        }
        deadLetterStatus.textContent = `${count || 0} dead-letter entr${count === 1 ? "y" : "ies"} recorded.`;
        if (!entries.length) {
            deadLetterList.innerHTML = `
                <section class="cloud-archive-empty">
                    <h3 class="cloud-archive-empty-title">No dead letters</h3>
                    <p class="cloud-archive-empty-copy">Failed uploads that exhaust retries appear here.</p>
                </section>`;
            return;
        }
        deadLetterList.innerHTML = entries.map((item) => `
            <article class="cloud-archive-list-item">
                <div class="cloud-archive-list-copy">
                    <h3 class="cloud-archive-list-name">${item.file_path}</h3>
                    <p class="cloud-archive-list-path">Retries ${item.retry_count || 0} · ${item.last_error || item.previous_last_error || "No error detail"}</p>
                </div>
                <div class="cloud-archive-actions">
                    <button class="cloud-archive-secondary-btn" type="button" aria-label="Retry dead letter" data-dead-letter-retry="${encodeURIComponent(item.file_path)}">Retry</button>
                    <button class="cloud-archive-secondary-btn" type="button" aria-label="Delete dead letter" data-dead-letter-delete="${encodeURIComponent(item.file_path)}">Delete</button>
                </div>
            </article>`).join("");
    }

    async function refreshQueue() {
        setBusy(queueRefreshButton, true, "Loading");
        try {
            const payload = await fetchJson(bootstrap.urls.queue);
            const items = queuePayloadToItems(payload);
            renderQueue(items);
            if (queueStatus) {
                queueStatus.textContent = items.length ? `${items.length} item(s) queued.` : "Queue contents load after the first refresh.";
            }
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(queueRefreshButton, false);
        }
    }

    async function addQueueItem(event) {
        event.preventDefault();
        const folder = queueFolder instanceof HTMLSelectElement ? queueFolder.value : "SentryClips";
        const eventName = queueEvent instanceof HTMLInputElement ? queueEvent.value.trim() : "";
        const priority = queuePriority instanceof HTMLSelectElement ? queuePriority.value === "true" : false;
        if (!eventName) {
            notify("Enter an event directory before adding it to the queue.", "warning");
            return;
        }
        try {
            const payload = await postJson(bootstrap.urls.queueEvent, { folder, event: eventName, priority });
            notify(payload.message || "Queue updated.", payload.success ? "success" : "warning");
            if (queueEvent instanceof HTMLInputElement) {
                queueEvent.value = "";
            }
            await refreshQueue();
        } catch (error) {
            notify(error.message, "error");
        }
    }

    async function removeQueueItem(filePath) {
        try {
            const payload = await postJson(bootstrap.urls.queueRemove, { file_path: filePath });
            notify(payload.message || "Queue item removed.", payload.success ? "success" : "warning");
            await refreshQueue();
        } catch (error) {
            notify(error.message, "error");
        }
    }

    async function clearQueue() {
        const message = queueClearButton?.dataset.confirm || "Clear the queue?";
        if (!window.confirm(message)) {
            return;
        }
        try {
            const payload = await postJson(bootstrap.urls.queueClear, {});
            notify(payload.message || "Queue cleared.", payload.success ? "success" : "warning");
            await refreshQueue();
        } catch (error) {
            notify(error.message, "error");
        }
    }

    async function checkBatch() {
        const rows = batchInput instanceof HTMLTextAreaElement
            ? batchInput.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
            : [];
        if (!rows.length) {
            notify("Paste at least one event name for status batch lookup.", "warning");
            return;
        }
        setBusy(batchCheckButton, true, "Checking");
        try {
            const payload = await postJson(bootstrap.urls.syncStatusBatch, { events: rows });
            const statuses = payload.statuses || {};
            batchResults.hidden = false;
            batchResults.innerHTML = `
                <table class="cloud-archive-batch-table">
                    <thead><tr><th>Event</th><th>Status</th></tr></thead>
                    <tbody>${rows.map((row) => `<tr><td>${row}</td><td>${statuses[row] || "unknown"}</td></tr>`).join("")}</tbody>
                </table>`;
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(batchCheckButton, false);
        }
    }

    async function refreshDeadLetters() {
        setBusy(deadLetterRefreshButton, true, "Loading");
        try {
            const payload = await fetchJson(bootstrap.urls.deadLetters);
            renderDeadLetters(payload.dead_letters || [], payload.count || 0);
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setBusy(deadLetterRefreshButton, false);
        }
    }

    async function retryDeadLetters(filePath = null) {
        try {
            const payload = await postJson(bootstrap.urls.deadLettersRetry, filePath ? { file_path: filePath } : {});
            notify(`Retry requested for ${payload.count || 0} item(s).`, "success");
            await refreshQueue();
            await refreshDeadLetters();
        } catch (error) {
            notify(error.message, "error");
        }
    }

    async function deleteDeadLetters(filePath = null, sourceButton = deadLetterDeleteAllButton) {
        const message = filePath
            ? "Delete this dead-letter entry?"
            : sourceButton?.dataset.confirm || "Delete dead-letter entries?";
        if (!window.confirm(message)) {
            return;
        }
        try {
            const payload = await postJson(bootstrap.urls.deadLettersDelete, filePath ? { file_path: filePath } : {});
            notify(`Deleted ${payload.count || 0} dead-letter entr${payload.count === 1 ? "y" : "ies"}.`, "success");
            await refreshDeadLetters();
        } catch (error) {
            notify(error.message, "error");
        }
    }

    return {
        init() {
            queueForm?.addEventListener("submit", (event) => {
                void addQueueItem(event);
            });
            queueRefreshButton?.addEventListener("click", () => {
                void refreshQueue();
            });
            queueClearButton?.addEventListener("click", () => {
                void clearQueue();
            });
            queueList?.addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target.closest("[data-queue-remove]") : null;
                if (!(target instanceof HTMLButtonElement)) {
                    return;
                }
                const filePath = decodeURIComponent(target.dataset.queueRemove || "");
                if (filePath) {
                    void removeQueueItem(filePath);
                }
            });
            batchCheckButton?.addEventListener("click", () => {
                void checkBatch();
            });
            deadLetterRefreshButton?.addEventListener("click", () => {
                void refreshDeadLetters();
            });
            deadLetterRetryAllButton?.addEventListener("click", () => {
                void retryDeadLetters();
            });
            deadLetterDeleteAllButton?.addEventListener("click", () => {
                void deleteDeadLetters();
            });
            deadLetterList?.addEventListener("click", (event) => {
                const retryTarget = event.target instanceof Element ? event.target.closest("[data-dead-letter-retry]") : null;
                if (retryTarget instanceof HTMLButtonElement) {
                    void retryDeadLetters(decodeURIComponent(retryTarget.dataset.deadLetterRetry || ""));
                    return;
                }
                const deleteTarget = event.target instanceof Element ? event.target.closest("[data-dead-letter-delete]") : null;
                if (deleteTarget instanceof HTMLButtonElement) {
                    void deleteDeadLetters(decodeURIComponent(deleteTarget.dataset.deadLetterDelete || ""), deleteTarget);
                }
            });
            void refreshQueue();
            void refreshDeadLetters();
        },
    };
}
