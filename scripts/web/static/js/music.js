(function () {
    const page = document.getElementById('music-page');
    if (!page) return;

    const chunkMb = parseInt(page.dataset.chunkMb || '8', 10) || 8;
    const maxMb = parseInt(page.dataset.maxMb || '0', 10) || 0;
    const uploadUrl = page.dataset.uploadUrl;
    const deleteTemplate = page.dataset.deleteUrl;
    const moveUrl = page.dataset.moveUrl;
    const browseUrl = page.dataset.browseUrl;
    const mkdirUrl = page.dataset.mkdirUrl;
    const currentPath = page.dataset.currentPath || '';
    const chunkSize = Math.max(1, chunkMb) * 1024 * 1024;
    const maxBytes = maxMb > 0 ? maxMb * 1024 * 1024 : null;

    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const startBtn = document.getElementById('startUpload');
    const clearBtn = document.getElementById('clearSelection');
    const statusEl = document.getElementById('uploadStatus');
    const fileTable = document.getElementById('fileTable');
    const folderTable = document.getElementById('folderTable');
    const folderNameInput = document.getElementById('folderName');
    const createFolderBtn = document.getElementById('createFolder');
    const currentPathLabel = document.getElementById('currentPathLabel');

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
                    <div class="meta">${formatBytes(item.file.size)} → /${item.targetPath || currentPath || ''}</div>
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

    function deriveTargetPath(file) {
        const rel = (file.webkitRelativePath || file.relativePath || '').replace(/^\/+/, '');
        const relDir = rel.includes('/') ? rel.substring(0, rel.lastIndexOf('/')) : '';
        if (currentPath && relDir) return `${currentPath}/${relDir}`;
        if (currentPath) return currentPath;
        return relDir;
    }

    function addFiles(files) {
        const allowed = ['.mp3', '.flac', '.wav', '.aac', '.m4a'];
        let skipped = 0;
        Array.from(files).forEach((file) => {
            if (file.name.startsWith('.')) { skipped += 1; return; }
            const lastDot = file.name.lastIndexOf('.');
            if (lastDot < 0) { skipped += 1; return; }
            const ext = file.name.substring(lastDot).toLowerCase();
            if (!allowed.includes(ext)) { skipped += 1; return; }
            if (maxBytes && file.size > maxBytes) {
                setStatus(`${file.name} exceeds ${maxMb} MiB limit`, true);
                return;
            }
            queue.push({ file, progressEl: null, targetPath: deriveTargetPath(file) });
        });
        if (queue.length > 0) {
            setStatus(`${queue.length} file(s) queued`, false);
        } else if (skipped > 0) {
            setStatus(`${skipped} item(s) skipped (unsupported type)`, true);
        }
        renderQueue();
    }

    function readEntriesAsync(dirReader) {
        return new Promise((resolve, reject) => {
            dirReader.readEntries((entries) => resolve(entries), reject);
        });
    }

    async function collectFromEntry(entry, basePath, out) {
        if (entry.isFile) {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            if (file.name.startsWith('.')) return;
            file.relativePath = basePath ? `${basePath}/${file.name}` : file.name;
            out.push(file);
            return;
        }
        if (entry.isDirectory) {
            const reader = entry.createReader();
            let entries;
            do {
                entries = await readEntriesAsync(reader);
                for (const child of entries) {
                    await collectFromEntry(child, basePath ? `${basePath}/${entry.name}` : entry.name, out);
                }
            } while (entries.length > 0);
        }
    }

    async function uploadFile(item) {
        const file = item.file;
        const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
        const uploadId = crypto.randomUUID ? crypto.randomUUID() : `upload-${Date.now()}-${Math.random()}`;
        const pathForFile = item.targetPath || currentPath || '';

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
            if (pathForFile) params.set('path', pathForFile);

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
            const target = currentPath ? `${browseUrl}?path=${encodeURIComponent(currentPath)}` : browseUrl;
            setTimeout(() => { window.location.href = target; }, 600);
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

    dropZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragging');

        const items = e.dataTransfer.items;
        if (items && items.length && items[0].webkitGetAsEntry) {
            const collected = [];
            for (const item of items) {
                const entry = item.webkitGetAsEntry();
                if (entry) {
                    await collectFromEntry(entry, '', collected);
                }
            }
            if (collected.length) {
                addFiles(collected);
                return;
            }
        }

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

    const navigateTo = (path) => {
        const target = path ? `${browseUrl}?path=${encodeURIComponent(path)}` : browseUrl;
        window.location.href = target;
    };

    // File actions: delete or move
    if (fileTable) {
        fileTable.addEventListener('click', async (e) => {
            const target = e.target;
            const filename = target.getAttribute('data-delete');
            const moveTarget = target.getAttribute('data-move');
            if (!filename && !moveTarget) return;
            e.preventDefault();

            if (filename) {
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
                return;
            }

            if (moveTarget) {
                const dest = window.prompt('Move to folder (relative path):', currentPath);
                if (dest === null) return;
                const newName = window.prompt('Optional new filename (leave blank to keep name):', '');
                target.disabled = true;
                const res = await fetch(moveUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify({ source: moveTarget, dest_path: dest || '', new_name: newName || '' }),
                });
                let data;
                try {
                    data = await res.json();
                } catch (err) {
                    target.disabled = false;
                    setStatus('Move failed: bad response', true);
                    return;
                }
                target.disabled = false;
                if (!res.ok || !data.success) {
                    setStatus(data && data.error ? data.error : 'Move failed', true);
                    return;
                }
                const targetPath = dest ? `${browseUrl}?path=${encodeURIComponent(dest)}` : browseUrl;
                window.location.href = targetPath;
            }
        });
    }

    if (folderTable) {
        folderTable.addEventListener('click', (e) => {
            const row = e.target.closest('tr');
            if (!row) return;
            const dir = row.getAttribute('data-dir');
            if (dir !== null) {
                navigateTo(dir);
            }
        });
    }

    if (createFolderBtn) {
        createFolderBtn.addEventListener('click', async () => {
            const name = (folderNameInput.value || '').trim();
            if (!name) {
                setStatus('Folder name required', true);
                return;
            }
            createFolderBtn.disabled = true;
            const res = await fetch(mkdirUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
                body: JSON.stringify({ path: currentPath, name }),
            });
            let data;
            try {
                data = await res.json();
            } catch (err) {
                createFolderBtn.disabled = false;
                setStatus('Create folder failed: bad response', true);
                return;
            }
            createFolderBtn.disabled = false;
            if (!res.ok || !data.success) {
                setStatus(data && data.error ? data.error : 'Create folder failed', true);
                return;
            }
            const target = currentPath ? `${browseUrl}?path=${encodeURIComponent(currentPath)}` : browseUrl;
            window.location.href = target;
        });
    }

    if (currentPathLabel) {
        const labelPath = currentPath ? `/${currentPath}` : '/';
        currentPathLabel.textContent = labelPath;
    }
})();
