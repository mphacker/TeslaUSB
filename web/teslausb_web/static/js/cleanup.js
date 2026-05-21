const confirmForms = () => {
    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        const message = form.dataset.confirm;
        if (message && !window.confirm(message)) {
            event.preventDefault();
        }
    });
};

const setText = (selector, value) => {
    const node = document.querySelector(selector);
    if (node instanceof HTMLElement) {
        node.textContent = value;
    }
};

const formatGiB = (bytes) => `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;

const updateRunStatus = (payload) => {
    const run = payload.run;
    setText("[data-cleanup-field='status']", run.status);
    setText("[data-cleanup-field='processed']", `${run.processed_candidates} / ${run.total_candidates}`);
    setText("[data-cleanup-field='deleted-count']", String(run.deleted_count));
    setText("[data-cleanup-field='deleted-bytes']", formatGiB(run.deleted_bytes));
    setText("[data-cleanup-field='current-path']", run.current_path || "Idle");
    const errors = document.querySelector("[data-cleanup-errors]");
    if (errors instanceof HTMLElement) {
        errors.innerHTML = "";
        for (const entry of run.errors) {
            const item = document.createElement("li");
            item.textContent = entry;
            errors.appendChild(item);
        }
    }
};

const pollRunStatus = () => {
    const panel = document.querySelector("[data-cleanup-poll-url]");
    if (!(panel instanceof HTMLElement)) {
        return;
    }
    const pollUrl = panel.dataset.cleanupPollUrl;
    if (!pollUrl) {
        return;
    }

    const tick = async () => {
        try {
            const response = await fetch(pollUrl, {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            updateRunStatus(payload);
            if (!payload.active) {
                window.setTimeout(() => window.location.reload(), 1200);
                return;
            }
        } catch {
            return;
        }
        window.setTimeout(tick, 2000);
    };

    void tick();
};

confirmForms();
pollRunStatus();
