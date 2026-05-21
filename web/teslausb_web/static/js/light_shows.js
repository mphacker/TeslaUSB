(function() {
    const page = document.getElementById("lightShowsPage");
    if (!page) {
        return;
    }

    const multiInput = document.getElementById("lightShowsMultiInput");
    const dropZone = document.getElementById("lightShowsDropZone");
    const preview = document.getElementById("lightShowsPreview");
    const previewCount = document.getElementById("lightShowsPreviewCount");
    const previewList = document.getElementById("lightShowsPreviewList");
    const clearSelectionButton = document.getElementById("lightShowsClearSelection");
    const progress = document.getElementById("lightShowsProgress");
    const progressTitle = document.getElementById("lightShowsProgressTitle");
    const progressMessage = document.getElementById("lightShowsProgressMessage");
    const progressFill = document.getElementById("lightShowsProgressFill");
    const results = document.getElementById("lightShowsResults");
    const selectAll = document.getElementById("lightShowsSelectAll");
    const bulkDeleteForm = document.getElementById("lightShowsBulkDeleteForm");
    const bulkDeleteButton = document.getElementById("lightShowsBulkDeleteButton");
    const selectedCount = document.getElementById("lightShowsSelectedCount");
    const uploadControls = document.getElementById("lightShowsUploadControls");
    const iosWarning = document.getElementById("lightShowsIosWarning");
    const uploadForms = document.querySelectorAll(".js-light-show-upload-form");
    const actionForms = document.querySelectorAll(".js-light-show-action-form");

    /** @type {File[]} */
    let selectedFiles = [];

    function isIOS() {
        return /(iPad|iPhone|iPod)/i.test(window.navigator.userAgent);
    }

    function isSafari() {
        const ua = window.navigator.userAgent;
        return /Safari/i.test(ua) && !/Chrome|CriOS|EdgiOS|FxiOS|OPiOS/i.test(ua);
    }

    function notify(message, type) {
        if (typeof window.showToast === "function") {
            window.showToast(message, type);
            return;
        }
        window.alert(message);
    }

    function formatBytes(size) {
        if (size < 1024) {
            return `${size} B`;
        }
        const kib = size / 1024;
        if (kib < 1024) {
            return `${kib.toFixed(1)} KB`;
        }
        return `${(kib / 1024).toFixed(2)} MB`;
    }

    function resetResults() {
        if (!results) {
            return;
        }
        results.replaceChildren();
    }

    function setProgressState(stateClass, title, message, percent) {
        if (!progress || !progressTitle || !progressMessage || !progressFill) {
            return;
        }
        progress.classList.remove("is-success", "is-error", "is-warning");
        if (stateClass) {
            progress.classList.add(stateClass);
        }
        progressTitle.textContent = title;
        progressMessage.textContent = message;
        progressFill.style.width = `${percent}%`;
        progressFill.textContent = `${percent}%`;
    }

    function appendResultLine(text, isSuccess) {
        if (!results) {
            return;
        }
        const row = document.createElement("div");
        row.className = "light-shows-results-item";

        const status = document.createElement("strong");
        status.textContent = isSuccess ? "Success" : "Error";
        row.appendChild(status);

        const message = document.createElement("span");
        message.className = "light-shows-results-text";
        message.textContent = text;
        row.appendChild(message);

        results.appendChild(row);
    }

    function renderUploadResults(payload) {
        resetResults();
        if (!payload) {
            return;
        }
        if (Array.isArray(payload.results) && payload.results.length > 0) {
            payload.results.forEach((result) => {
                const fileCount = result.file_count > 1 ? ` (${result.file_count} files)` : "";
                appendResultLine(`${result.filename}${fileCount}: ${result.message}`, result.success === true);
            });
            return;
        }
        if (payload.message) {
            appendResultLine(payload.message, payload.success === true);
        }
    }

    function updatePreview() {
        if (!preview || !previewCount || !previewList) {
            return;
        }
        previewList.replaceChildren();
        if (selectedFiles.length === 0) {
            preview.hidden = true;
            previewCount.textContent = "No files selected yet.";
            return;
        }
        preview.hidden = false;
        previewCount.textContent = `${selectedFiles.length} file(s) ready`;
        selectedFiles.forEach((file, index) => {
            const row = document.createElement("div");
            row.className = "light-shows-preview-item";

            const meta = document.createElement("div");
            meta.className = "light-shows-preview-name";
            meta.textContent = `${file.name} — ${formatBytes(file.size)}`;
            row.appendChild(meta);

            const removeButton = document.createElement("button");
            removeButton.type = "button";
            removeButton.className = "light-shows-btn light-shows-btn-secondary light-shows-preview-remove";
            removeButton.setAttribute("aria-label", `Remove ${file.name}`);
            removeButton.textContent = "Remove";
            removeButton.addEventListener("click", function() {
                selectedFiles.splice(index, 1);
                syncMultiInput();
                updatePreview();
            });
            row.appendChild(removeButton);
            previewList.appendChild(row);
        });
    }

    function syncMultiInput() {
        if (!multiInput) {
            return;
        }
        if (typeof window.DataTransfer === "undefined") {
            if (selectedFiles.length === 0) {
                multiInput.value = "";
            }
            return;
        }
        const transfer = new DataTransfer();
        selectedFiles.forEach((file) => {
            transfer.items.add(file);
        });
        multiInput.files = transfer.files;
    }

    function mergeSelectedFiles(files) {
        const allowed = new Set(["fseq", "mp3", "wav", "zip"]);
        files.forEach((file) => {
            const pieces = file.name.split(".");
            const extension = pieces.length > 1 ? pieces[pieces.length - 1].toLowerCase() : "";
            if (!allowed.has(extension)) {
                return;
            }
            const exists = selectedFiles.some((candidate) => candidate.name === file.name && candidate.size === file.size);
            if (!exists) {
                selectedFiles.push(file);
            }
        });
        syncMultiInput();
        updatePreview();
    }

    function selectedBaseNames() {
        const selected = new Set();
        document.querySelectorAll(".js-light-show-bulk-checkbox:checked").forEach((checkbox) => {
            if (checkbox instanceof HTMLInputElement) {
                selected.add(checkbox.value);
            }
        });
        return Array.from(selected);
    }

    function totalBaseNames() {
        const names = new Set();
        document.querySelectorAll(".js-light-show-bulk-checkbox").forEach((checkbox) => {
            if (checkbox instanceof HTMLInputElement) {
                names.add(checkbox.value);
            }
        });
        return names.size;
    }

    function syncCheckboxMirrors() {
        const byValue = new Map();
        document.querySelectorAll(".js-light-show-bulk-checkbox").forEach((checkbox) => {
            if (!(checkbox instanceof HTMLInputElement)) {
                return;
            }
            const isChecked = byValue.get(checkbox.value) === true || checkbox.checked;
            byValue.set(checkbox.value, isChecked);
        });
        document.querySelectorAll(".js-light-show-bulk-checkbox").forEach((checkbox) => {
            if (checkbox instanceof HTMLInputElement) {
                checkbox.checked = byValue.get(checkbox.value) === true;
            }
        });
    }

    function updateBulkDeleteState() {
        const selected = selectedBaseNames();
        if (selectedCount) {
            selectedCount.textContent = `${selected.length} selected`;
        }
        if (bulkDeleteButton) {
            bulkDeleteButton.disabled = selected.length === 0;
        }
        if (selectAll) {
            const total = totalBaseNames();
            selectAll.checked = total > 0 && selected.length === total;
            selectAll.indeterminate = selected.length > 0 && selected.length < total;
        }
    }

    function sendFormWithProgress(form, files) {
        return new Promise((resolve, reject) => {
            const request = new XMLHttpRequest();
            const formData = new FormData(form);

            if (files) {
                formData.delete("show_files");
                files.forEach((file) => {
                    formData.append("show_files", file);
                });
            }

            request.upload.addEventListener("progress", function(event) {
                if (!event.lengthComputable) {
                    return;
                }
                const percent = Math.max(1, Math.round((event.loaded / event.total) * 100));
                setProgressState("", "Uploading light shows", `Transferred ${formatBytes(event.loaded)} of ${formatBytes(event.total)}.`, percent);
            });

            request.addEventListener("load", function() {
                let payload = null;
                try {
                    payload = JSON.parse(request.responseText);
                } catch (error) {
                    reject(new Error("The server returned an unexpected response."));
                    return;
                }
                if (request.status >= 200 && request.status < 300) {
                    resolve(payload);
                    return;
                }
                reject(new Error(payload && payload.error ? payload.error : request.statusText || "Request failed"));
            });

            request.addEventListener("error", function() {
                reject(new Error("Network error while uploading files."));
            });

            request.open(form.method || "POST", form.action);
            request.setRequestHeader("X-Requested-With", "XMLHttpRequest");
            request.send(formData);
        });
    }

    async function postForm(form) {
        const response = await fetch(form.action, {
            method: form.method || "POST",
            body: new FormData(form),
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        });
        const payload = await response.json();
        if (!response.ok || payload.success !== true) {
            throw new Error(payload.error || payload.message || "Request failed");
        }
        return payload;
    }

    uploadForms.forEach((form) => {
        form.addEventListener("submit", async function(event) {
            event.preventDefault();
            resetResults();

            const kind = form.dataset.uploadKind;
            if (kind === "single") {
                const input = document.getElementById("lightShowsSingleInput");
                if (!(input instanceof HTMLInputElement) || !input.files || input.files.length === 0) {
                    notify("Select one file before uploading.", "error");
                    return;
                }
            }
            if (kind === "zip") {
                const input = document.getElementById("lightShowsZipInput");
                if (!(input instanceof HTMLInputElement) || !input.files || input.files.length === 0) {
                    notify("Select one ZIP archive before uploading.", "error");
                    return;
                }
            }
            if (kind === "multiple" && selectedFiles.length === 0) {
                notify("Select at least one file before uploading.", "error");
                return;
            }

            const submitter = form.querySelector('button[type="submit"]');
            if (!(submitter instanceof HTMLButtonElement)) {
                return;
            }
            const originalMarkup = submitter.innerHTML;
            submitter.disabled = true;
            submitter.textContent = "Uploading…";
            setProgressState("", "Uploading light shows", "Preparing files for upload.", 0);

            try {
                const payload = await sendFormWithProgress(form, kind === "multiple" ? selectedFiles : null);
                const message = payload.summary || payload.message || "Upload complete.";
                const stateClass = payload.success === true ? "is-success" : "is-warning";
                setProgressState(stateClass, payload.success === true ? "Upload complete" : "Upload finished with issues", message, 100);
                renderUploadResults(payload);
                notify(message, payload.success === true ? "success" : "warning");
                if (payload.success === true) {
                    window.setTimeout(function() {
                        window.location.reload();
                    }, 900);
                }
            } catch (error) {
                const message = error instanceof Error ? error.message : "Upload failed.";
                setProgressState("is-error", "Upload failed", message, 100);
                renderUploadResults({ success: false, message });
                notify(message, "error");
            } finally {
                submitter.disabled = false;
                submitter.innerHTML = originalMarkup;
            }
        });
    });

    actionForms.forEach((form) => {
        form.addEventListener("submit", async function(event) {
            event.preventDefault();
            const confirmMessage = form.dataset.confirm;
            if (confirmMessage && !window.confirm(confirmMessage)) {
                return;
            }
            const submitter = form.querySelector('button[type="submit"]');
            if (!(submitter instanceof HTMLButtonElement)) {
                return;
            }
            const originalMarkup = submitter.innerHTML;
            submitter.disabled = true;
            submitter.textContent = "Working…";
            try {
                const payload = await postForm(form);
                notify(payload.message || form.dataset.actionLabel || "Done.", "success");
                window.setTimeout(function() {
                    window.location.reload();
                }, 500);
            } catch (error) {
                const message = error instanceof Error ? error.message : "Request failed.";
                notify(message, "error");
            } finally {
                submitter.disabled = false;
                submitter.innerHTML = originalMarkup;
            }
        });
    });

    if (bulkDeleteForm) {
        bulkDeleteForm.addEventListener("submit", async function(event) {
            event.preventDefault();
            const selected = selectedBaseNames();
            if (selected.length === 0) {
                notify("Select at least one light show to delete.", "error");
                return;
            }
            if (!window.confirm(`Delete ${selected.length} selected light show(s)?`)) {
                return;
            }
            if (bulkDeleteButton instanceof HTMLButtonElement) {
                bulkDeleteButton.disabled = true;
            }
            try {
                const response = await fetch(bulkDeleteForm.action, {
                    method: bulkDeleteForm.method || "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    body: JSON.stringify({ base_names: selected }),
                });
                const payload = await response.json();
                if (!response.ok || payload.success !== true) {
                    throw new Error(payload.error || payload.message || "Bulk delete failed");
                }
                notify(payload.message || "Deleted selected light shows.", "success");
                window.setTimeout(function() {
                    window.location.reload();
                }, 500);
            } catch (error) {
                const message = error instanceof Error ? error.message : "Bulk delete failed.";
                notify(message, "error");
                if (bulkDeleteButton instanceof HTMLButtonElement) {
                    bulkDeleteButton.disabled = false;
                }
            }
        });
    }

    if (multiInput instanceof HTMLInputElement) {
        multiInput.addEventListener("change", function() {
            if (!multiInput.files) {
                return;
            }
            selectedFiles = [];
            mergeSelectedFiles(Array.from(multiInput.files));
        });
    }

    if (clearSelectionButton instanceof HTMLButtonElement) {
        clearSelectionButton.addEventListener("click", function() {
            selectedFiles = [];
            syncMultiInput();
            updatePreview();
        });
    }

    if (dropZone instanceof HTMLButtonElement) {
        dropZone.addEventListener("click", function() {
            if (multiInput instanceof HTMLInputElement) {
                multiInput.click();
            }
        });
        ["dragenter", "dragover"].forEach((eventName) => {
            dropZone.addEventListener(eventName, function(event) {
                event.preventDefault();
                dropZone.classList.add("is-dragover");
            });
        });
        ["dragleave", "drop"].forEach((eventName) => {
            dropZone.addEventListener(eventName, function(event) {
                event.preventDefault();
                dropZone.classList.remove("is-dragover");
            });
        });
        dropZone.addEventListener("drop", function(event) {
            const droppedFiles = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
            if (droppedFiles.length === 0) {
                return;
            }
            mergeSelectedFiles(droppedFiles);
        });
    }

    document.querySelectorAll(".js-light-show-bulk-checkbox").forEach((checkbox) => {
        checkbox.addEventListener("change", function() {
            syncCheckboxMirrors();
            updateBulkDeleteState();
        });
    });

    if (selectAll instanceof HTMLInputElement) {
        selectAll.addEventListener("change", function() {
            document.querySelectorAll(".js-light-show-bulk-checkbox").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) {
                    checkbox.checked = selectAll.checked;
                }
            });
            syncCheckboxMirrors();
            updateBulkDeleteState();
        });
    }

    if (isIOS() && !isSafari()) {
        if (iosWarning) {
            iosWarning.hidden = false;
        }
        if (uploadControls) {
            uploadControls.hidden = true;
        }
    }

    updatePreview();
    updateBulkDeleteState();
    setProgressState("", "Ready to upload", "Choose files above to start an upload.", 0);
})();
