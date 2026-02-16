(function () {
    const page = document.getElementById('music-page');
    if (!page) return;

    const chunkMb = parseInt(page.dataset.chunkMb || '8', 10) || 8;
    const maxMb = parseInt(page.dataset.maxMb || '0', 10) || 0;
    const uploadUrl = page.dataset.uploadUrl;
    const deleteTemplate = page.dataset.deleteUrl;
    const chunkSize = Math.max(1, chunkMb) * 1024 * 1024;
    const maxBytes = maxMb > 0 ? maxMb * 1024 * 1024 : null;

    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const startBtn = document.getElementById('startUpload');
    const clearBtn = document.getElementById('clearSelection');
    const statusEl = document.getElementById('uploadStatus');
    const table = page.querySelector('table');

    let queue = [];
    let uploading = false;

    function setStatus(msg, isError) {
        statusEl.textContent = msg || '';
        statusEl.className = isError ? 'error' : 'muted';
    }

    function renderQueue() {
        fileList.innerHTML = '';
        queue.forEach((item, idx) => {
            const row = document.createElement('div');
            row.className = 'file-row';
            row.innerHTML = `
                <div>
                    <div>${item.file.name}</div>
                    <div class="meta">${formatBytes(item.file.size)}</div>
                </div>
                <button class="secondary" data-remove="${idx}">Remove</button>
            `;
            const progress = document.createElement('div');
            progress.className = 'progress-bar';
            const bar = document.createElement('span');
            progress.appendChild(bar);
            row.appendChild(progress);
            item.progressEl = bar;
            fileList.appendChild(row);
        });
        const disabled = queue.length === 0 || uploading;
        startBtn.disabled = disabled;
        clearBtn.disabled = queue.length === 0 || uploading;
    }

    function formatBytes(bytes) {
        if (!Number.isFinite(bytes)) return '0 B';
        if (bytes < 1024) return `${bytes} B`;
        const units = ['KB', 'MB', 'GB'];
        let val = bytes;
        let i = 0;
        while (val >= 1024 && i < units.length) {
            val /= 1024;
            i += 1;
        }
        return `${val.toFixed(val >= 10 ? 0 : 1)} ${units[i - 1] || 'KB'}`;
    }

    function addFiles(files) {
        const allowed = ['.mp3', '.flac', '.wav', '.aac', '.m4a'];
        Array.from(files).forEach((file) => {
            const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
            if (!allowed.includes(ext)) {
                setStatus(`${file.name} skipped (unsupported type)`, true);
                return;
            }
            if (maxBytes && file.size > maxBytes) {
                setStatus(`${file.name} exceeds ${maxMb} MiB limit`, true);
                return;
            }
            queue.push({ file, progressEl: null });
        });
        if (queue.length > 0) {
            setStatus(`${queue.length} file(s) queued`, false);
        }
        renderQueue();
    }

    async function uploadFile(item) {
        const file = item.file;
        const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
        const uploadId = crypto.randomUUID ? crypto.randomUUID() : `upload-${Date.now()}-${Math.random()}`;

        for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex += 1) {
            const start = chunkIndex * chunkSize;
            const end = Math.min(start + chunkSize, file.size);
            const chunk = file.slice(start, end);

            const params = new URLSearchParams({
                upload_id: uploadId,
                filename: file.name,
                chunk_index: String(chunkIndex),
                total_chunks: String(totalChunks),
                total_size: String(file.size),
            });

            const res = await fetch(`${uploadUrl}?${params.toString()}`, {
                method: 'POST',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-Type': 'application/octet-stream',
                    'X-File-Size': String(file.size),
                },
                body: chunk,
            });

            let data;
            try {
                data = await res.json();
            } catch (e) {
                setStatus('Upload failed: invalid server response', true);
                return false;
            }

            if (!res.ok || !data.success) {
                const message = data && data.error ? data.error : 'Upload failed';
                setStatus(`${file.name}: ${message}`, true);
                return false;
            }

            const pct = Math.round(((chunkIndex + 1) / totalChunks) * 100);
            if (item.progressEl) item.progressEl.style.width = `${pct}%`;
        }

        setStatus(`${file.name} uploaded`, false);
        return true;
    }

    async function uploadQueue() {
        if (queue.length === 0 || uploading) return;
        uploading = true;
        startBtn.disabled = true;
        clearBtn.disabled = true;

        for (const item of queue) {
            if (item.progressEl) item.progressEl.style.width = '0%';
            const ok = await uploadFile(item);
            if (!ok) break;
        }

        uploading = false;
        startBtn.disabled = queue.length === 0;
        clearBtn.disabled = queue.length === 0;
        // Refresh page to show new files
        if (!statusEl.classList.contains('error')) {
            setTimeout(() => window.location.reload(), 600);
        }
    }

    function clearQueue() {
        if (uploading) return;
        queue = [];
        fileInput.value = '';
        renderQueue();
        setStatus('', false);
    }

    dropZone.addEventListener('click', () => fileInput.click());

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragging');
    });

    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragging'));

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragging');
        if (e.dataTransfer.files) {
            addFiles(e.dataTransfer.files);
        }
    });

    fileInput.addEventListener('change', (e) => {
        addFiles(e.target.files);
    });

    fileList.addEventListener('click', (e) => {
        const removeIdx = e.target.getAttribute('data-remove');
        if (removeIdx !== null) {
            queue.splice(Number(removeIdx), 1);
            renderQueue();
            return;
        }
    });

    startBtn.addEventListener('click', () => uploadQueue());
    clearBtn.addEventListener('click', () => clearQueue());

    // Delete handling
    if (table) {
        table.addEventListener('click', async (e) => {
            const target = e.target;
            const filename = target.getAttribute('data-delete');
            if (!filename) return;
            e.preventDefault();
            const url = deleteTemplate.replace('__NAME__', encodeURIComponent(filename));
            target.disabled = true;
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            let data;
            try {
                data = await res.json();
            } catch (err) {
                target.disabled = false;
                setStatus('Delete failed: bad response', true);
                return;
            }
            if (!res.ok || !data.success) {
                target.disabled = false;
                setStatus(data && data.error ? data.error : 'Delete failed', true);
                return;
            }
            const row = target.closest('tr');
            if (row) row.remove();
            setStatus(data.message || 'Deleted', false);
        });
    }
})();
