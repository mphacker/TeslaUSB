function asMetaRows(status) {
    return [
        ["Queue depth", status.queue_depth ?? 0],
        ["Files done", status.files_done_session ?? 0],
        ["Active file", status.active_file || "—"],
        ["Source", status.source || "—"],
    ];
}

function stateForStatus(status) {
    if (status.last_error) {
        return "error";
    }
    return status.running ? "running" : "idle";
}

function labelForStatus(status) {
    if (status.last_error) {
        return "Error";
    }
    return status.running ? "Running" : "Idle";
}

function statusCopy(status) {
    if (status.last_error) {
        return status.last_error;
    }
    if (status.running) {
        return status.active_file ? `Parsing ${status.active_file}` : "Indexer is processing queued clips.";
    }
    if (status.last_result) {
        return String(status.last_result);
    }
    return "Indexer is ready for the next scan.";
}

export function createIndexerPanel(options) {
    const statusLabel = options.statusLabel;
    const statusPill = options.statusPill;
    const statusText = options.statusText;
    const metaContainer = options.metaContainer;
    const diagnoseOutput = options.diagnoseOutput;
    const triggerButton = options.triggerButton;
    const rebuildButton = options.rebuildButton;
    const cancelButton = options.cancelButton;
    const diagnoseButton = options.diagnoseButton;
    let pollHandle = 0;

    function renderStatus(status) {
        const label = labelForStatus(status);
        statusLabel.textContent = label;
        statusPill.textContent = label;
        statusPill.dataset.state = stateForStatus(status);
        statusText.textContent = statusCopy(status);
        metaContainer.replaceChildren();
        asMetaRows(status).forEach(([key, value]) => {
            const item = document.createElement("span");
            item.className = "mapping-status-meta";
            item.textContent = `${key}: ${value}`;
            metaContainer.append(item);
        });
    }

    async function fetchStatus() {
        const status = await options.fetchJson(options.api.index_status);
        renderStatus(status);
        window.clearTimeout(pollHandle);
        pollHandle = window.setTimeout(fetchStatus, status.running ? 3000 : 15000);
        return status;
    }

    async function runAction(url, payload) {
        const response = await options.fetchJson(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            body: payload ? JSON.stringify(payload) : undefined,
        });
        options.notify(response.message || "Action complete", response.success === false ? "warning" : "success");
        await fetchStatus();
        await options.onRefresh();
    }

    triggerButton.addEventListener("click", () => {
        runAction(options.api.index_trigger);
    });

    rebuildButton.addEventListener("click", () => {
        runAction(options.api.index_rebuild, { confirm: true });
    });

    cancelButton.addEventListener("click", () => {
        runAction(options.api.index_cancel);
    });

    diagnoseButton.addEventListener("click", async () => {
        diagnoseButton.disabled = true;
        try {
            const payload = await options.fetchJson(`${options.api.index_diagnose}?max=5`);
            diagnoseOutput.textContent = JSON.stringify(payload, null, 2);
        } catch (error) {
            diagnoseOutput.textContent = String(error instanceof Error ? error.message : error);
            options.notify(diagnoseOutput.textContent, "warning");
        } finally {
            diagnoseButton.disabled = false;
        }
    });

    return {
        start() {
            fetchStatus().catch((error) => {
                diagnoseOutput.textContent = String(error instanceof Error ? error.message : error);
            });
        },
        renderStatus,
    };
}
