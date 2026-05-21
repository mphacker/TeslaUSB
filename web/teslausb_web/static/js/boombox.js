(function() {
    const page = document.getElementById("boomboxPage");
    if (!page) {
        return;
    }

    const maxFiles = Number(page.dataset.maxFiles || "0");
    const currentCount = Number(page.dataset.currentCount || "0");
    const maxFileBytes = Number(page.dataset.maxFileBytes || "0");
    const allowedExtensions = [".mp3", ".wav"];
    const libraryFull = currentCount >= maxFiles;

    const browseButton = document.getElementById("boomboxBrowseButton");
    const fileInput = document.getElementById("boomboxFileInput");
    const uploadForm = document.getElementById("boomboxUploadForm");
    const uploadButton = document.getElementById("boomboxUploadButton");
    const clearButton = document.getElementById("boomboxClearSelection");
    const selection = document.getElementById("boomboxSelection");
    const selectionName = document.getElementById("boomboxSelectionName");
    const selectionMeta = document.getElementById("boomboxSelectionMeta");
    const selectionBadge = document.getElementById("boomboxSelectionBadge");
    const uploadStatus = document.getElementById("boomboxUploadStatus");
    const selectAll = document.getElementById("boomboxSelectAll");
    const selectedCount = document.getElementById("boomboxSelectedCount");
    const bulkDeleteForm = document.getElementById("boomboxBulkDeleteForm");
    const bulkDeleteButton = document.getElementById("boomboxBulkDeleteButton");
    const fileCheckboxes = Array.from(document.querySelectorAll(".js-boombox-select"));
    const confirmForms = Array.from(document.querySelectorAll(".js-boombox-confirm"));

    let submitting = false;
    let selectedFileIsValid = false;

    function notify(message, type) {
        if (typeof window.showToast === "function") {
            window.showToast(message, type);
            return;
        }
        window.alert(message);
    }

    function setStatus(message, tone) {
        if (!uploadStatus) {
            return;
        }
        uploadStatus.textContent = message;
        uploadStatus.className = "boombox-status";
        if (tone === "error") {
            uploadStatus.classList.add("is-error");
        } else if (tone === "success") {
            uploadStatus.classList.add("is-success");
        }
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

    function selectedFile() {
        if (!(fileInput instanceof HTMLInputElement) || !fileInput.files || fileInput.files.length === 0) {
            return null;
        }
        return fileInput.files[0] || null;
    }

    function validateFile(file) {
        const extensionIndex = file.name.lastIndexOf(".");
        const extension = extensionIndex >= 0 ? file.name.slice(extensionIndex).toLowerCase() : "";
        if (!allowedExtensions.includes(extension)) {
            return { valid: false, message: "Only MP3 and WAV files are allowed." };
        }
        if (maxFileBytes > 0 && file.size > maxFileBytes) {
            return {
                valid: false,
                message: `File is ${formatBytes(file.size)}. Limit is ${formatBytes(maxFileBytes)}.`,
            };
        }
        return {
            valid: true,
            message: `${file.name} is ready to upload (${formatBytes(file.size)}).`,
        };
    }

    function updateUploadButtons() {
        const hasSelection = selectedFile() !== null;
        if (browseButton instanceof HTMLButtonElement) {
            browseButton.disabled = submitting || libraryFull;
        }
        if (uploadButton instanceof HTMLButtonElement) {
            uploadButton.disabled = submitting || libraryFull || !hasSelection || !selectedFileIsValid;
        }
        if (clearButton instanceof HTMLButtonElement) {
            clearButton.disabled = submitting || !hasSelection;
        }
    }

    function clearSelection(options) {
        const silent = options && options.silent === true;
        if (fileInput instanceof HTMLInputElement) {
            fileInput.value = "";
        }
        selectedFileIsValid = false;
        if (selection) {
            selection.hidden = true;
            selection.classList.remove("is-invalid");
        }
        if (selectionName) {
            selectionName.textContent = "No file selected";
        }
        if (selectionMeta) {
            selectionMeta.textContent = "Choose one file to upload.";
        }
        if (selectionBadge) {
            selectionBadge.textContent = "Ready for review";
        }
        if (!silent && !libraryFull) {
            setStatus("Select a file to review its name and size before upload.", "");
        }
        updateUploadButtons();
    }

    function renderSelection(file, validation) {
        if (!selection || !selectionName || !selectionMeta || !selectionBadge) {
            return validation.valid;
        }
        selectedFileIsValid = validation.valid === true;
        selection.hidden = false;
        selectionName.textContent = file.name;
        selectionMeta.textContent = `${formatBytes(file.size)} • Tesla scans clips alphabetically.`;
        selection.classList.toggle("is-invalid", validation.valid !== true);
        selectionBadge.textContent = validation.valid ? "Ready to upload" : "Needs review";
        setStatus(validation.message, validation.valid ? "success" : "error");
        updateUploadButtons();
        return validation.valid;
    }

    function syncSelectionFeedback(options) {
        const quiet = options && options.quiet === true;
        const file = selectedFile();
        if (!file) {
            clearSelection({ silent: quiet });
            return false;
        }
        const validation = validateFile(file);
        if (!validation.valid && !quiet) {
            notify(validation.message, "error");
        }
        return renderSelection(file, validation);
    }

    function setSubmittingState(isSubmitting) {
        submitting = isSubmitting;
        updateUploadButtons();
        fileCheckboxes.forEach((checkbox) => {
            if (checkbox instanceof HTMLInputElement) {
                checkbox.disabled = isSubmitting;
            }
        });
        if (selectAll instanceof HTMLInputElement) {
            selectAll.disabled = isSubmitting || fileCheckboxes.length === 0;
        }
        confirmForms.forEach((form) => {
            Array.from(form.querySelectorAll("button")).forEach((button) => {
                if (button instanceof HTMLButtonElement) {
                    button.disabled = isSubmitting;
                }
            });
        });
        updateBulkSelection();
    }

    function checkedFileCount() {
        return fileCheckboxes.filter((checkbox) => checkbox instanceof HTMLInputElement && checkbox.checked).length;
    }

    function updateBulkSelection() {
        const selected = checkedFileCount();
        if (selectedCount) {
            selectedCount.textContent = `${selected} selected`;
        }
        if (bulkDeleteButton instanceof HTMLButtonElement) {
            bulkDeleteButton.disabled = submitting || selected === 0;
        }
        if (selectAll instanceof HTMLInputElement) {
            const total = fileCheckboxes.length;
            selectAll.checked = total > 0 && selected === total;
            selectAll.indeterminate = selected > 0 && selected < total;
        }
    }

    function handleFiles(fileList) {
        if (!(fileInput instanceof HTMLInputElement)) {
            return;
        }
        if (!fileList || fileList.length === 0) {
            clearSelection({ silent: false });
            return;
        }
        if (fileList.length > 1) {
            notify("Choose only one file at a time for Boombox uploads.", "error");
            clearSelection({ silent: true });
            setStatus("Choose only one MP3 or WAV file for each upload.", "error");
            return;
        }
        try {
            fileInput.files = fileList;
        } catch {
            clearSelection({ silent: true });
            setStatus("Your browser could not attach the dropped file. Use the browse button instead.", "error");
            return;
        }
        syncSelectionFeedback({ quiet: false });
    }

    if (browseButton instanceof HTMLButtonElement && fileInput instanceof HTMLInputElement) {
        browseButton.addEventListener("click", function() {
            if (!libraryFull && !submitting) {
                fileInput.click();
            }
        });

        ["dragenter", "dragover"].forEach((eventName) => {
            browseButton.addEventListener(eventName, function(event) {
                event.preventDefault();
                if (!libraryFull && !submitting) {
                    browseButton.classList.add("is-dragover");
                }
            });
        });

        ["dragleave", "dragend", "drop"].forEach((eventName) => {
            browseButton.addEventListener(eventName, function(event) {
                event.preventDefault();
                browseButton.classList.remove("is-dragover");
            });
        });

        browseButton.addEventListener("drop", function(event) {
            if (libraryFull || submitting) {
                return;
            }
            const files = event.dataTransfer ? event.dataTransfer.files : null;
            handleFiles(files);
        });

        fileInput.addEventListener("change", function() {
            syncSelectionFeedback({ quiet: false });
        });
    }

    if (clearButton instanceof HTMLButtonElement) {
        clearButton.addEventListener("click", function() {
            clearSelection({ silent: false });
        });
    }

    if (uploadForm instanceof HTMLFormElement) {
        uploadForm.addEventListener("submit", function(event) {
            if (libraryFull) {
                event.preventDefault();
                notify("The library is full. Delete a sound before uploading another one.", "error");
                setStatus("The library is full. Delete a sound before uploading another one.", "error");
                return;
            }
            if (!syncSelectionFeedback({ quiet: false })) {
                event.preventDefault();
                return;
            }
            setSubmittingState(true);
        });
    }

    fileCheckboxes.forEach((checkbox) => {
        if (checkbox instanceof HTMLInputElement) {
            checkbox.addEventListener("change", updateBulkSelection);
        }
    });

    if (selectAll instanceof HTMLInputElement) {
        selectAll.addEventListener("change", function() {
            fileCheckboxes.forEach((checkbox) => {
                if (checkbox instanceof HTMLInputElement && !checkbox.disabled) {
                    checkbox.checked = selectAll.checked;
                }
            });
            updateBulkSelection();
        });
    }

    if (bulkDeleteForm instanceof HTMLFormElement) {
        bulkDeleteForm.addEventListener("submit", function(event) {
            const selected = checkedFileCount();
            if (selected === 0) {
                event.preventDefault();
                notify("Select at least one sound to delete.", "error");
                return;
            }
            const confirmed = window.confirm(
                selected === 1
                    ? "Delete the selected Boombox sound?"
                    : `Delete ${selected} selected Boombox sounds?`,
            );
            if (!confirmed) {
                event.preventDefault();
                return;
            }
            setSubmittingState(true);
        });
    }

    confirmForms.forEach((form) => {
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        form.addEventListener("submit", function(event) {
            const message = form.dataset.confirm || "Delete this Boombox sound?";
            if (!window.confirm(message)) {
                event.preventDefault();
                return;
            }
            setSubmittingState(true);
        });
    });

    if (libraryFull) {
        setStatus("The library already has the maximum number of sounds. Delete one before uploading a replacement.", "error");
    }
    clearSelection({ silent: libraryFull });
    updateBulkSelection();
})();
