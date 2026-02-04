/* ------------------------------------------------------------------
   P2P File Sharing - Production Version
   ------------------------------------------------------------------ */

// Wait for DOM to be ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initApp);
} else {
    initApp();
}

function initApp() {
    /* ------------------------------------------------------------------
       GLOBAL CONSTANTS & STATE
       ------------------------------------------------------------------ */
    window.room = window.location.pathname.split('/').filter(p=>p)[0] || "main";
    
    const roomNameEl = document.getElementById("roomName");
    const roomDisplayEl = document.getElementById("roomDisplay");
    const currentUserEl = document.getElementById("currentUser");
    
    if (roomNameEl) roomNameEl.textContent = window.room;
    if (roomDisplayEl) roomDisplayEl.textContent = window.room;

    // DOM refs
    window.progressArea = document.getElementById("progressArea");
    window.filesDiv = document.getElementById("files");
    window.chatBox = document.getElementById("chatBox");

    // auth
    window.authToken = localStorage.getItem(`auth_token_${window.room}`) || "";
    window.authenticated = false;
    window.currentUser = localStorage.getItem('archive_username') || "Anonymous";
    if (currentUserEl) currentUserEl.textContent = window.currentUser;

    // keep uploaded files in memory for P2P sharing
    window.myFiles = {};

    // peer‑tracking for P2P
    window.myPeerId = "";
    window.peers = {};
    window.filePeers = {};
    window.pendingChunkRequests = {};
    window.CHUNK_SIZE = 4 * 1024 * 1024;

    // WS connections
    window.updateWs = null;
    window.chatWs = null;
    window.signalWs = null;

    // Set up event listeners
    setupEventListeners();
    
    // Bootstrap
    bootstrapApp();
}

function setupEventListeners() {
    const fileInput = document.getElementById('fileInput');
    const searchInput = document.getElementById('searchInput');
    const streamSearch = document.getElementById('streamSearch');
    const dropzone = document.getElementById('dropzone');
    
    if (fileInput) {
        fileInput.addEventListener('change', handleFileSelect);
    }
    
    if (searchInput) {
        searchInput.addEventListener('input', e => renderArchiveList(e.target.value));
    }
    
    if (streamSearch) {
        streamSearch.addEventListener('input', e => {
            if (window.currentFolderData) renderStreamFolder(window.currentFolderData);
        });
    }
    
    if (dropzone) {
        dropzone.addEventListener('dragover', e => e.preventDefault());
        dropzone.addEventListener('drop', handleDrop);
    }
}

function handleFileSelect(e) {
    if (!window.authenticated) {
        alert("You must authenticate first – type pass:YOUR_PASSWORD in the chat.");
        e.target.value = "";
        return;
    }
    Array.from(e.target.files).forEach(file => uploadFile(file));
    e.target.value = "";
}

function handleDrop(e) {
    e.preventDefault();
    if (!window.authenticated) {
        alert("You must authenticate first.");
        return;
    }
    for (const file of e.dataTransfer.files) uploadFile(file);
}

/* ------------------------------------------------------------------
   Helper UI utilities
   ------------------------------------------------------------------ */
function getWsProtocol() {
    return location.protocol === 'https:' ? 'wss:' : 'ws:';
}

function humanFileSize(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) {
        bytes /= 1024;
        i++;
    }
    return `${bytes.toFixed(1)} ${units[i]}`;
}

function showTab(tabId, btn) {
    document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    const tab = document.getElementById(tabId);
    if (tab) tab.classList.add('active');
    if (btn) btn.classList.add('active');
}

function createProgressBar(text) {
    const div = document.createElement('div');
    div.className = "progress-item";
    div.innerHTML = `<div>${text}</div>
                   <div class="progress-bar-bg"><div class="bar"></div></div>`;
    window.progressArea.appendChild(div);
    return div;
}

function updateProgressBar(container, cur, total) {
    const pct = Math.round(cur / total * 100);
    const bar = container.querySelector('.bar');
    if (bar) {
        bar.style.width = pct + "%";
    }
    if (pct >= 100) {
        setTimeout(() => container.remove(), 1500);
    }
}

function addSystemMessage(msg) {
    const p = document.createElement('p');
    p.textContent = msg;
    p.style.fontStyle = "italic";
    p.style.color = "#555";
    window.chatBox.appendChild(p);
    window.chatBox.scrollTop = window.chatBox.scrollHeight;
}

/* ------------------------------------------------------------------
   Auth & password flow
   ------------------------------------------------------------------ */
async function initAuth() {
    try {
        const pwdResp = await fetch(`/room_password?room=${encodeURIComponent(window.room)}`);
        const pwdData = await pwdResp.json();
        
        if (!pwdData.required) {
            setAuthenticated(true);
            return;
        }

        if (window.authToken) {
            try {
                const test = await fetch(`/list?room=${encodeURIComponent(window.room)}&auth_token=${encodeURIComponent(window.authToken)}`);
                if (test.ok) {
                    setAuthenticated(true);
                    return;
                }
            } catch (err) {
                console.warn('Token validation failed:', err);
            }
        }

        setAuthenticated(false);
    } catch (err) {
        console.error('Auth init failed:', err);
        setAuthenticated(false);
    }
}

function setAuthenticated(val) {
    window.authenticated = val;
    const fileInput = document.getElementById('fileInput');
    const msgInput = document.getElementById('msgInput');

    if (fileInput) fileInput.disabled = !val;
    if (msgInput) {
        msgInput.placeholder = val ? "Type a message…" : "Enter password: pass:YOUR_PASSWORD";
    }
}

/* ------------------------------------------------------------------
   Chat
   ------------------------------------------------------------------ */
function changeUsername() {
    const inp = document.getElementById('usernameInput');
    if (!inp) return;
    const val = inp.value.trim() || "Anonymous";
    window.currentUser = val;
    localStorage.setItem('archive_username', window.currentUser);
    const userDisplay = document.getElementById('currentUser');
    if (userDisplay) userDisplay.textContent = window.currentUser;
}

function sendMessage() {
    const inp = document.getElementById('msgInput');
    if (!inp) return;
    const text = inp.value.trim();
    if (!text) return;

    if (!window.authenticated && !text.toLowerCase().startsWith("pass:")) {
        addSystemMessage("You must authenticate first – type pass:YOUR_PASSWORD");
        return;
    }

    if (!window.authenticated && text.toLowerCase().startsWith("pass:")) {
        if (window.chatWs && window.chatWs.readyState === WebSocket.OPEN) {
            window.chatWs.send(text);
            inp.value = "";
        } else {
            addSystemMessage("Chat connection not ready. Please wait...");
        }
        return;
    }

    if (window.chatWs && window.chatWs.readyState === WebSocket.OPEN) {
        window.chatWs.send(`${window.currentUser}: ${text}`);
        inp.value = "";
    } else {
        addSystemMessage("Chat connection lost. Reconnecting...");
    }
}

/* ------------------------------------------------------------------
   Archive
   ------------------------------------------------------------------ */
window.archiveFileList = [];

async function refreshArchiveList() {
    try {
        const rsp = await fetch(`/list?room=${encodeURIComponent(window.room)}${window.authToken ? `&auth_token=${encodeURIComponent(window.authToken)}` : ''}`);
        if (!rsp.ok) return;
        const data = await rsp.json();
        window.archiveFileList = data.files.sort((a, b) => a.name.localeCompare(b.name));
        renderArchiveList();
    } catch (err) {
        console.error('Archive list error:', err);
    }
}

function renderArchiveList(filter = "") {
    const lower = filter.toLowerCase();
    window.filesDiv.innerHTML = "";
    const filtered = window.archiveFileList.filter(f => f.name.toLowerCase().includes(lower));
    
    filtered.forEach(f => {
        const item = document.createElement('div');
        item.className = "file-item";
        const safeName = f.name.replace(/'/g, "\\'");
        item.innerHTML = `<div class="file-info"><div class="file-name">${f.name}</div>
                       <div class="file-meta">${humanFileSize(f.size)}</div></div>
                       <div>
                         <button class="btn-blue" onclick="downloadP2P('${safeName}','archive')">P2P</button>
                         <button class="btn-red" onclick="deleteFile('${safeName}')">🗑</button>
                         <button class="btn-blue" onclick="downloadLegacy('${safeName}','archive')">Legacy</button>
                       </div>`;
        window.filesDiv.appendChild(item);
    });
}

async function deleteFile(name) {
    if (!window.authenticated) {
        alert("You must authenticate first.");
        return;
    }
    if (!confirm(`Delete ${name}?`)) return;

    try {
        const delUrl = `/delete/${encodeURIComponent(window.room)}/${encodeURIComponent(name)}${window.authToken ? `?auth_token=${encodeURIComponent(window.authToken)}` : ''}`;
        const delResp = await fetch(delUrl, { method: 'DELETE' });
        if (!delResp.ok) {
            const err = await delResp.json();
            alert(err.detail || "Delete failed");
            return;
        }
        refreshArchiveList();
    } catch (err) {
        console.error('Delete error:', err);
        alert('Delete failed: ' + err.message);
    }
}

/* ------------------------------------------------------------------
   Upload
   ------------------------------------------------------------------ */
function rememberLocalFile(file) {
    window.myFiles[file.name] = file;
}

async function uploadFile(file) {
    rememberLocalFile(file);

    const totalChunks = Math.ceil(file.size / window.CHUNK_SIZE);
    const prog = createProgressBar(`Uploading ${file.name}…`);
    const abortCtrl = new AbortController();

    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = "Cancel";
    cancelBtn.className = "btn-red";
    cancelBtn.style.marginLeft = "10px";
    cancelBtn.onclick = () => {
        abortCtrl.abort();
        fetch(`/cancel_upload?room=${encodeURIComponent(window.room)}&filename=${encodeURIComponent(file.name)}`, { method: "POST" })
            .catch(err => console.warn('Cancel upload error:', err));
        prog.remove();
    };
    prog.appendChild(cancelBtn);

    for (let i = 0; i < totalChunks; i++) {
        const chunk = file.slice(i * window.CHUNK_SIZE, (i + 1) * window.CHUNK_SIZE);
        const form = new FormData();
        form.append("file", chunk);
        const url = `/upload_chunk?room=${encodeURIComponent(window.room)}&filename=${encodeURIComponent(file.name)}&chunk_index=${i}&total_chunks=${totalChunks}` +
            (window.authToken ? `&auth_token=${encodeURIComponent(window.authToken)}` : '');
        try {
            const resp = await fetch(url, { method: "POST", body: form, signal: abortCtrl.signal });
            if (!resp.ok) {
                const err = await resp.json();
                alert(err.detail || "Upload failed");
                prog.remove();
                return;
            }
        } catch (e) {
            if (e.name === "AbortError") return;
            console.error('Upload error:', e);
            prog.remove();
            return;
        }
        updateProgressBar(prog, i + 1, totalChunks);
    }

    if (window.signalWs && window.signalWs.readyState === WebSocket.OPEN && window.myPeerId) {
        window.signalWs.send(JSON.stringify({
            type: "file_available",
            filename: file.name,
            total_chunks: totalChunks,
            size: file.size,
            peer_id: window.myPeerId
        }));
    }

    refreshArchiveList();
}

/* ------------------------------------------------------------------
   STREAMS
   ------------------------------------------------------------------ */
window.currentDrive = "";
window.currentPath = "";
window.currentFolderData = null;
window.firstDriveLoad = true;

async function populateDriveSelect() {
    try {
        const rsp = await fetch("/streams/drives");
        const data = await rsp.json();
        const sel = document.getElementById('driveSelect');
        if (!sel) return;

        const previous = sel.value;
        sel.innerHTML = "";
        let anyMissing = false;
        
        data.drives.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.mount;
            opt.textContent = d.display + (d.available ? "" : " (missing)");
            opt.disabled = !d.available;
            sel.appendChild(opt);
            if (!d.available) anyMissing = true;
        });

        if (previous && [...sel.options].some(o => o.value === previous && !o.disabled)) {
            sel.value = previous;
        } else {
            const stored = localStorage.getItem('last_drive');
            if (stored && [...sel.options].some(o => o.value === stored && !o.disabled)) {
                sel.value = stored;
            } else if (sel.options.length > 0) {
                sel.selectedIndex = 0;
            }
        }

        if (sel.value) {
            localStorage.setItem('last_drive', sel.value);
        }

        const statusDiv = document.getElementById('driveStatus');
        if (statusDiv) {
            if (anyMissing) {
                statusDiv.textContent = "⚠️ One or more drives are missing – check server console.";
                statusDiv.classList.remove('hidden');
            } else {
                statusDiv.classList.add('hidden');
            }
        }

        if (window.firstDriveLoad) {
            listStreamFiles();
            window.firstDriveLoad = false;
        }
    } catch (err) {
        console.error('Drive list error:', err);
    }
}

function driveChanged() {
    const sel = document.getElementById('driveSelect');
    if (sel && sel.value) {
        localStorage.setItem('last_drive', sel.value);
        listStreamFiles();
    }
}

function listStreamFiles() {
    const sel = document.getElementById('driveSelect');
    if (!sel) return;
    window.currentDrive = sel.value;
    window.currentPath = "";
    loadStreamFolder();
}

async function loadStreamFolder() {
    if (!window.currentDrive) return;
    try {
        const url = `/streams/browse?drive=${encodeURIComponent(window.currentDrive)}&dir_path=${encodeURIComponent(window.currentPath)}`;
        const rsp = await fetch(url);
        if (!rsp.ok) {
            const txt = await rsp.text();
            alert(`Failed to load folder: ${txt}`);
            return;
        }
        const data = await rsp.json();
        window.currentFolderData = data;
        renderStreamFolder(data);
    } catch (err) {
        console.error('Load folder error:', err);
        alert('Failed to load folder: ' + err.message);
    }
}

function renderStreamFolder(data) {
    const container = document.getElementById('streamFiles');
    if (!container) return;
    container.innerHTML = "";

    const bc = document.getElementById('breadcrumb');
    if (bc) {
        const parts = data.current ? data.current.split('/') : [];
        let crumbHtml = `<a href="javascript:void(0)" onclick="gotoRoot()">Root</a>`;
        let accum = "";
        parts.forEach(p => {
            accum = accum ? `${accum}/${p}` : p;
            const safeAccum = accum.replace(/'/g, "\\'");
            crumbHtml += ` / <a href="javascript:void(0)" onclick="navigateTo('${safeAccum}')">${p}</a>`;
        });
        bc.innerHTML = crumbHtml;
    }

    if (data.current) {
        const upBtn = document.createElement('button');
        upBtn.textContent = "⬆️ Up";
        upBtn.className = "btn-blue";
        upBtn.style.marginBottom = "8px";
        upBtn.onclick = () => {
            const parts = data.current.split('/');
            parts.pop();
            window.currentPath = parts.join('/');
            loadStreamFolder();
        };
        container.appendChild(upBtn);
    }

    data.folders.forEach(fname => {
        const div = document.createElement('div');
        div.className = "file-item folder-item";
        div.innerHTML = `<span style="font-weight:bold;">📁 ${fname}</span>`;
        div.onclick = () => {
            window.currentPath = data.current ? `${data.current}/${fname}` : fname;
            loadStreamFolder();
        };
        container.appendChild(div);
    });

    const streamSearch = document.getElementById('streamSearch');
    const filter = streamSearch ? streamSearch.value.trim().toLowerCase() : '';
    const filesToShow = data.files.filter(f => f.name.toLowerCase().includes(filter));

    filesToShow.forEach(f => {
        const div = document.createElement('div');
        div.className = "file-item";
        const safeName = f.name.replace(/'/g, "\\'");
        const safePath = f.path.replace(/'/g, "\\'");
        div.innerHTML = `<div class="file-info"><div class="file-name">${f.name}</div>
                     <div class="file-meta">${humanFileSize(f.size)}</div></div>
                     <button class="btn-blue" onclick="downloadP2P('${safeName}','streams','${safePath}')">P2P</button>
                     <button class="btn-blue" onclick="downloadLegacy('${safeName}','streams','${safePath}')">Legacy</button>`;
        container.appendChild(div);
    });
}

function navigateTo(posixPath) {
    window.currentPath = posixPath;
    loadStreamFolder();
}

function gotoRoot() {
    window.currentPath = "";
    loadStreamFolder();
}

/* ------------------------------------------------------------------
   P2P Download
   ------------------------------------------------------------------ */
async function downloadP2P(filename, mode, filepath = "") {
    const prog = createProgressBar(`Downloading ${filename}…`);
    const driveSelect = document.getElementById('driveSelect');
    const drive = driveSelect ? driveSelect.value : '';

    try {
        const metaUrl = mode === "archive"
            ? `/file_info?room=${encodeURIComponent(window.room)}&filename=${encodeURIComponent(filename)}${window.authToken ? `&auth_token=${encodeURIComponent(window.authToken)}` : ''}`
            : `/streams/file_info?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}`;
        const metaResp = await fetch(metaUrl);
        if (!metaResp.ok) {
            alert(`Could not fetch metadata for ${filename}`);
            prog.remove();
            return;
        }
        const meta = await metaResp.json();

        const total = meta.total_chunks;
        const chunks = new Array(total);

        for (let i = 0; i < total; i++) {
            const url = mode === "archive"
                ? `/download_chunk?room=${encodeURIComponent(window.room)}&filename=${encodeURIComponent(filename)}&chunk_id=${i}${window.authToken ? `&auth_token=${encodeURIComponent(window.authToken)}` : ''}`
                : `/streams/data?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}&chunk_id=${i}`;
            const resp = await fetch(url);
            if (!resp.ok) {
                alert(`Chunk ${i} failed`);
                prog.remove();
                return;
            }
            const buf = await resp.arrayBuffer();
            chunks[i] = new Uint8Array(buf);
            updateProgressBar(prog, i + 1, total);
        }

        const finalBlob = new Blob(chunks);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(finalBlob);
        a.download = filename;
        a.click();
        prog.remove();
    } catch (err) {
        console.error('Download error:', err);
        alert('Download failed: ' + err.message);
        prog.remove();
    }
}

async function downloadLegacy(filename, mode, filepath = "") {
    const prog = createProgressBar(`Downloading (Legacy) ${filename}…`);
    const driveSelect = document.getElementById('driveSelect');
    const drive = driveSelect ? driveSelect.value : '';

    try {
        const url = mode === "archive"
            ? `/download?room=${encodeURIComponent(window.room)}&filename=${encodeURIComponent(filename)}${window.authToken ? `&auth_token=${encodeURIComponent(window.authToken)}` : ''}`
            : `/streams/data_legacy?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}`;

        const resp = await fetch(url);
        if (!resp.ok) {
            const err = await resp.json();
            alert(err.detail || "Legacy download failed");
            prog.remove();
            return;
        }

        const blob = await resp.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        prog.remove();
    } catch (err) {
        console.error('Legacy download error:', err);
        alert('Legacy download failed: ' + err.message);
        prog.remove();
    }
}

/* ------------------------------------------------------------------
   WebSocket initialization
   ------------------------------------------------------------------ */
function initSockets() {
    const updateUrl = `${getWsProtocol()}//${location.host}/ws/${window.room}`;
    window.updateWs = new WebSocket(updateUrl);
    window.updateWs.onmessage = e => {
        if (e.data === "UPDATE") refreshArchiveList();
    };
    window.updateWs.onopen = () => console.log("Update WS open");
    window.updateWs.onclose = () => console.log("Update WS closed");
    window.updateWs.onerror = (err) => console.error("Update WS error:", err);

    const chatUrl = `${getWsProtocol()}//${location.host}/ws/chat/${window.room}` +
        (window.authToken ? `?auth_token=${encodeURIComponent(window.authToken)}` : '');
    window.chatWs = new WebSocket(chatUrl);
    window.chatWs.onmessage = e => {
        try {
            const parsed = JSON.parse(e.data);
            if (parsed.type === "auth") {
                window.authToken = parsed.token;
                localStorage.setItem(`auth_token_${window.room}`, window.authToken);
                setAuthenticated(true);
                addSystemMessage("✅ Password accepted – you can now chat, upload and download.");
                return;
            }
        } catch { }
        if (typeof e.data === "string" && e.data.startsWith("SYSTEM:")) {
            addSystemMessage(e.data.replace(/^SYSTEM:\s*/, ""));
            return;
        }
        if (window.authenticated) {
            const p = document.createElement('p');
            p.textContent = e.data;
            window.chatBox.appendChild(p);
            window.chatBox.scrollTop = window.chatBox.scrollHeight;
        }
    };
    window.chatWs.onopen = () => console.log("Chat WS open");
    window.chatWs.onclose = () => console.log("Chat WS closed");
    window.chatWs.onerror = (err) => console.error("Chat WS error:", err);
}

function initSignal() {
    const wsUrl = `${getWsProtocol()}//${location.host}/ws/signal/${window.room}`;
    window.signalWs = new WebSocket(wsUrl);

    window.signalWs.onopen = () => {
        window.myPeerId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : Math.random().toString(36).slice(2, 12);
        window.signalWs.send(JSON.stringify({ type: "join", id: window.myPeerId }));
        console.log("Signalling WS opened – peer ID:", window.myPeerId);
    };

    window.signalWs.onmessage = e => {
        try {
            const msg = JSON.parse(e.data);
            if (msg.type === "peer_list" || msg.type === "new_peer") {
                const count = (msg.peers ? msg.peers.length : 0) + 1;
                const archivePeerCount = document.getElementById('archivePeerCount');
                const streamPeerCount = document.getElementById('streamPeerCount');
                if (archivePeerCount) archivePeerCount.textContent = count;
                if (streamPeerCount) streamPeerCount.textContent = count;
                return;
            }
        } catch (err) {
            console.error('Signal message error:', err);
        }
    };

    window.signalWs.onerror = (err) => console.error('Signalling WS error:', err);
    window.signalWs.onclose = () => console.log('Signalling WS closed');
}

function leaveRoom() {
    localStorage.removeItem(`auth_token_${window.room}`);
    location.reload();
}

/* ------------------------------------------------------------------
   Bootstrap
   ------------------------------------------------------------------ */
async function bootstrapApp() {
    try {
        await initAuth();
        await refreshArchiveList();
        await populateDriveSelect();
        setInterval(populateDriveSelect, 5000);
        initSockets();
        initSignal();
    } catch (err) {
        console.error("Bootstrap failed:", err);
    }
}
