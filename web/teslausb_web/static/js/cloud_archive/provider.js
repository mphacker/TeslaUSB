export function createProviderController({ bootstrap, fetchJson, postJson, formatBytes, formatDateTime, notify, state, toTitleCase }) {
    const providerGrid = document.getElementById("cloudProviderGrid");
    const authVisitLink = document.getElementById("cloudAuthVisitLink");
    const authSessionId = document.getElementById("cloudAuthSessionId");
    const authCallbackInput = document.getElementById("cloudAuthCallbackInput");
    const authCompleteButton = document.getElementById("cloudAuthCompleteButton");
    const authStatus = document.getElementById("cloudAuthStatus");
    const providerStatusTitle = document.getElementById("cloudProviderStatusTitle");
    const providerStatusLine = document.getElementById("cloudProviderStatusLine");
    const providerStateBadge = document.getElementById("cloudProviderStateBadge");
    const providerValue = document.getElementById("cloudProviderValue");
    const tokenExpiryValue = document.getElementById("cloudTokenExpiryValue");
    const remoteRootValue = document.getElementById("cloudRemoteRootValue");
    const headerConnection = document.getElementById("cloudHeaderConnection");
    const headerProvider = document.getElementById("cloudHeaderProvider");
    const disconnectButton = document.getElementById("cloudDisconnectButton");
    const testButton = document.getElementById("cloudConnectionTestButton");
    const refreshButton = document.getElementById("cloudConnectionRefreshButton");
    const storageRefreshButton = document.getElementById("cloudStorageRefreshButton");
    const storageUsed = document.getElementById("cloudStorageUsed");
    const storageFree = document.getElementById("cloudStorageFree");
    const storageObjects = document.getElementById("cloudStorageObjects");
    const storageBar = document.getElementById("cloudStorageBar");
    const storageStatus = document.getElementById("cloudStorageStatus");

    function setButtonBusy(button, busy, busyText) {
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

    function providerLabel(provider) {
        return provider ? toTitleCase(provider) : "Choose a provider";
    }

    function updateHeader() {
        if (headerConnection) {
            headerConnection.lastElementChild.textContent = state.providerConnected ? "Connected" : "Not connected";
        }
        if (headerProvider) {
            headerProvider.lastElementChild.textContent = providerLabel(state.provider);
        }
    }

    function highlightCards() {
        providerGrid?.querySelectorAll("[data-provider-card]").forEach((card) => {
            const provider = card.getAttribute("data-provider-card") || "";
            card.classList.toggle("is-active", provider === state.provider);
            card.classList.toggle("is-connected", provider === state.provider && state.providerConnected);
            const badge = card.querySelector(".cloud-archive-badge");
            if (badge instanceof HTMLElement) {
                if (provider === state.provider && state.providerConnected) {
                    badge.textContent = "Connected";
                    badge.className = "cloud-archive-badge is-success";
                } else if (provider === state.provider) {
                    badge.textContent = "Selected";
                    badge.className = "cloud-archive-badge is-warning";
                }
            }
        });
    }

    function renderConnectionStatus(payload) {
        state.providerConnected = payload.connected === true;
        state.provider = payload.provider || state.provider || "";
        state.pendingAuthorization = payload.pending_authorization || state.pendingAuthorization || null;
        updateHeader();
        highlightCards();
        if (providerStatusTitle) {
            providerStatusTitle.textContent = providerLabel(state.providerConnected ? state.provider : state.provider || "");
        }
        if (providerStatusLine) {
            providerStatusLine.textContent = state.providerConnected
                ? "Authorization is stored locally and rclone configuration is ready to render."
                : state.pendingAuthorization
                    ? `Finish the ${providerLabel(state.pendingAuthorization.provider)} authorization to continue.`
                    : "Choose a provider card below to start authorization.";
        }
        if (providerStateBadge) {
            providerStateBadge.textContent = state.providerConnected ? "Connected" : state.pendingAuthorization ? "Pending" : "Awaiting authorization";
            providerStateBadge.className = `cloud-archive-badge ${state.providerConnected ? "is-success" : "is-warning"}`;
        }
        if (providerValue) {
            providerValue.textContent = state.providerConnected ? providerLabel(state.provider) : "—";
        }
        if (tokenExpiryValue) {
            tokenExpiryValue.textContent = payload.token_expiry ? formatDateTime(payload.token_expiry) : "—";
        }
        if (remoteRootValue) {
            remoteRootValue.textContent = payload.remote?.root || "teslausb:";
        }
        if (authSessionId instanceof HTMLInputElement) {
            authSessionId.value = state.pendingAuthorization?.session_id || "";
        }
        if (authVisitLink instanceof HTMLAnchorElement) {
            authVisitLink.href = state.pendingAuthorization?.authorization_url || "#";
            authVisitLink.toggleAttribute("disabled", !state.pendingAuthorization);
        }
        if (authStatus) {
            authStatus.textContent = state.pendingAuthorization
                ? `Pending session ${state.pendingAuthorization.session_id} expires at ${formatDateTime(state.pendingAuthorization.expires_at)}.`
                : state.providerConnected
                    ? "Authorization is complete. You can test the connection or disconnect the provider."
                    : "Start a provider authorization to populate this panel.";
        }
    }

    async function refreshConnectionStatus() {
        try {
            const payload = await fetchJson(bootstrap.urls.connectionStatus);
            renderConnectionStatus(payload);
        } catch (error) {
            notify(error.message, "error");
        }
    }

    async function testConnection() {
        setButtonBusy(testButton, true, "Testing");
        try {
            const payload = await postJson(bootstrap.urls.testConnection, {});
            notify(payload.message || "Connection test finished.", payload.success === false ? "warning" : "success");
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setButtonBusy(testButton, false);
        }
    }

    async function disconnectProvider() {
        const message = disconnectButton?.dataset.confirm || "Disconnect the provider?";
        if (!window.confirm(message)) {
            return;
        }
        setButtonBusy(disconnectButton, true, "Disconnecting");
        try {
            const payload = await postJson(bootstrap.urls.disconnect, {});
            state.providerConnected = false;
            state.pendingAuthorization = null;
            notify(payload.message || "Provider disconnected.", payload.success ? "success" : "warning");
            await refreshConnectionStatus();
            await loadStorageUsage();
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setButtonBusy(disconnectButton, false);
        }
    }

    async function startAuthorization(provider) {
        state.provider = provider;
        const button = providerGrid?.querySelector(`[data-provider-connect="${provider}"]`);
        setButtonBusy(button, true, "Opening");
        try {
            const payload = await postJson(bootstrap.urls.connect, { provider });
            state.pendingAuthorization = payload;
            renderConnectionStatus({
                connected: false,
                provider,
                token_expiry: null,
                remote: null,
                pending_authorization: payload,
            });
            window.open(payload.authorization_url, "_blank", "noopener");
            notify(`Authorization opened for ${providerLabel(provider)}. Paste the callback URL here when it finishes.`, "info");
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setButtonBusy(button, false);
        }
    }

    async function completeAuthorization() {
        const sessionId = authSessionId instanceof HTMLInputElement ? authSessionId.value.trim() : "";
        const redirectValue = authCallbackInput instanceof HTMLTextAreaElement ? authCallbackInput.value.trim() : "";
        if (!sessionId || !redirectValue) {
            notify("Paste the callback URL or code for the pending authorization session.", "warning");
            return;
        }
        setButtonBusy(authCompleteButton, true, "Completing");
        try {
            const payload = await postJson(bootstrap.urls.connect, {
                session_id: sessionId,
                redirect_url: redirectValue,
            });
            state.provider = payload.provider || state.provider;
            state.providerConnected = true;
            state.pendingAuthorization = null;
            if (authCallbackInput instanceof HTMLTextAreaElement) {
                authCallbackInput.value = "";
            }
            notify(payload.message || "Authorization completed.", "success");
            await refreshConnectionStatus();
            await loadStorageUsage();
        } catch (error) {
            notify(error.message, "error");
        } finally {
            setButtonBusy(authCompleteButton, false);
        }
    }

    async function loadStorageUsage() {
        if (!state.providerConnected) {
            if (storageUsed) {
                storageUsed.textContent = "—";
            }
            if (storageFree) {
                storageFree.textContent = "—";
            }
            if (storageObjects) {
                storageObjects.textContent = "—";
            }
            if (storageBar) {
                storageBar.style.width = "0%";
            }
            if (storageStatus) {
                storageStatus.textContent = "Refresh storage usage after connecting a provider.";
            }
            return;
        }
        setButtonBusy(storageRefreshButton, true, "Loading");
        try {
            const payload = await fetchJson(bootstrap.urls.storageUsage);
            const used = Number(payload.used_bytes || 0);
            const free = Number(payload.free_bytes || 0);
            const total = Number(payload.total_bytes || used + free);
            if (storageUsed) {
                storageUsed.textContent = formatBytes(used);
            }
            if (storageFree) {
                storageFree.textContent = formatBytes(free);
            }
            if (storageObjects) {
                storageObjects.textContent = String(payload.object_count || 0);
            }
            if (storageBar) {
                storageBar.style.width = `${total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0}%`;
            }
            if (storageStatus) {
                storageStatus.textContent = `Remote size ${formatBytes(payload.size_bytes || 0)} across ${payload.object_count || 0} objects.`;
            }
        } catch (error) {
            if (storageStatus) {
                storageStatus.textContent = error.message;
            }
            notify(error.message, "warning");
        } finally {
            setButtonBusy(storageRefreshButton, false);
        }
    }

    return {
        init() {
            providerGrid?.addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target.closest("[data-provider-connect]") : null;
                if (!(target instanceof HTMLButtonElement)) {
                    return;
                }
                const provider = target.dataset.providerConnect || "";
                if (provider) {
                    void startAuthorization(provider);
                }
            });
            authCompleteButton?.addEventListener("click", () => {
                void completeAuthorization();
            });
            refreshButton?.addEventListener("click", () => {
                void refreshConnectionStatus();
            });
            testButton?.addEventListener("click", () => {
                void testConnection();
            });
            disconnectButton?.addEventListener("click", () => {
                void disconnectProvider();
            });
            storageRefreshButton?.addEventListener("click", () => {
                void loadStorageUsage();
            });
            renderConnectionStatus({
                connected: state.providerConnected,
                provider: state.provider,
                token_expiry: bootstrap.tokenExpiry,
                remote: { root: bootstrap.remotePath || "teslausb:" },
                pending_authorization: state.pendingAuthorization,
            });
            void loadStorageUsage();
            window.setInterval(() => {
                void refreshConnectionStatus();
            }, 30000);
        },
    };
}
