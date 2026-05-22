(function() {
    const page = document.getElementById("music-page");
    if (!page) {
        return;
    }

    const allowedExtensions = [".mp3", ".flac", ".wav", ".aac", ".m4a"];
    const directUploadUrl = page.dataset.uploadUrl || "";
    const chunkUploadUrl = page.dataset.uploadChunkUrl || "";
    const deleteFileTemplate = page.dataset.deleteUrl || "";
    const deleteDirTemplate = page.dataset.deleteDirUrl || "";
    const moveUrl = page.dataset.moveUrl || "";
    const mkdirUrl = page.dataset.mkdirUrl || "";
    const browseUrl = page.dataset.browseUrl || "/music/";
    const currentPath = page.dataset.currentPath || "";
    const usedPct = Number(page.dataset.usedPct || "0");
    const maxMb = Number(page.dataset.maxMb || "0");
    const chunkMb = Math.max(1, Number(page.dataset.chunkMb || "1") || 1);
    const maxBytes = maxMb > 0 ? maxMb * 1024 * 1024 : null;
    const chunkBytes = chunkMb * 1024 * 1024;

    const usageFill = document.getElementById("musicUsageFill");
    const dropZone = document.getElementById("musicDropZone");
    const fileInput = document.getElementById("musicFileInput");
    const queuePanel = document.getElementById("musicQueue");
    const queueSummary = document.getElementById("musicQueueSummary");
    const queueList = document.getElementById("musicQueueList");
    const clearQueueButton = document.getElementById("musicClearSelection");
    const startUploadButton = document.getElementById("musicStartUpload");
    const createFolderButton = document.getElementById("musicCreateFolder");
    const folderNameInput = document.getElementById("musicFolderName");
    const statusEl = document.getElementById("musicStatus");
    const overlay = document.getElementById("musicLoadingOverlay");
    const overlayText = document.getElementById("musicOverlayText");
    const selectAll = document.getElementById("musicSelectAll");
    const selectedCount = document.getElementById("musicSelectedCount");
    const bulkDeleteButton = document.getElementById("musicBulkDeleteButton");

    /** @type {Array<{file: File, targetPath: string, progressEl: HTMLElement | null, statusEl: HTMLElement | null, rowEl: HTMLElement | null}>} */
    let queue = [];
    let busy = false;

    function notify(message, type) {
        if (typeof window.showToast === "function") {
            window.showToast(message, type);
            return;
        }
        window.alert(message);
    }

    function setStatus(message, tone) {
        if (!statusEl) {
            return;
        }
        statusEl.textContent = message;
        statusEl.className = "music-status";
        if (tone === "error") {
            statusEl.classList.add("is-error");
        } else if (tone === "success") {
            statusEl.classList.add("is-success");
        } else if (tone === "working") {
            statusEl.classList.add("is-working");
        }
    }

    function showOverlay(message) {
        if (overlayText) {
            overlayText.textContent = message;
        }
        if (overlay) {
            overlay.hidden = false;
        }
    }

    function hideOverlay() {
        if (overlay) {
            overlay.hidden = true;
        }
    }

    function setBusyState(isBusy) {
        busy = isBusy;
        if (dropZone instanceof HTMLButtonElement) {
            dropZone.disabled = isBusy;
        }
        if (clearQueueButton instanceof HTMLButtonElement) {
            clearQueueButton.disabled = isBusy || queue.length === 0;
        }
        if (startUploadButton instanceof HTMLButtonElement) {
            startUploadButton.disabled = isBusy || queue.length === 0;
        }
        if (createFolderButton instanceof HTMLButtonElement) {
            createFolderButton.disabled = isBusy;
        }
        if (bulkDeleteButton instanceof HTMLButtonElement) {
            bulkDeleteButton.disabled = isBusy || selectedFilePaths().length === 0;
        }
        if (selectAll instanceof HTMLInputElement) {
            selectAll.disabled = isBusy;
        }
        document.querySelectorAll(".js-music-file-select, .js-delete-file, .js-delete-dir, .js-move-file").forEach((element) => {
            if (element instanceof HTMLInputElement || element instanceof HTMLButtonElement) {
                element.disabled = isBusy;
            }
        });
    }

    function currentBrowseUrl(path) {
        if (!path) {
            return browseUrl;
        }
        return `${browseUrl}?path=${encodeURIComponent(path)}`;
    }

    function formatBytes(size) {
        if (!Number.isFinite(size) || size < 1024) {
            return `${Math.max(0, Math.round(size || 0))} B`;
        }
        const kib = size / 1024;
        if (kib < 1024) {
            return `${kib.toFixed(1)} KB`;
        }
        return `${(kib / 1024).toFixed(2)} MB`;
    }

    function fileExtension(name) {
        const index = name.lastIndexOf(".");
        return index >= 0 ? name.slice(index).toLowerCase() : "";
    }

    function queueKey(file, targetPath) {
        return `${file.name}__${file.size}__${file.lastModified}__${targetPath}`;
    }

    function deriveTargetPath(file) {
        const relativePath = (file.webkitRelativePath || file.relativePath || "").replace(/^\/+/, "");
        const relativeDir = relativePath.includes("/")
            ? relativePath.slice(0, relativePath.lastIndexOf("/"))
            : "";
        if (currentPath && relativeDir) {
            return `${currentPath}/${relativeDir}`;
        }
        return currentPath || relativeDir;
    }

    function describeTargetPath(path) {
        return path ? `/Music/${path}` : "/Music";
    }

    function uploadModeLabel(file) {
        return file.size > chunkBytes ? "Chunked upload" : "Direct upload";
    }

    function safeMessage(payload, fallback) {
        if (payload && typeof payload === "object") {
            if (typeof payload.error === "string" && payload.error) {
                return payload.error;
            }
            if (typeof payload.message === "string" && payload.message) {
                return payload.message;
            }
            if (Array.isArray(payload.messages) && payload.messages.length > 0) {
                const first = payload.messages[0];
                if (typeof first === "string" && first) {
                    return first;
                }
            }
        }
        return fallback;
    }

    function readEntriesAsync(dirReader) {
        return new Promise((resolve, reject) => {
            dirReader.readEntries((entries) => resolve(entries), reject);
        });
    }

    async function collectFromEntry(entry, basePath, out) {
        if (entry.isFile) {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            file.relativePath = basePath ? `${basePath}/${file.name}` : file.name;
            out.push(file);
            return;
        }
        if (!entry.isDirectory) {
            return;
        }
        const reader = entry.createReader();
        let entries;
        do {
            entries = await readEntriesAsync(reader);
            for (const child of entries) {
                await collectFromEntry(
                    child,
                    basePath ? `${basePath}/${entry.name}` : entry.name,
                    out,
                );
            }
        } while (entries.length > 0);
    }

    function renderQueue() {
        if (!queuePanel || !queueList || !queueSummary) {
            return;
        }
        queueList.replaceChildren();
        queuePanel.hidden = queue.length === 0;

        if (queue.length === 0) {
            queueSummary.textContent = "No files queued.";
            setBusyState(busy);
            return;
        }

        queueSummary.textContent = `${queue.length} file(s) staged for upload.`;
        queue.forEach((item, index) => {
            const row = document.createElement("article");
            row.className = "music-entry-card music-queue-item";

            const rowHeader = document.createElement("div");
            rowHeader.className = "music-queue-row";

            const titleWrap = document.createElement("div");
            titleWrap.className = "music-panel-head";

            const title = document.createElement("div");
            title.className = "music-queue-name";
            title.textContent = item.file.name;
            titleWrap.appendChild(title);

            const meta = document.createElement("div");
            meta.className = "music-queue-meta";

            const size = document.createElement("span");
            size.textContent = formatBytes(item.file.size);
            meta.appendChild(size);

            const mode = document.createElement("span");
            mode.textContent = uploadModeLabel(item.file);
            meta.appendChild(mode);

            const target = document.createElement("span");
            target.textContent = describeTargetPath(item.targetPath);
            meta.appendChild(target);

            titleWrap.appendChild(meta);
            rowHeader.appendChild(titleWrap);

            const removeButton = document.createElement("button");
            removeButton.type = "button";
            removeButton.className = "music-btn music-btn-secondary";
            removeButton.dataset.removeIndex = String(index);
            removeButton.textContent = "Remove";
            rowHeader.appendChild(removeButton);
            row.appendChild(rowHeader);

            const progress = document.createElement("div");
            progress.className = "music-queue-progress";
            const progressFill = document.createElement("span");
            progressFill.className = "music-queue-progress-fill";
            progress.appendChild(progressFill);
            row.appendChild(progress);

            const state = document.createElement("p");
            state.className = "music-entry-meta";
            state.textContent = "Waiting";
            row.appendChild(state);

            item.rowEl = row;
            item.progressEl = progressFill;
            item.statusEl = state;
            queueList.appendChild(row);
        });
        setBusyState(busy);
    }

    function addFiles(files) {
        const existing = new Set(queue.map((item) => queueKey(item.file, item.targetPath)));
        let added = 0;
        let skipped = 0;

        Array.from(files).forEach((file) => {
            if (!(file instanceof File) || file.name.startsWith(".")) {
                skipped += 1;
                return;
            }
            const extension = fileExtension(file.name);
            if (!allowedExtensions.includes(extension)) {
                skipped += 1;
                return;
            }
            if (maxBytes !== null && file.size > maxBytes) {
                skipped += 1;
                return;
            }
            const targetPath = deriveTargetPath(file);
            const key = queueKey(file, targetPath);
            if (existing.has(key)) {
                skipped += 1;
                return;
            }
            existing.add(key);
            queue.push({
                file,
                targetPath,
                progressEl: null,
                statusEl: null,
                rowEl: null,
            });
            added += 1;
        });

        renderQueue();
        if (added > 0) {
            setStatus(`${added} file(s) added to the queue.`, "success");
        } else if (skipped > 0) {
            setStatus("No supported files were added. Check file types and device size limits.", "error");
        }
    }

    function clearQueue() {
        if (busy) {
            return;
        }
        queue = [];
        if (fileInput instanceof HTMLInputElement) {
            fileInput.value = "";
        }
        renderQueue();
        setStatus("Queue cleared.", "success");
    }

    function updateQueueItem(item, percent, text, stateClass) {
        if (item.progressEl) {
            item.progressEl.style.width = `${percent}%`;
        }
        if (item.statusEl) {
            item.statusEl.textContent = text;
        }
        if (item.rowEl) {
            item.rowEl.classList.remove("is-complete", "is-error");
            if (stateClass) {
                item.rowEl.classList.add(stateClass);
            }
        }
    }

    async function uploadDirectFile(item) {
        const url = currentBrowseUrl(item.targetPath).replace(browseUrl, directUploadUrl);
        const formData = new FormData();
        formData.append("music_files", item.file, item.file.name);
        if (item.targetPath) {
            formData.append("path", item.targetPath);
        }
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
            body: formData,
        });
        const payload = await response.json();
        if (!response.ok || payload.success !== true) {
            throw new Error(safeMessage(payload, `Upload failed for ${item.file.name}.`));
        }
        return safeMessage(payload, `Uploaded ${item.file.name}.`);
    }

    function uploadId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID().replace(/-/g, "").slice(0, 32).padEnd(32, "0");
        }
        const randomPart = `${Date.now()}${Math.random()}`.replace(/[^0-9]/g, "");
        return randomPart.slice(0, 32).padEnd(32, "0");
    }

    async function uploadChunkedFile(item) {
        const file = item.file;
        const totalChunks = Math.max(1, Math.ceil(file.size / chunkBytes));
        const uploadIdentifier = uploadId();

        for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
            const start = chunkIndex * chunkBytes;
            const end = Math.min(start + chunkBytes, file.size);
            const chunk = file.slice(start, end);
            const params = new URLSearchParams({
                upload_id: uploadIdentifier,
                filename: file.name,
                chunk_index: String(chunkIndex),
                total_chunks: String(totalChunks),
                total_size: String(file.size),
            });
            if (item.targetPath) {
                params.set("path", item.targetPath);
            }
            const response = await fetch(`${chunkUploadUrl}?${params.toString()}`, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/octet-stream",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-File-Size": String(file.size),
                },
                body: chunk,
            });
            const payload = await response.json();
            if (!response.ok || payload.success !== true) {
                throw new Error(safeMessage(payload, `Chunked upload failed for ${file.name}.`));
            }
            const percent = Math.round(((chunkIndex + 1) / totalChunks) * 100);
            updateQueueItem(item, percent, `${percent}% transferred`, "");
        }
        return `Uploaded ${file.name}.`;
    }

    async function uploadQueue() {
        if (busy || queue.length === 0) {
            return;
        }
        setBusyState(true);
        showOverlay("Uploading music…");
        setStatus("Uploading queued files…", "working");

        try {
            for (const item of queue) {
                updateQueueItem(item, 0, "Preparing upload", "");
                const message = item.file.size > chunkBytes
                    ? await uploadChunkedFile(item)
                    : await uploadDirectFile(item);
                updateQueueItem(item, 100, "Uploaded", "is-complete");
                notify(message, "success");
            }
            setStatus("Upload complete. Refreshing the library…", "success");
            window.setTimeout(() => {
                window.location.href = currentBrowseUrl(currentPath);
            }, 600);
        } catch (error) {
            const message = error instanceof Error ? error.message : "Upload failed.";
            setStatus(message, "error");
            notify(message, "error");
            const currentItem = queue.find((item) => item.statusEl && item.statusEl.textContent !== "Uploaded");
            if (currentItem) {
                updateQueueItem(currentItem, 100, "Failed", "is-error");
            }
        } finally {
            hideOverlay();
            setBusyState(false);
        }
    }

    function resolveTemplate(template, placeholder, value) {
        return template.replace(placeholder, encodeURIComponent(value));
    }

    async function fetchJson(url, options) {
        const response = await fetch(url, options);
        const payload = await response.json();
        if (!response.ok || payload.success !== true) {
            throw new Error(safeMessage(payload, "Request failed."));
        }
        return payload;
    }

    function selectedFilePaths() {
        const selected = [];
        document.querySelectorAll(".js-music-file-select:checked").forEach((checkbox) => {
            if (checkbox instanceof HTMLInputElement) {
                selected.push(checkbox.value);
            }
        });
        return selected;
    }

    function updateSelectionState() {
        const checkboxes = document.querySelectorAll(".js-music-file-select");
        const selected = selectedFilePaths();
        if (selectedCount) {
            selectedCount.textContent = `${selected.length} selected`;
        }
        if (bulkDeleteButton instanceof HTMLButtonElement) {
            bulkDeleteButton.disabled = busy || selected.length === 0;
        }
        if (selectAll instanceof HTMLInputElement) {
            selectAll.checked = checkboxes.length > 0 && selected.length === checkboxes.length;
            selectAll.indeterminate = selected.length > 0 && selected.length < checkboxes.length;
        }
    }

    async function deleteSingleFile(filePath) {
        const url = resolveTemplate(deleteFileTemplate, "__NAME__", filePath);
        await fetchJson(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
    }

    async function handleDeleteFile(filePath) {
        if (!window.confirm(`Delete "${filePath}"?`)) {
            return;
        }
        setBusyState(true);
        showOverlay("Deleting file…");
        setStatus("Deleting file…", "working");
        try {
            const payload = await fetchJson(resolveTemplate(deleteFileTemplate, "__NAME__", filePath), {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            notify(payload.message || "File deleted.", "success");
            window.setTimeout(() => {
                window.location.href = currentBrowseUrl(currentPath);
            }, 500);
        } catch (error) {
            const message = error instanceof Error ? error.message : "Delete failed.";
            setStatus(message, "error");
            notify(message, "error");
        } finally {
            hideOverlay();
            setBusyState(false);
        }
    }

    async function handleDeleteDirectory(directoryPath) {
        if (!window.confirm(`Delete folder "${directoryPath}" and everything inside it?`)) {
            return;
        }
        setBusyState(true);
        showOverlay("Deleting folder…");
        setStatus("Deleting folder…", "working");
        try {
            const payload = await fetchJson(resolveTemplate(deleteDirTemplate, "__DIR__", directoryPath), {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            notify(payload.message || "Folder deleted.", "success");
            window.setTimeout(() => {
                window.location.href = currentBrowseUrl(currentPath);
            }, 500);
        } catch (error) {
            const message = error instanceof Error ? error.message : "Delete folder failed.";
            setStatus(message, "error");
            notify(message, "error");
        } finally {
            hideOverlay();
            setBusyState(false);
        }
    }

    async function handleMoveFile(filePath) {
        const destination = window.prompt("Move to folder (relative path, blank for /Music):", currentPath);
        if (destination === null) {
            return;
        }
        const newName = window.prompt("Optional new filename (leave blank to keep the current name):", "");
        if (newName === null) {
            return;
        }
        setBusyState(true);
        showOverlay("Moving file…");
        setStatus("Moving file…", "working");
        try {
            const payload = await fetchJson(moveUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({
                    source: filePath,
                    dest_path: destination || "",
                    new_name: newName || "",
                }),
            });
            notify(payload.message || "File moved.", "success");
            window.setTimeout(() => {
                window.location.href = currentBrowseUrl(destination || "");
            }, 500);
        } catch (error) {
            const message = error instanceof Error ? error.message : "Move failed.";
            setStatus(message, "error");
            notify(message, "error");
        } finally {
            hideOverlay();
            setBusyState(false);
        }
    }

    async function handleCreateFolder() {
        if (!(folderNameInput instanceof HTMLInputElement)) {
            return;
        }
        const name = folderNameInput.value.trim();
        if (!name) {
            setStatus("Enter a folder name before creating it.", "error");
            return;
        }
        setBusyState(true);
        showOverlay("Creating folder…");
        setStatus("Creating folder…", "working");
        try {
            const payload = await fetchJson(mkdirUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({ path: currentPath, name }),
            });
            notify(payload.message || "Folder created.", "success");
            window.setTimeout(() => {
                window.location.href = currentBrowseUrl(currentPath);
            }, 500);
        } catch (error) {
            const message = error instanceof Error ? error.message : "Create folder failed.";
            setStatus(message, "error");
            notify(message, "error");
        } finally {
            hideOverlay();
            setBusyState(false);
        }
    }

    async function handleBulkDelete() {
        const selected = selectedFilePaths();
        if (selected.length === 0) {
            setStatus("Select at least one file to delete.", "error");
            return;
        }
        if (!window.confirm(`Delete ${selected.length} selected file(s)?`)) {
            return;
        }
        setBusyState(true);
        showOverlay("Deleting selected files…");
        setStatus("Deleting selected files…", "working");
        try {
            for (const filePath of selected) {
                await deleteSingleFile(filePath);
            }
            notify(`Deleted ${selected.length} file(s).`, "success");
            window.setTimeout(() => {
                window.location.href = currentBrowseUrl(currentPath);
            }, 500);
        } catch (error) {
            const message = error instanceof Error ? error.message : "Bulk delete failed.";
            setStatus(message, "error");
            notify(message, "error");
        } finally {
            hideOverlay();
            setBusyState(false);
        }
    }

    if (usageFill) {
        const bounded = Math.max(0, Math.min(100, usedPct));
        usageFill.style.width = `${bounded}%`;
    }

    renderQueue();
    updateSelectionState();

    if (dropZone instanceof HTMLButtonElement && fileInput instanceof HTMLInputElement) {
        dropZone.addEventListener("click", () => {
            if (!busy) {
                fileInput.click();
            }
        });
        ["dragenter", "dragover"].forEach((eventName) => {
            dropZone.addEventListener(eventName, (event) => {
                event.preventDefault();
                if (!busy) {
                    dropZone.classList.add("is-dragover");
                }
            });
        });
        ["dragleave", "drop"].forEach((eventName) => {
            dropZone.addEventListener(eventName, (event) => {
                event.preventDefault();
                dropZone.classList.remove("is-dragover");
            });
        });
        dropZone.addEventListener("drop", async (event) => {
            if (busy) {
                return;
            }
            const items = event.dataTransfer ? event.dataTransfer.items : null;
            if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === "function") {
                const collected = [];
                for (const item of items) {
                    const entry = item.webkitGetAsEntry();
                    if (entry) {
                        await collectFromEntry(entry, "", collected);
                    }
                }
                if (collected.length > 0) {
                    addFiles(collected);
                    return;
                }
            }
            if (event.dataTransfer && event.dataTransfer.files) {
                addFiles(event.dataTransfer.files);
            }
        });

        fileInput.addEventListener("change", (event) => {
            const target = event.target;
            if (target instanceof HTMLInputElement && target.files) {
                addFiles(target.files);
            }
        });
    }

    if (queueList) {
        queueList.addEventListener("click", (event) => {
            const target = event.target instanceof Element ? event.target.closest("[data-remove-index]") : null;
            if (!(target instanceof HTMLButtonElement) || busy) {
                return;
            }
            const index = Number(target.dataset.removeIndex || "-1");
            if (index >= 0) {
                queue.splice(index, 1);
                renderQueue();
                setStatus("Removed one file from the queue.", "success");
            }
        });
    }

    if (clearQueueButton instanceof HTMLButtonElement) {
        clearQueueButton.addEventListener("click", clearQueue);
    }

    if (startUploadButton instanceof HTMLButtonElement) {
        startUploadButton.addEventListener("click", uploadQueue);
    }

    if (createFolderButton instanceof HTMLButtonElement) {
        createFolderButton.addEventListener("click", handleCreateFolder);
    }

    if (folderNameInput instanceof HTMLInputElement) {
        folderNameInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                handleCreateFolder();
            }
        });
    }

    document.querySelectorAll(".js-delete-file").forEach((button) => {
        button.addEventListener("click", () => {
            if (button instanceof HTMLButtonElement) {
                void handleDeleteFile(button.dataset.delete || "");
            }
        });
    });

    document.querySelectorAll(".js-delete-dir").forEach((button) => {
        button.addEventListener("click", () => {
            if (button instanceof HTMLButtonElement) {
                void handleDeleteDirectory(button.dataset.deleteDir || "");
            }
        });
    });

    document.querySelectorAll(".js-move-file").forEach((button) => {
        button.addEventListener("click", () => {
            if (button instanceof HTMLButtonElement) {
                void handleMoveFile(button.dataset.move || "");
            }
        });
    });

    document.querySelectorAll(".js-music-file-select").forEach((checkbox) => {
        checkbox.addEventListener("change", updateSelectionState);
    });

    // Folder navigation: clicking a folder row (desktop) or card (mobile)
    // navigates into that folder. Clicks on action buttons (.action-btn) or
    // form controls inside the row are excluded so Delete still works.
    function bindFolderNavigation(selector) {
        document.querySelectorAll(selector).forEach((row) => {
            if (!(row instanceof HTMLElement)) return;
            const dir = row.dataset.dir;
            if (!dir) return;
            row.style.cursor = "pointer";
            row.addEventListener("click", (event) => {
                const target = event.target;
                if (target instanceof Element) {
                    if (target.closest("button, a, input, label, .action-btn")) {
                        return;
                    }
                }
                window.location.href = currentBrowseUrl(dir);
            });
            row.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    window.location.href = currentBrowseUrl(dir);
                }
            });
            if (!row.hasAttribute("tabindex")) row.setAttribute("tabindex", "0");
            if (!row.hasAttribute("role")) row.setAttribute("role", "link");
        });
    }
    bindFolderNavigation(".music-folder-row");
    bindFolderNavigation(".music-mobile-folder");

    if (selectAll instanceof HTMLInputElement) {
        selectAll.addEventListener("change", () => {
            document.querySelectorAll(".js-music-file-select").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) {
                    checkbox.checked = selectAll.checked;
                }
            });
            updateSelectionState();
        });
    }

    if (bulkDeleteButton instanceof HTMLButtonElement) {
        bulkDeleteButton.addEventListener("click", () => {
            void handleBulkDelete();
        });
    }
})();
