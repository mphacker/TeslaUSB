(function () {
    const page = document.getElementById('music-page');
    if (!page) return;

    const chunkMb = parseInt(page.dataset.chunkMb || '8', 10) || 8;
    const maxMb = parseInt(page.dataset.maxMb || '0', 10) || 0;
    const uploadUrl = page.dataset.uploadUrl;
    const deleteTemplate = page.dataset.deleteUrl;
    const deleteDirTemplate = page.dataset.deleteDirUrl;
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
    const mobileFolderList = document.getElementById('mobileFolderList');
    const mobileFileList = document.getElementById('mobileFileList');
    const folderNameInput = document.getElementById('folderName');
    const createFolderBtn = document.getElementById('createFolder');
    const currentPathLabel = document.getElementById('currentPathLabel');

    const overlay = document.getElementById('musicLoadingOverlay');
    const overlayText = document.getElementById('musicOverlayText');
    let queue = [];
    let uploading = false;

    function showOverlay(msg) {
        if (overlayText) overlayText.textContent = msg || 'Processing...';
        if (overlay) overlay.style.display = 'flex';
    }

    function hideOverlay() {
        if (overlay) overlay.style.display = 'none';
    }

    function setStatus(msg, isError) {
        statusEl.textContent = msg || '';
        statusEl.className = isError ? 'music-status error' : 'music-status';
    }

    function renderQueue() {
        fileList.innerHTML = '';
        queue.forEach((item, idx) => {
            const row = document.createElement('div');
            row.className = 'music-file-row';
            row.innerHTML = `
                <div>
                    <div>${item.file.name}</div>
                    <div class="meta">${formatBytes(item.file.size)} → /${item.targetPath || currentPath || ''}</div>
                </div>
                <button class="action-btn btn-danger" data-remove="${idx}">Remove</button>
            `;
            const progress = document.createElement('div');
            progress.className = 'music-progress-bar';
            const bar = document.createElement('span');
            progress.appendChild(bar);
            row.appendChild(progress);
            const status = document.createElement('div');
            status.className = 'meta';
            status.textContent = 'Waiting';
            row.appendChild(status);
            item.progressEl = bar;
            item.statusEl = status;
            item.rowEl = row;
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
        const uploadId = (crypto.randomUUID ? crypto.randomUUID() : `upload-${Date.now()}-${Math.random()}`).replaceAll('-', '').replace(/[^0-9a-f]/gi, '').slice(0, 32).padEnd(32, '0');
        const pathForFile = item.targetPath || currentPath || '';

        if (item.statusEl) item.statusEl.textContent = 'Starting...';
        if (item.rowEl) item.rowEl.classList.remove('uploaded');
        if (item.progressEl) item.progressEl.style.width = '0%';

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
                if (item.statusEl) item.statusEl.textContent = 'Failed';
                setStatus('Upload failed: invalid server response', true);
                return false;
            }

            if (!res.ok || !data.success) {
                const message = data && data.error ? data.error : 'Upload failed';
                if (item.statusEl) item.statusEl.textContent = 'Failed';
                setStatus(`${file.name}: ${message}`, true);
                return false;
            }

            const pct = Math.round(((chunkIndex + 1) / totalChunks) * 100);
            if (item.progressEl) item.progressEl.style.width = `${pct}%`;
            if (item.statusEl) item.statusEl.textContent = `${pct}%`;
        }

        if (item.progressEl) item.progressEl.style.width = '100%';
        if (item.statusEl) item.statusEl.textContent = 'Uploaded';
        if (item.rowEl) item.rowEl.classList.add('uploaded');
        setStatus(`${file.name} uploaded`, false);
        return true;
    }

    async function uploadQueue() {
        if (queue.length === 0 || uploading) return;
        uploading = true;
        startBtn.disabled = true;
        clearBtn.disabled = true;
        showOverlay('Uploading files...');

        for (const item of queue) {
            if (item.progressEl) item.progressEl.style.width = '0%';
            const ok = await uploadFile(item);
            if (!ok) break;
        }

        uploading = false;
        hideOverlay();
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
    async function handleDeleteFile(target, filename) {
        if (!confirm(`Delete "${filename}"?`)) return;
        const url = deleteTemplate.replace('__NAME__', encodeURIComponent(filename));
        target.disabled = true;
        showOverlay('Deleting file...');
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        let data;
        try {
            data = await res.json();
        } catch (err) {
            hideOverlay();
            target.disabled = false;
            setStatus('Delete failed: bad response', true);
            return;
        }
        hideOverlay();
        if (!res.ok || !data.success) {
            target.disabled = false;
            setStatus(data && data.error ? data.error : 'Delete failed', true);
            return;
        }
        // Remove from both table and mobile views
        document.querySelectorAll(`tr[data-filename="${filename}"]`).forEach(el => el.remove());
        document.querySelectorAll(`.music-mobile-file[data-filename="${filename}"]`).forEach(el => el.remove());
        setStatus(data.message || 'Deleted', false);
    }

    async function handleMoveFile(target, moveSource) {
        const dest = window.prompt('Move to folder (relative path):', currentPath);
        if (dest === null) return;
        const newName = window.prompt('Optional new filename (leave blank to keep name):', '');
        target.disabled = true;
        showOverlay('Moving file...');
        const res = await fetch(moveUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            },
            body: JSON.stringify({ source: moveSource, dest_path: dest || '', new_name: newName || '' }),
        });
        let data;
        try {
            data = await res.json();
        } catch (err) {
            hideOverlay();
            target.disabled = false;
            setStatus('Move failed: bad response', true);
            return;
        }
        hideOverlay();
        target.disabled = false;
        if (!res.ok || !data.success) {
            setStatus(data && data.error ? data.error : 'Move failed', true);
            return;
        }
        const targetPath = dest ? `${browseUrl}?path=${encodeURIComponent(dest)}` : browseUrl;
        window.location.href = targetPath;
    }

    async function handleDeleteDir(target, deleteDir) {
        if (!confirm(`Delete folder "${deleteDir}" and all its contents?`)) return;
        if (!deleteDirTemplate) return;
        target.disabled = true;
        showOverlay('Deleting folder...');
        const url = deleteDirTemplate.replace('__DIR__', encodeURIComponent(deleteDir));
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        let data;
        try {
            data = await res.json();
        } catch (err) {
            hideOverlay();
            target.disabled = false;
            setStatus('Delete folder failed: bad response', true);
            return;
        }
        hideOverlay();
        target.disabled = false;
        if (!res.ok || !data.success) {
            setStatus(data && data.error ? data.error : 'Delete folder failed', true);
            return;
        }
        // Remove from both table and mobile views
        document.querySelectorAll(`tr[data-dir="${deleteDir}"]`).forEach(el => el.remove());
        document.querySelectorAll(`.music-mobile-folder[data-dir="${deleteDir}"]`).forEach(el => el.remove());
        setStatus(data.message || 'Folder deleted', false);
    }

    function handleFileClick(e) {
        const target = e.target;
        const filename = target.getAttribute('data-delete');
        const moveTarget = target.getAttribute('data-move');
        if (!filename && !moveTarget) return;
        e.preventDefault();
        if (filename) return handleDeleteFile(target, filename);
        if (moveTarget) return handleMoveFile(target, moveTarget);
    }

    function handleFolderClick(e) {
        const target = e.target;
        const deleteDir = target.getAttribute('data-delete-dir');
        if (deleteDir) {
            e.preventDefault();
            e.stopPropagation();
            return handleDeleteDir(target, deleteDir);
        }
        // Navigate on row/card click
        const container = target.closest('tr[data-dir]') || target.closest('.music-mobile-folder[data-dir]');
        if (!container) return;
        const dir = container.getAttribute('data-dir');
        if (dir !== null) navigateTo(dir);
    }

    if (fileTable) {
        fileTable.addEventListener('click', handleFileClick);
    }
    if (mobileFileList) {
        mobileFileList.addEventListener('click', handleFileClick);
    }
    if (folderTable) {
        folderTable.addEventListener('click', handleFolderClick);
    }
    if (mobileFolderList) {
        mobileFolderList.addEventListener('click', handleFolderClick);
    }

    if (createFolderBtn) {
        createFolderBtn.addEventListener('click', async () => {
            const name = (folderNameInput.value || '').trim();
            if (!name) {
                setStatus('Folder name required', true);
                return;
            }
            createFolderBtn.disabled = true;
            showOverlay('Creating folder...');
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
                hideOverlay();
                createFolderBtn.disabled = false;
                setStatus('Create folder failed: bad response', true);
                return;
            }
            hideOverlay();
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
