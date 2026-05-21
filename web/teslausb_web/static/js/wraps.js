(function() {
    const page = document.getElementById("wrapsPage");
    if (!page) {
        return;
    }

    const spriteUrl = page.dataset.spriteUrl || "/static/icons/lucide-sprite.svg";
    const maxFileSize = Number(page.dataset.maxFileSize || "0");
    const minDimension = Number(page.dataset.minDimension || "0");
    const maxDimension = Number(page.dataset.maxDimension || "0");
    const maxFilenameLength = Number(page.dataset.maxFilenameLength || "0");
    const maxWrapCount = Number(page.dataset.maxWrapCount || "0");
    const currentWrapCount = Number(page.dataset.currentWrapCount || "0");

    const multiInput = document.getElementById("wrapsMultiInput");
    const singleInput = document.getElementById("wrapsSingleInput");
    const dropZone = document.getElementById("wrapsDropZone");
    const preview = document.getElementById("wrapsPreview");
    const previewCount = document.getElementById("wrapsPreviewCount");
    const previewList = document.getElementById("wrapsPreviewList");
    const clearSelectionButton = document.getElementById("wrapsClearSelection");
    const multiSubmitButton = document.getElementById("wrapsMultiSubmit");
    const progress = document.getElementById("wrapsProgress");
    const progressTitle = document.getElementById("wrapsProgressTitle");
    const progressMessage = document.getElementById("wrapsProgressMessage");
    const progressFill = document.getElementById("wrapsProgressFill");
    const results = document.getElementById("wrapsResults");
    const selectAll = document.getElementById("wrapsSelectAll");
    const bulkDeleteForm = document.getElementById("wrapsBulkDeleteForm");
    const bulkDeleteButton = document.getElementById("wrapsBulkDeleteButton");
    const selectedCount = document.getElementById("wrapsSelectedCount");
    const uploadControls = document.getElementById("wrapsUploadControls");
    const iosWarning = document.getElementById("wrapsIosWarning");
    const uploadForms = document.querySelectorAll(".js-wrap-upload-form");
    const actionForms = document.querySelectorAll(".js-wrap-action-form");

    /** @type {File[]} */
    let selectedFiles = [];
    const validationResults = new Map();

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

    function fileKey(file) {
        return `${file.name}__${file.size}__${file.lastModified}`;
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
        row.className = "wraps-result-item";

        const status = document.createElement("strong");
        status.textContent = isSuccess ? "Success" : "Error";
        row.appendChild(status);

        const message = document.createElement("span");
        message.className = "wraps-result-text";
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
                const dimensionSuffix = result.dimensions ? ` (${result.dimensions})` : "";
                appendResultLine(`${result.filename}${dimensionSuffix}: ${result.message}`, result.success === true);
            });
            return;
        }
        if (payload.message) {
            appendResultLine(payload.message, payload.success === true);
        }
    }

    function getValidation(file) {
        return validationResults.get(fileKey(file)) || null;
    }

    function validSelectedFiles() {
        return selectedFiles.filter((file) => {
            const validation = getValidation(file);
            return Boolean(validation && validation.valid === true);
        });
    }

    function updatePreview() {
        if (!preview || !previewCount || !previewList) {
            return;
        }

        previewList.replaceChildren();
        if (selectedFiles.length === 0) {
            preview.hidden = true;
            previewCount.textContent = "No files selected yet.";
            if (multiSubmitButton instanceof HTMLButtonElement) {
                multiSubmitButton.disabled = false;
            }
            return;
        }

        const validCount = validSelectedFiles().length;
        const invalidCount = selectedFiles.length - validCount;
        preview.hidden = false;
        previewCount.textContent = `${validCount} ready, ${invalidCount} need fixes.`;

        selectedFiles.forEach((file, index) => {
            const validation = getValidation(file);
            const row = document.createElement("div");
            row.className = "wraps-preview-item";

            const info = document.createElement("div");
            info.className = "wraps-title-group";

            const meta = document.createElement("div");
            meta.className = "wraps-preview-meta";

            const name = document.createElement("strong");
            name.className = "wraps-preview-name";
            name.textContent = file.name;
            meta.appendChild(name);

            const badge = document.createElement("span");
            badge.className = `wraps-badge ${validation && validation.valid ? "is-valid" : "is-invalid"}`;
            badge.textContent = validation && validation.valid ? "Ready" : "Needs review";
            meta.appendChild(badge);
            info.appendChild(meta);

            const copy = document.createElement("p");
            copy.className = "wraps-preview-copy";
            const dimensionText = validation && validation.dimensions
                ? ` — ${validation.dimensions.width}x${validation.dimensions.height}`
                : "";
            copy.textContent = `${formatBytes(file.size)}${dimensionText}`;
            info.appendChild(copy);

            if (validation && Array.isArray(validation.errors) && validation.errors.length > 0) {
                const errorList = document.createElement("ul");
                errorList.className = "wraps-preview-errors";
                validation.errors.forEach((error) => {
                    const item = document.createElement("li");
                    item.textContent = error;
                    errorList.appendChild(item);
                });
                info.appendChild(errorList);
            }

            row.appendChild(info);

            const removeButton = document.createElement("button");
            removeButton.type = "button";
            removeButton.className = "wraps-btn wraps-btn-secondary wraps-icon-only";
            removeButton.setAttribute("aria-label", `Remove ${file.name}`);
            removeButton.innerHTML = `<svg class="wraps-icon" aria-hidden="true"><use href="${spriteUrl}#icon-x"></use></svg>`;
            removeButton.addEventListener("click", function() {
                validationResults.delete(fileKey(file));
                selectedFiles.splice(index, 1);
                syncMultiInput();
                updatePreview();
            });
            row.appendChild(removeButton);
            previewList.appendChild(row);
        });

        if (multiSubmitButton instanceof HTMLButtonElement) {
            multiSubmitButton.disabled = validCount === 0;
        }
    }

    function syncMultiInput() {
        if (!(multiInput instanceof HTMLInputElement)) {
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

    function getImageDimensions(file) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            const url = URL.createObjectURL(file);

            img.onload = function() {
                URL.revokeObjectURL(url);
                resolve({ width: img.width, height: img.height });
            };

            img.onerror = function() {
                URL.revokeObjectURL(url);
                reject(new Error("Failed to load image"));
            };

            img.src = url;
        });
    }

    async function validateFile(file) {
        const errors = [];
        const lowerName = file.name.toLowerCase();
        const isPng = lowerName.endsWith(".png") || file.type === "image/png";
        if (!isPng) {
            errors.push("PNG files only.");
        }

        const baseName = file.name.replace(/\.png$/i, "");
        if (baseName.length === 0) {
            errors.push("Filename is required.");
        }
        if (baseName.length > maxFilenameLength) {
            errors.push(`Filename must be ${maxFilenameLength} characters or less.`);
        }
        if (!/^[A-Za-z0-9_\- ]+$/.test(baseName)) {
            errors.push("Filename can only use letters, numbers, underscores, dashes, and spaces.");
        }
        if (file.size > maxFileSize) {
            errors.push(`File must be ${formatBytes(maxFileSize)} or smaller.`);
        }

        let dimensions = null;
        try {
            dimensions = await getImageDimensions(file);
            if (dimensions.width !== dimensions.height) {
                errors.push("Image must be square.");
            }
            if (dimensions.width < minDimension || dimensions.height < minDimension) {
                errors.push(`Image must be at least ${minDimension}x${minDimension}px.`);
            }
            if (dimensions.width > maxDimension || dimensions.height > maxDimension) {
                errors.push(`Image must be no larger than ${maxDimension}x${maxDimension}px.`);
            }
        } catch (error) {
            errors.push("Could not read image dimensions.");
        }

        return {
            valid: errors.length === 0,
            errors,
            dimensions,
        };
    }

    async function mergeSelectedFiles(files) {
        if (maxWrapCount > 0 && currentWrapCount >= maxWrapCount) {
            notify(`Maximum of ${maxWrapCount} wraps allowed. Delete some wraps first.`, "error");
            return;
        }

        let remainingSlots = maxWrapCount > 0 ? maxWrapCount - currentWrapCount - selectedFiles.length : files.length;
        if (remainingSlots <= 0) {
            notify(`Maximum of ${maxWrapCount} wraps allowed. Delete some wraps first.`, "error");
            return;
        }

        let skippedForCount = false;
        for (const file of files) {
            const exists = selectedFiles.some((candidate) => fileKey(candidate) === fileKey(file));
            if (exists) {
                continue;
            }
            if (remainingSlots <= 0) {
                skippedForCount = true;
                break;
            }
            const validation = await validateFile(file);
            validationResults.set(fileKey(file), validation);
            selectedFiles.push(file);
            remainingSlots -= 1;
        }

        if (skippedForCount) {
            notify(`Only ${maxWrapCount - currentWrapCount} new wrap(s) can be staged at once.`, "warning");
        }

        syncMultiInput();
        updatePreview();
    }

    function sendFormWithProgress(form, files, fieldName) {
        return new Promise((resolve, reject) => {
            const request = new XMLHttpRequest();
            const formData = new FormData(form);

            if (Array.isArray(files) && fieldName) {
                formData.delete(fieldName);
                files.forEach((file) => {
                    formData.append(fieldName, file);
                });
            }

            request.upload.addEventListener("progress", function(event) {
                if (!event.lengthComputable) {
                    return;
                }
                const percent = Math.max(1, Math.round((event.loaded / event.total) * 100));
                setProgressState("", "Uploading wraps", `Transferred ${formatBytes(event.loaded)} of ${formatBytes(event.total)}.`, percent);
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
                reject(new Error(payload && (payload.error || payload.message) ? payload.error || payload.message : request.statusText || "Request failed"));
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

    function selectedFilenames() {
        const selected = [];
        document.querySelectorAll(".js-wrap-bulk-checkbox:checked").forEach((checkbox) => {
            if (checkbox instanceof HTMLInputElement) {
                selected.push(checkbox.value);
            }
        });
        return selected;
    }

    function updateBulkDeleteState() {
        const selected = selectedFilenames();
        const total = document.querySelectorAll(".js-wrap-bulk-checkbox").length;

        if (selectedCount) {
            selectedCount.textContent = `${selected.length} selected`;
        }
        if (bulkDeleteButton instanceof HTMLButtonElement) {
            bulkDeleteButton.disabled = selected.length === 0;
        }
        if (selectAll instanceof HTMLInputElement) {
            selectAll.checked = total > 0 && selected.length === total;
            selectAll.indeterminate = selected.length > 0 && selected.length < total;
        }
        document.querySelectorAll("[data-wrap-card]").forEach((card, index) => {
            if (!(card instanceof HTMLElement)) {
                return;
            }
            const checkbox = document.querySelectorAll(".js-wrap-bulk-checkbox")[index];
            card.classList.toggle("is-selected", checkbox instanceof HTMLInputElement && checkbox.checked);
        });
    }

    uploadForms.forEach((form) => {
        form.addEventListener("submit", async function(event) {
            event.preventDefault();
            resetResults();

            const kind = form.dataset.uploadKind;
            let files = null;
            let fieldName = null;

            if (kind === "single") {
                if (!(singleInput instanceof HTMLInputElement) || !singleInput.files || singleInput.files.length === 0) {
                    notify("Select one file before uploading.", "error");
                    return;
                }
                const file = singleInput.files[0];
                const validation = await validateFile(file);
                if (!validation.valid) {
                    const message = validation.errors.join(" ");
                    setProgressState("is-error", "Upload blocked", message, 100);
                    renderUploadResults({ success: false, message });
                    notify(message, "error");
                    return;
                }
                files = [file];
                fieldName = "wrap_file";
            }

            if (kind === "multiple") {
                files = validSelectedFiles();
                fieldName = "wrap_files";
                if (selectedFiles.length === 0) {
                    notify("Select at least one file before uploading.", "error");
                    return;
                }
                if (files.length === 0) {
                    notify("Resolve the selected file issues before uploading.", "error");
                    return;
                }
            }

            const submitter = form.querySelector('button[type="submit"]');
            if (!(submitter instanceof HTMLButtonElement)) {
                return;
            }
            const originalMarkup = submitter.innerHTML;
            submitter.disabled = true;
            submitter.textContent = "Uploading…";
            setProgressState("", "Uploading wraps", "Preparing files for upload.", 0);

            try {
                const payload = await sendFormWithProgress(form, files, fieldName);
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
            const selected = selectedFilenames();
            if (selected.length === 0) {
                notify("Select at least one wrap to delete.", "error");
                return;
            }
            if (!window.confirm(`Delete ${selected.length} selected wrap(s)?`)) {
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
                    body: JSON.stringify({ filenames: selected }),
                });
                const payload = await response.json();
                if (!response.ok || payload.success !== true) {
                    throw new Error(payload.error || payload.message || "Bulk delete failed");
                }
                notify(payload.message || "Deleted selected wraps.", "success");
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
        multiInput.addEventListener("change", async function() {
            if (!multiInput.files) {
                return;
            }
            selectedFiles = [];
            validationResults.clear();
            await mergeSelectedFiles(Array.from(multiInput.files));
        });
    }

    if (clearSelectionButton instanceof HTMLButtonElement) {
        clearSelectionButton.addEventListener("click", function() {
            selectedFiles = [];
            validationResults.clear();
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
        dropZone.addEventListener("drop", async function(event) {
            const droppedFiles = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
            if (droppedFiles.length === 0) {
                return;
            }
            await mergeSelectedFiles(droppedFiles);
        });
    }

    document.querySelectorAll(".js-wrap-bulk-checkbox").forEach((checkbox) => {
        checkbox.addEventListener("change", function() {
            updateBulkDeleteState();
        });
    });

    if (selectAll instanceof HTMLInputElement) {
        selectAll.addEventListener("change", function() {
            document.querySelectorAll(".js-wrap-bulk-checkbox").forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement) {
                    checkbox.checked = selectAll.checked;
                }
            });
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
