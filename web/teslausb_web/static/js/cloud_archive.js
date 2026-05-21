import { createArchiveController } from "./cloud_archive/archive.js";
import { createBrowseController } from "./cloud_archive/browse.js";
import { createProviderController } from "./cloud_archive/provider.js";
import { createQueueController } from "./cloud_archive/queue.js";
import { createSyncControlController } from "./cloud_archive/sync_control.js";

const page = document.getElementById("cloudArchivePage");
const bootstrap = window.CLOUD_ARCHIVE_BOOTSTRAP || null;

if (!page || !bootstrap) {
    throw new Error("Cloud archive bootstrap is missing.");
}

const state = {
    provider: bootstrap.provider || "",
    providerConnected: bootstrap.providerConnected === true,
    pendingAuthorization: bootstrap.pendingAuthorization || null,
    syncStatus: bootstrap.syncStatus || {},
    syncStats: bootstrap.syncStats || {},
    syncHistory: Array.isArray(bootstrap.syncHistory) ? bootstrap.syncHistory : [],
    browsePath: bootstrap.remotePath || "",
};

function notify(message, tone = "info") {
    if (typeof window.showToast === "function") {
        window.showToast(message, tone);
        return;
    }
    window.alert(message);
}

async function fetchJson(url, options = {}) {
    const request = {
        credentials: "same-origin",
        headers: {
            "X-Requested-With": "XMLHttpRequest",
            ...(options.headers || {}),
        },
        ...options,
    };
    const response = await fetch(url, request);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(payload.error || payload.message || `Request failed: ${response.status}`);
    }
    return payload;
}

function postJson(url, payload) {
    return fetchJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}),
    });
}

function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (!Number.isFinite(value) || value <= 0) {
        return "0 B";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = value;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
        size /= 1024;
        index += 1;
    }
    return `${size.toFixed(index === 0 ? 0 : size >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatDateTime(value) {
    if (!value) {
        return "—";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return String(value);
    }
    return date.toLocaleString();
}

function formatRelativeTime(value) {
    if (!value) {
        return "—";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return String(value);
    }
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) {
        return "just now";
    }
    if (seconds < 3600) {
        return `${Math.floor(seconds / 60)}m ago`;
    }
    if (seconds < 86400) {
        return `${Math.floor(seconds / 3600)}h ago`;
    }
    return `${Math.floor(seconds / 86400)}d ago`;
}

function formatDuration(startValue, endValue) {
    const start = new Date(startValue);
    const end = new Date(endValue);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
        return "—";
    }
    const totalSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
    if (totalSeconds < 60) {
        return `${totalSeconds}s`;
    }
    if (totalSeconds < 3600) {
        return `${Math.floor(totalSeconds / 60)}m ${totalSeconds % 60}s`;
    }
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    return `${hours}h ${minutes}m`;
}

function toTitleCase(value) {
    if (!value) {
        return "—";
    }
    return String(value)
        .replace(/-/g, " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const helpers = {
    bootstrap,
    fetchJson,
    postJson,
    formatBytes,
    formatDateTime,
    formatRelativeTime,
    formatDuration,
    notify,
    state,
    toTitleCase,
};

const controllers = [
    createProviderController(helpers),
    createSyncControlController(helpers),
    createQueueController(helpers),
    createBrowseController(helpers),
    createArchiveController(helpers),
];

for (const controller of controllers) {
    controller.init();
}
