export function createBrowseController({ bootstrap, fetchJson, postJson, notify, state }) {
    const browseTree = document.getElementById("cloudBrowseTree");
    const breadcrumbs = document.getElementById("cloudBrowseBreadcrumbs");
    const remotePathLabel = document.getElementById("cloudRemotePathLabel");
    const browseStatus = document.getElementById("cloudBrowseStatus");
    const refreshButton = document.getElementById("cloudBrowseRefreshButton");
    const upButton = document.getElementById("cloudBrowseUpButton");
    const selectButton = document.getElementById("cloudBrowseSelectButton");
    const createButton = document.getElementById("cloudBrowseCreateButton");

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

    function renderBreadcrumbs(path) {
        if (!breadcrumbs) {
            return;
        }
        const parts = path ? path.split("/").filter(Boolean) : [];
        const rows = [`<button class="cloud-archive-secondary-btn" type="button" data-browse-path="">Root</button>`];
        let cumulative = "";
        for (const part of parts) {
            cumulative = cumulative ? `${cumulative}/${part}` : part;
            rows.push(`<span>/</span><button class="cloud-archive-secondary-btn" type="button" data-browse-path="${cumulative}">${part}</button>`);
        }
        breadcrumbs.innerHTML = rows.join("");
    }

    function renderTree(path, folders) {
        state.browsePath = path || "";
        if (remotePathLabel) {
            remotePathLabel.textContent = state.browsePath || "Root";
        }
        renderBreadcrumbs(state.browsePath);
        if (!browseTree) {
            return;
        }
        const parent = state.browsePath.includes("/")
            ? state.browsePath.split("/").slice(0, -1).join("/")
            : "";
        const rows = [];
        if (state.browsePath) {
            rows.push(`<button class="cloud-archive-tree-row" type="button" data-browse-path="${parent}"><span>..</span></button>`);
        }
        if (!folders.length) {
            rows.push('<div class="cloud-archive-tree-row is-current"><span>No folders at this level.</span></div>');
        }
        for (const folder of folders) {
            const folderPath = state.browsePath ? `${state.browsePath}/${folder}` : folder;
            rows.push(`<button class="cloud-archive-tree-row${folderPath === state.browsePath ? " is-current" : ""}" type="button" data-browse-path="${folderPath}"><span>${folder}</span></button>`);
        }
        browseTree.innerHTML = rows.join("");
    }

    async function loadPath(path = state.browsePath || "") {
        setBusy(refreshButton, true, "Loading");
        try {
            const payload = await fetchJson(`${bootstrap.urls.browse}?path=${encodeURIComponent(path)}`);
            renderTree(payload.path || path, Array.isArray(payload.folders) ? payload.folders : []);
            if (browseStatus) {
                browseStatus.textContent = payload.path ? `Browsing ${payload.path}.` : "Browsing the remote root.";
            }
        } catch (error) {
            notify(error.message, "warning");
            if (browseStatus) {
                browseStatus.textContent = error.message;
            }
        } finally {
            setBusy(refreshButton, false);
        }
    }

    async function selectCurrentPath() {
        setBusy(selectButton, true, "Saving");
        try {
            const payload = await postJson(bootstrap.urls.setRemotePath, { path: state.browsePath || "" });
            notify(payload.message || `Remote path set to ${payload.path || state.browsePath || "root"}.`, payload.success ? "success" : "warning");
            if (payload.path && remotePathLabel) {
                remotePathLabel.textContent = payload.path;
            }
        } catch (error) {
            notify(error.message, "warning");
        } finally {
            setBusy(selectButton, false);
        }
    }

    async function createFolder() {
        const name = window.prompt("Folder name");
        if (!name) {
            return;
        }
        setBusy(createButton, true, "Creating");
        try {
            const cleanName = name.trim().replace(/[\\/]/g, "");
            const path = state.browsePath ? `${state.browsePath}/${cleanName}` : cleanName;
            const payload = await postJson(bootstrap.urls.createFolder, { path });
            notify(payload.message || "Folder request sent.", payload.success ? "success" : "warning");
            await loadPath(state.browsePath || "");
        } catch (error) {
            notify(error.message, "warning");
        } finally {
            setBusy(createButton, false);
        }
    }

    async function toggleFolder(folder) {
        try {
            const payload = await postJson(bootstrap.urls.toggleSync, { folder });
            notify(payload.message || `Toggle request sent for ${folder}.`, payload.success ? "success" : "warning");
        } catch (error) {
            notify(error.message, "warning");
        }
    }

    return {
        init() {
            refreshButton?.addEventListener("click", () => {
                void loadPath(state.browsePath || "");
            });
            upButton?.addEventListener("click", () => {
                const parent = state.browsePath.includes("/")
                    ? state.browsePath.split("/").slice(0, -1).join("/")
                    : "";
                void loadPath(parent);
            });
            selectButton?.addEventListener("click", () => {
                void selectCurrentPath();
            });
            createButton?.addEventListener("click", () => {
                void createFolder();
            });
            breadcrumbs?.addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target.closest("[data-browse-path]") : null;
                if (target instanceof HTMLButtonElement) {
                    void loadPath(target.dataset.browsePath || "");
                }
            });
            browseTree?.addEventListener("click", (event) => {
                const target = event.target instanceof Element ? event.target.closest("[data-browse-path]") : null;
                if (target instanceof HTMLButtonElement) {
                    void loadPath(target.dataset.browsePath || "");
                }
            });
            document.querySelectorAll("[data-toggle-folder]").forEach((button) => {
                button.addEventListener("click", () => {
                    const folder = button.getAttribute("data-toggle-folder") || "";
                    if (folder) {
                        void toggleFolder(folder);
                    }
                });
            });
            renderBreadcrumbs(state.browsePath || "");
            void loadPath(state.browsePath || "");
        },
    };
}
