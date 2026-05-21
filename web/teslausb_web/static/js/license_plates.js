const page = document.getElementById("licensePlatesPage");

if (page instanceof HTMLElement) {
    const matchUrl = page.dataset.matchUrl || "";
    const filterInput = document.getElementById("platesFilter");
    const selectAll = document.getElementById("platesSelectAll");
    const selectedCount = document.getElementById("platesSelectedCount");
    const bulkDeleteButton = document.getElementById("platesBulkDeleteButton");
    const matchForm = document.getElementById("plateMatchForm");
    const matchButton = document.getElementById("plateMatchButton");
    const matchResult = document.getElementById("plateMatchResult");
    const cardNodes = Array.from(document.querySelectorAll(".js-plate-card"));
    const checkboxes = Array.from(document.querySelectorAll(".js-plate-select"));
    const confirmForms = Array.from(document.querySelectorAll(".js-plate-confirm"));

    function notify(message, type) {
        if (typeof window.showToast === "function") {
            window.showToast(message, type);
            return;
        }
        window.alert(message);
    }

    function visibleCheckboxes() {
        return checkboxes.filter((checkbox) => {
            if (!(checkbox instanceof HTMLInputElement)) {
                return false;
            }
            const card = checkbox.closest(".js-plate-card");
            return card instanceof HTMLElement && !card.hidden;
        });
    }

    function checkedCount() {
        return visibleCheckboxes().filter((checkbox) => checkbox.checked).length;
    }

    function updateSelectionState() {
        if (!(selectedCount instanceof HTMLElement)) {
            return;
        }
        const visible = visibleCheckboxes();
        const selected = checkedCount();
        selectedCount.textContent = `${selected} selected`;
        if (bulkDeleteButton instanceof HTMLButtonElement) {
            bulkDeleteButton.disabled = selected === 0;
        }
        if (selectAll instanceof HTMLInputElement) {
            selectAll.checked = visible.length > 0 && selected === visible.length;
            selectAll.indeterminate = selected > 0 && selected < visible.length;
            selectAll.disabled = visible.length === 0;
        }
    }

    function applyFilter() {
        const query = filterInput instanceof HTMLInputElement ? filterInput.value.trim().toLowerCase() : "";
        cardNodes.forEach((node) => {
            if (!(node instanceof HTMLElement)) {
                return;
            }
            const haystack = (node.dataset.filterText || "").toLowerCase();
            node.hidden = query.length > 0 && !haystack.includes(query);
            if (node.hidden) {
                const checkbox = node.querySelector(".js-plate-select");
                if (checkbox instanceof HTMLInputElement) {
                    checkbox.checked = false;
                }
            }
        });
        updateSelectionState();
    }

    if (filterInput instanceof HTMLInputElement) {
        filterInput.addEventListener("input", applyFilter);
    }

    if (selectAll instanceof HTMLInputElement) {
        selectAll.addEventListener("change", function() {
            const checked = selectAll.checked;
            visibleCheckboxes().forEach((checkbox) => {
                checkbox.checked = checked;
            });
            updateSelectionState();
        });
    }

    checkboxes.forEach((checkbox) => {
        if (checkbox instanceof HTMLInputElement) {
            checkbox.addEventListener("change", updateSelectionState);
        }
    });

    confirmForms.forEach((form) => {
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        form.addEventListener("submit", function(event) {
            const message = form.dataset.confirm;
            if (message && !window.confirm(message)) {
                event.preventDefault();
            }
        });
    });

    const bulkDeleteForm = document.getElementById("platesBulkDeleteForm");
    if (bulkDeleteForm instanceof HTMLFormElement) {
        bulkDeleteForm.addEventListener("submit", function(event) {
            if (checkedCount() === 0) {
                event.preventDefault();
                notify("Select at least one tracked plate to delete.", "error");
                return;
            }
            if (!window.confirm("Delete the selected tracked plates?")) {
                event.preventDefault();
            }
        });
    }

    function setMatchResult(message, tone) {
        if (!(matchResult instanceof HTMLElement)) {
            return;
        }
        matchResult.textContent = message;
        matchResult.className = "plates-match-result";
        if (tone === "success") {
            matchResult.classList.add("is-success");
        } else if (tone === "warning") {
            matchResult.classList.add("is-warning");
        }
    }

    async function submitMatch(candidate) {
        if (!matchUrl) {
            setMatchResult("Match endpoint is unavailable.", "warning");
            return;
        }
        if (matchButton instanceof HTMLButtonElement) {
            matchButton.disabled = true;
        }
        setMatchResult("Checking plate normalization…", "");
        try {
            const response = await window.fetch(matchUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({ candidate }),
            });
            const payload = await response.json();
            if (!response.ok || payload.success !== true) {
                const message = typeof payload.error === "string" ? payload.error : "Could not check plate.";
                setMatchResult(message, "warning");
                return;
            }
            const match = payload.match;
            if (match && match.is_match === true && match.matched_plate) {
                setMatchResult(
                    `${match.normalized_candidate} matches ${match.matched_plate.plate_text}.`,
                    "success",
                );
                return;
            }
            setMatchResult(`${match.normalized_candidate} is not tracked.`, "warning");
        } catch {
            setMatchResult("Could not contact the server.", "warning");
        } finally {
            if (matchButton instanceof HTMLButtonElement) {
                matchButton.disabled = false;
            }
        }
    }

    if (matchForm instanceof HTMLFormElement) {
        matchForm.addEventListener("submit", function(event) {
            event.preventDefault();
            const formData = new window.FormData(matchForm);
            const candidate = String(formData.get("candidate") || "").trim();
            if (!candidate) {
                setMatchResult("Enter a plate to check.", "warning");
                return;
            }
            void submitMatch(candidate);
        });
    }

    applyFilter();
}
