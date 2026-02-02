/* ------------------------------------------------------------------
   GLOBAL CONSTANTS & STATE
   ------------------------------------------------------------------ */
const room = window.location.pathname.split('/').filter(p=>p)[0] || "main";
document.getElementById("roomName").textContent = room;
document.getElementById("roomDisplay").textContent = room;

// DOM refs
const progressArea = document.getElementById("progressArea");
const filesDiv    = document.getElementById("files");
const chatBox    = document.getElementById("chatBox");

// auth
let authToken = localStorage.getItem(`auth_token_${room}`) || "";
let authenticated = false;
let currentUser = localStorage.getItem('archive_username') || "Anonymous";
document.getElementById("currentUser").textContent = currentUser;

// keep uploaded files in memory for P2P sharing
const myFiles = {};          // filename → File object

// peer‑tracking for P2P
let myPeerId = "";                // will be generated after signalling WS opens
const peers = {};                 // peerId → SimplePeer instance
const filePeers = {};             // filename → [peerId, …] (who announced they have it)
const pendingChunkRequests = {};   // "filename:chunkIdx" → {resolve,reject}
const CHUNK_SIZE = 4 * 1024 * 1024; // 4 MiB – must match server

// WS connections (initialised later)
let updateWs, chatWs, signalWs;

/* ------------------------------------------------------------------
   Helper UI utilities
   ------------------------------------------------------------------ */
function humanFileSize(bytes){
  const units=['B','KB','MB','GB','TB','PB'];
  let i=0;
  while(bytes>=1024 && i<units.length-1){bytes/=1024;i++;}
  return `${bytes.toFixed(1)} ${units[i]}`;
}
function showTab(tabId, btn){
  document.querySelectorAll('.content-section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  btn.classList.add('active');
}
function createProgressBar(text){
  const div = document.createElement('div');
  div.className = "progress-item";
  div.innerHTML = `<div>${text}</div>
                   <div class="progress-bar-bg"><div class="bar"></div></div>`;
  progressArea.appendChild(div);
  return div;
}
function updateProgressBar(container, cur, total){
  const pct = Math.round(cur/total*100);
  container.querySelector('.bar').style.width = pct + "%";
  if(pct>=100){
    setTimeout(()=>container.remove(),1500);
  }
}
function addSystemMessage(msg){
  const p=document.createElement('p');
  p.textContent = msg;
  p.style.fontStyle="italic";
  p.style.color="#555";
  chatBox.appendChild(p);
  chatBox.scrollTop = chatBox.scrollHeight;
}

/* ------------------------------------------------------------------
   Auth & password flow (modal comes from chat, but we also use it for uploads)
   ------------------------------------------------------------------ */
async function initAuth(){
  const pwdResp = await fetch(`/room_password?room=${encodeURIComponent(room)}`);
  const pwdData = await pwdResp.json();
  if (!pwdData.required){
    setAuthenticated(true);
    return;
  }

  // try cached token silently
  if (authToken){
    try{
      const test = await fetch(`/list?room=${encodeURIComponent(room)}&auth_token=${encodeURIComponent(authToken)}`);
      if (test.ok){ setAuthenticated(true); return; }
    }catch{}
  }

  setAuthenticated(false);
}
function setAuthenticated(val){
  authenticated = val;
  document.getElementById('fileInput').disabled = !val;
  document.getElementById('msgInput')
          .placeholder = val ? "Type a message…" : "Enter password: pass:YOUR_PASSWORD";
}

/* ------------------------------------------------------------------
   Chat – includes password entry & token handling
   ------------------------------------------------------------------ */
function changeUsername(){
  const val = document.getElementById('usernameInput').value.trim()||"Anonymous";
  currentUser = val;
  localStorage.setItem('archive_username', currentUser);
  document.getElementById('currentUser').textContent = currentUser;
}
function sendMessage(){
  const inp = document.getElementById('msgInput');
  const text = inp.value.trim();
  if (!text) return;

  if (!authenticated && !text.toLowerCase().startsWith("pass:")){
    addSystemMessage("You must authenticate first – type pass:YOUR_PASSWORD");
    return;
  }

  if (!authenticated && text.toLowerCase().startsWith("pass:")){
    chatWs.send(text);          // e.g. "pass:mySecret"
    inp.value = "";
    return;
  }

  chatWs.send(`${currentUser}: ${text}`);
  inp.value = "";
}

/* ------------------------------------------------------------------
   Archive – list, search, upload, delete
   ------------------------------------------------------------------ */
let archiveFileList = [];

async function refreshArchiveList(){
  const rsp = await fetch(`/list?room=${encodeURIComponent(room)}${authToken?`&auth_token=${encodeURIComponent(authToken)}`:''}`);
  if (!rsp.ok) return;
  const data = await rsp.json();
  archiveFileList = data.files.sort((a,b)=>a.name.localeCompare(b.name));
  renderArchiveList();
}
function renderArchiveList(filter=""){
  const lower = filter.toLowerCase();
  filesDiv.innerHTML="";
  archiveFileList
    .filter(f=>f.name.toLowerCase().includes(lower))
    .forEach(f=>{
      const item=document.createElement('div');
      item.className="file-item";
      item.innerHTML = `<div class="file-info"><div class="file-name">${f.name}</div>
                       <div class="file-meta">${humanFileSize(f.size)}</div></div>
                       <div>
                         <button class="btn-blue" onclick="downloadP2P('${f.name}','archive')">P2P</button>
                         <button class="btn-red" onclick="deleteFile('${f.name}')">🗑</button>
                         <button class="btn-blue" onclick="downloadLegacy('${f.name}','archive')">Legacy</button>
                       </div>`;
      filesDiv.appendChild(item);
    });
}
document.getElementById('searchInput').addEventListener('input', e=>renderArchiveList(e.target.value));

async function deleteFile(name){
  if (!authenticated){ alert("You must authenticate first."); return; }
  if (!confirm(`Delete ${name}?`)) return;

  const delUrl = `/delete/${encodeURIComponent(room)}/${encodeURIComponent(name)}${authToken?`?auth_token=${encodeURIComponent(authToken)}`:''}`;
  const delResp = await fetch(delUrl, {method:'DELETE'});
  if (!delResp.ok){
    const err = await delResp.json();
    alert(err.detail || "Delete failed");
    return;
  }
  refreshArchiveList();
}

/* ------------------------------------------------------------------
   Upload (chunked, cancellable)
   ------------------------------------------------------------------ */
document.getElementById('fileInput').addEventListener('change', e=>{
  if (!authenticated){
    alert("You must authenticate first – type pass:YOUR_PASSWORD in the chat.");
    e.target.value = "";
    return;
  }
  Array.from(e.target.files).forEach(file=>uploadFile(file));
  e.target.value = "";
});
document.getElementById('dropzone')?.addEventListener('dragover', e=>e.preventDefault());
document.getElementById('dropzone')?.addEventListener('drop', e=>{
  e.preventDefault();
  if (!authenticated){
    alert("You must authenticate first.");
    return;
  }
  for (const file of e.dataTransfer.files) uploadFile(file);
});

/* Store a reference to the uploaded file for later P2P sharing */
function rememberLocalFile(file){
  myFiles[file.name] = file;   // keep the whole File object in memory
}

async function uploadFile(file){
  rememberLocalFile(file);

  const totalChunks = Math.ceil(file.size/CHUNK_SIZE);
  const prog = createProgressBar(`Uploading ${file.name}…`);
  const abortCtrl = new AbortController();

  // cancel button (required feature)
  const cancelBtn=document.createElement('button');
  cancelBtn.textContent="Cancel";
  cancelBtn.className="btn-red";
  cancelBtn.style.marginLeft="10px";
  cancelBtn.onclick=()=>{
    abortCtrl.abort();
    fetch(`/cancel_upload?room=${encodeURIComponent(room)}&filename=${encodeURIComponent(file.name)}`,{method:"POST"});
    prog.remove();
  };
  prog.appendChild(cancelBtn);

  for (let i=0;i<totalChunks;i++){
    const chunk = file.slice(i*CHUNK_SIZE,(i+1)*CHUNK_SIZE);
    const form = new FormData();
    form.append("file", chunk);
    const url = `/upload_chunk?room=${encodeURIComponent(room)}&filename=${encodeURIComponent(file.name)}&chunk_index=${i}&total_chunks=${totalChunks}`+
                (authToken?`&auth_token=${encodeURIComponent(authToken)}`:'');
    try{
      const resp = await fetch(url,{method:"POST",body:form,signal:abortCtrl.signal});
      if (!resp.ok){
        const err = await resp.json();
        alert(err.detail || "Upload failed");
        prog.remove();
        return;
      }
    }catch(e){
      if (e.name==="AbortError"){
        console.log(`Upload of ${file.name} aborted`);
        return;
      }
      console.error(e);
      prog.remove();
      return;
    }
    updateProgressBar(prog,i+1,totalChunks);
  }

  // announce file availability to peers (P2P swarm)
  if (signalWs && myPeerId){
    signalWs.send(JSON.stringify({
      type:"file_available",
      filename:file.name,
      total_chunks:totalChunks,
      size:file.size,
      peer_id:myPeerId
    }));
  }

  // optimistic UI refresh (server also sends UPDATE)
  refreshArchiveList();
}

/* ------------------------------------------------------------------
   STREAMS – drive selector, folder navigation, download
   ------------------------------------------------------------------ */
let currentDrive = "";      // mount name
let currentPath = "";       // relative POSIX path (empty = root)
let currentFolderData = null;
let firstDriveLoad = true;   // used so that we only load the folder once on page start

async function populateDriveSelect(){
  const rsp = await fetch("/streams/drives");
  const data = await rsp.json();
  const sel = document.getElementById('driveSelect');
  const previous = sel.value;   // keep whatever the UI currently shows

  sel.innerHTML="";
  let anyMissing = false;
  data.drives.forEach(d=>{
    const opt=document.createElement('option');
    opt.value = d.mount;
    opt.textContent = d.display + (d.available?"":" (missing)");
    opt.disabled = !d.available;
    sel.appendChild(opt);
    if (!d.available) anyMissing = true;
  });

  // Restore previous selection if still available
  if (previous && [...sel.options].some(o=>o.value===previous && !o.disabled)){
    sel.value = previous;
  } else {
    // otherwise try to load stored preference
    const stored = localStorage.getItem('last_drive');
    if (stored && [...sel.options].some(o=>o.value===stored && !o.disabled)){
      sel.value = stored;
    } else if (sel.options.length>0){
      sel.selectedIndex = 0;
    }
  }

  // remember the selection for the next reload
  localStorage.setItem('last_drive', sel.value);

  const statusDiv = document.getElementById('driveStatus');
  if (anyMissing){
    statusDiv.textContent = "⚠️ One or more drives are missing – check server console.";
    statusDiv.classList.remove('hidden');
  }else{
    statusDiv.classList.add('hidden');
  }

  // Load folder only once on the initial page load.
  // Subsequent periodic refreshes will only update the drive‑list status.
  if (firstDriveLoad){
    listStreamFiles();
    firstDriveLoad = false;
  }
}
setInterval(populateDriveSelect,5000);   // refresh drive list every 5 s (no folder reset)

/* Called when user selects a different drive */
function driveChanged(){
  const sel = document.getElementById('driveSelect');
  localStorage.setItem('last_drive', sel.value);
  listStreamFiles();
}

/* Load the root of the selected drive */
function listStreamFiles(){
  currentDrive = document.getElementById('driveSelect').value;
  currentPath = "";          // reset to root when the drive changes
  loadStreamFolder();
}

/* Load the folder indicated by currentPath within currentDrive */
async function loadStreamFolder(){
  if (!currentDrive) return;
  const url = `/streams/browse?drive=${encodeURIComponent(currentDrive)}&dir_path=${encodeURIComponent(currentPath)}`;
  const rsp = await fetch(url);
  if (!rsp.ok){
    const txt = await rsp.text();
    alert(`Failed to load folder: ${txt}`);
    return;
  }
  const data = await rsp.json();
  currentFolderData = data;
  renderStreamFolder(data);
}

/* Render breadcrumb + folders + files */
function renderStreamFolder(data){
  const container = document.getElementById('streamFiles');
  container.innerHTML="";

  // breadcrumb
  const bc = document.getElementById('breadcrumb');
  const parts = data.current ? data.current.split('/') : [];
  let crumbHtml = `<a href="javascript:void(0)" onclick="gotoRoot()">Root</a>`;
  let accum = "";
  parts.forEach(p=>{
    accum = accum ? `${accum}/${p}` : p;
    crumbHtml += ` / <a href="javascript:void(0)" onclick="navigateTo('${accum}')">${p}</a>`;
  });
  bc.innerHTML = crumbHtml;

  // Up button (if not at root)
  if (data.current){
    const upBtn=document.createElement('button');
    upBtn.textContent = "⬆️ Up";
    upBtn.className = "btn-blue";
    upBtn.style.marginBottom = "8px";
    upBtn.onclick = () => {
      const parts = data.current.split('/');
      parts.pop();
      currentPath = parts.join('/');
      loadStreamFolder();
    };
    container.appendChild(upBtn);
  }

  // sub‑folders
  data.folders.forEach(fname=>{
    const div=document.createElement('div');
    div.className="file-item folder-item";
    div.innerHTML = `<span style="font-weight:bold;">📁 ${fname}</span>`;
    div.onclick = () => {
      currentPath = data.current ? `${data.current}/${fname}` : fname;
      loadStreamFolder();
    };
    container.appendChild(div);
  });

  // file filter
  const filter = document.getElementById('streamSearch').value.trim().toLowerCase();
  const filesToShow = data.files.filter(f=>f.name.toLowerCase().includes(filter));

  filesToShow.forEach(f=>{
    const div=document.createElement('div');
    div.className="file-item";
    div.innerHTML = `<div class="file-info"><div class="file-name">${f.name}</div>
                     <div class="file-meta">${humanFileSize(f.size)}</div></div>
                     <button class="btn-blue" onclick="downloadP2P('${f.name}','streams','${f.path}')">P2P</button>
                     <button class="btn-blue" onclick="downloadLegacy('${f.name}','streams','${f.path}')">Legacy</button>`;
    container.appendChild(div);
  });
}

/* Breadcrumb navigation helpers */
function navigateTo(posixPath){
  currentPath = posixPath;
  loadStreamFolder();
}
function gotoRoot(){
  currentPath = "";
  loadStreamFolder();
}

/* Re‑render folder when searching */
document.getElementById('streamSearch').addEventListener('input', e=>{
  if (!currentFolderData) return;
  renderStreamFolder(currentFolderData);
});

/* ------------------------------------------------------------------
   P2P – signalling, peer handling, chunk exchange (unchanged)
   ------------------------------------------------------------------ */
function initSignal(){
  signalWs = new WebSocket(`${location.protocol.replace('http','ws')}//${location.host}/ws/signal/${room}`);
  signalWs.onopen = ()=>{
    myPeerId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : Math.random().toString(36).slice(2,12);
    signalWs.send(JSON.stringify({type:"join", id:myPeerId}));
    console.log("🟢 signalling WS opened – my peer id:",myPeerId);
  };
  signalWs.onmessage = e=>{
    const msg=JSON.parse(e.data);

    // tracker messages
    if (msg.type==="peer_list" || msg.type==="new_peer"){
      const count = (msg.peers ? msg.peers.length : 0) + 1;
      document.getElementById('archivePeerCount').textContent = count;
      document.getElementById('streamPeerCount').textContent = count;
      return;
    }

    // file availability announcements
    if (msg.type==="file_available"){
      const fn = msg.filename;
      if (!filePeers[fn]) filePeers[fn]=[];
      if (!filePeers[fn].includes(msg.peer_id)){
        filePeers[fn].push(msg.peer_id);
        console.log(`📢 Peer ${msg.peer_id} announced file ${fn}`);
      }
      return;
    }

    // WebRTC signalling
    if (msg.to && msg.from && msg.type==="signal"){
      const from = msg.from;
      const data = msg.data;
      let peer = peers[from];
      if (!peer){
        peer = new SimplePeer({initiator:false, trickle:false});
        peers[from]=peer;
        attachPeerEvents(peer, from);
      }
      peer.signal(data);
      return;
    }
  };
}

// create (or reuse) a SimplePeer for a given remote peer
function getPeer(remoteId){
  let peer = peers[remoteId];
  if (peer) return peer;
  peer = new SimplePeer({initiator:true, trickle:false});
  peers[remoteId]=peer;
  // forward our signalling data
  peer.on('signal', data=>{
    signalWs.send(JSON.stringify({
      type:"signal",
      to: remoteId,
      from: myPeerId,
      data
    }));
  });
  attachPeerEvents(peer, remoteId);
  return peer;
}

// generic handlers for a SimplePeer instance
function attachPeerEvents(peer, remoteId){
  peer.on('connect',()=>{console.log(`🔗 P2P connection with ${remoteId} established`);});
  peer.on('data', raw=>{
    let msg;
    try{ msg=JSON.parse(raw); }catch(e){ console.error('Invalid P2P message',e); return; }
    if (msg.type==="request_chunk"){
      const fileObj = myFiles[msg.filename];
      if (!fileObj){ return; }
      const start = msg.chunk_index*CHUNK_SIZE;
      const chunkBlob = fileObj.slice(start, start+CHUNK_SIZE);
      chunkBlob.arrayBuffer().then(buf=>{
        const b64 = arrayBufferToBase64(buf);
        peer.send(JSON.stringify({
          type:"chunk_data",
          filename:msg.filename,
          chunk_index:msg.chunk_index,
          data:b64
        }));
      });
    }else if (msg.type==="chunk_data"){
      const key = `${msg.filename}:${msg.chunk_index}`;
      const pending = pendingChunkRequests[key];
      if (!pending) return;
      const arrayBuf = base64ToArrayBuffer(msg.data);
      pending.resolve(arrayBuf);
      delete pendingChunkRequests[key];
    }
  });
  peer.on('error', err=>{ console.error(`⚠️ Peer ${remoteId} error:`,err); delete peers[remoteId]; });
  peer.on('close', ()=>{ console.log(`🔌 Peer ${remoteId} closed`); delete peers[remoteId]; });
}

// wait for SimplePeer to be connected (or timeout)
function waitForPeerConnect(peer){
  return new Promise((resolve,reject)=>{
    if (peer.connected) return resolve();
    const timer=setTimeout(()=>{reject(new Error("Peer connect timeout"));},5000);
    peer.once('connect',()=>{clearTimeout(timer); resolve();});
  });
}

/* ------------------------------------------------------------------
   P2P download orchestrator (archive or streams)
   ------------------------------------------------------------------ */
async function downloadP2P(filename, mode, filepath=""){
  const prog = createProgressBar(`Downloading ${filename}…`);
  const drive = document.getElementById('driveSelect').value;   // only for streams

  // metadata – always fetched from server
  const metaUrl = mode==="archive"
    ? `/file_info?room=${encodeURIComponent(room)}&filename=${encodeURIComponent(filename)}${authToken?`&auth_token=${encodeURIComponent(authToken)}`:''}`
    : `/streams/file_info?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}`;
  const metaResp = await fetch(metaUrl);
  if (!metaResp.ok){ alert(`Could not fetch metadata for ${filename}`); prog.remove(); return; }
  const meta = await metaResp.json();

  const total = meta.total_chunks;
  const chunks = new Array(total);
  const usePeers = (filePeers[filename] || []).filter(pid=>pid!==myPeerId);
  let peer = null;

  // try to connect to the first peer that announced they have the file
  for (const pid of usePeers){
    try{
      peer = getPeer(pid);
      await waitForPeerConnect(peer);
      console.log(`✅ P2P connection to ${pid} ready`);
      break;
    }catch(e){
      console.warn(`❌ Could not connect to peer ${pid}:`,e);
    }
  }

  // request a chunk (via P2P if possible, else HTTP)
  async function fetchChunk(idx){
    if (peer){
      return await requestChunkViaPeer(peer, filename, idx);
    }
    const url = mode==="archive"
      ? `/download_chunk?room=${encodeURIComponent(room)}&filename=${encodeURIComponent(filename)}&chunk_id=${idx}${authToken?`&auth_token=${encodeURIComponent(authToken)}`:''}`
      : `/streams/data?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}&chunk_id=${idx}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Chunk ${idx} fetch failed`);
    return await resp.arrayBuffer();
  }

  function requestChunkViaPeer(p, fn, idx){
    return new Promise((resolve,reject)=>{
      const key=`${fn}:${idx}`;
      pendingChunkRequests[key]={resolve,reject};
      p.send(JSON.stringify({type:"request_chunk",filename:fn,chunk_index:idx}));
      setTimeout(()=>{
        if (pendingChunkRequests[key]){
          delete pendingChunkRequests[key];
          reject(new Error("Chunk request timeout"));
        }
      },8000);
    });
  }

  for (let i=0;i<total;i++){
    try{
      const buf = await fetchChunk(i);
      chunks[i] = new Uint8Array(buf);
    }catch(e){
      console.warn(`Chunk ${i} failed via P2P – falling back to server`,e);
      if (peer){
        const url = mode==="archive"
          ? `/download_chunk?room=${encodeURIComponent(room)}&filename=${encodeURIComponent(filename)}&chunk_id=${i}${authToken?`&auth_token=${encodeURIComponent(authToken)}`:''}`
          : `/streams/data?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}&chunk_id=${i}`;
        const resp = await fetch(url);
        if (!resp.ok){ alert(`Chunk ${i} failed entirely`); prog.remove(); return; }
        const fallback = await resp.arrayBuffer();
        chunks[i] = new Uint8Array(fallback);
      }
    }
    updateProgressBar(prog,i+1,total);
  }

  // assemble final Blob
  const finalBlob = new Blob(chunks);
  const a=document.createElement('a');
  a.href=URL.createObjectURL(finalBlob);
  a.download=filename;
  a.click();
  prog.remove();
}

/* ------------------------------------------------------------------
   Legacy download – limited to MAX_LEGACY_USERS (default 2)
   ------------------------------------------------------------------ */
async function downloadLegacy(filename, mode, filepath=""){
  const prog = createProgressBar(`Downloading (Legacy) ${filename}…`);
  const drive = document.getElementById('driveSelect').value;

  const url = mode==="archive"
    ? `/download?room=${encodeURIComponent(room)}&filename=${encodeURIComponent(filename)}${authToken?`&auth_token=${encodeURIComponent(authToken)}`:''}`
    : `/streams/data_legacy?path=${encodeURIComponent(filepath)}&drive=${encodeURIComponent(drive)}`;

  const resp = await fetch(url);
  if (!resp.ok){
    const err = await resp.json();
    alert(err.detail || "Legacy download failed");
    prog.remove();
    return;
  }

  const blob = await resp.blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=filename;
  a.click();
  prog.remove();
}

/* ------------------------------------------------------------------
   WebSocket initialisation (updates + chat)
   ------------------------------------------------------------------ */
function initSockets(){
  // UI file‑list update WS
  updateWs = new WebSocket(`${location.protocol.replace('http','ws')}//${location.host}/ws/${room}`);
  updateWs.onmessage = e=>{ if (e.data==="UPDATE") refreshArchiveList(); };
  updateWs.onopen   = ()=>console.log("🔔 update WS open");
  updateWs.onclose  = ()=>console.log("🔔 update WS closed");

  // Chat WS – includes auth token in query string (if we have one)
  const chatUrl = `${location.protocol.replace('http','ws')}//${location.host}/ws/chat/${room}` +
                 (authToken ? `?auth_token=${encodeURIComponent(authToken)}` : '');
  chatWs = new WebSocket(chatUrl);
  chatWs.onmessage = e=>{
    try{
      const parsed = JSON.parse(e.data);
      if (parsed.type==="auth"){
        authToken = parsed.token;
        localStorage.setItem(`auth_token_${room}`,authToken);
        setAuthenticated(true);
        addSystemMessage("✅ Password accepted – you can now chat, upload and download.");
        return;
      }
    }catch{}
    if (typeof e.data==="string" && e.data.startsWith("SYSTEM:")){
      addSystemMessage(e.data.replace(/^SYSTEM:\s*/,""));
      return;
    }
    if (authenticated){
      const p=document.createElement('p');
      p.textContent = e.data;
      chatBox.appendChild(p);
      chatBox.scrollTop = chatBox.scrollHeight;
    }
  };
  chatWs.onopen   = ()=>console.log("💬 chat WS open");
  chatWs.onclose  = ()=>console.log("💬 chat WS closed");
}

/* ------------------------------------------------------------------
   Utilities for Base64 ↔ ArrayBuffer (used by P2P)
   ------------------------------------------------------------------ */
function arrayBufferToBase64(buf){
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i=0;i<bytes.byteLength;i++) binary+=String.fromCharCode(bytes[i]);
  return btoa(binary);
}
function base64ToArrayBuffer(b64){
  const binary = atob(b64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i=0;i<len;i++) bytes[i]=binary.charCodeAt(i);
  return bytes.buffer;
}

/* ------------------------------------------------------------------
   Logout / “Leave room” – clear token & reload page
   ------------------------------------------------------------------ */
function leaveRoom(){
  localStorage.removeItem(`auth_token_${room}`);
  location.reload();
}

/* ------------------------------------------------------------------
   First‑run bootstrap
   ------------------------------------------------------------------ */
(async ()=>{
  await initAuth();          // detect password‑required, try token
  refreshArchiveList();      // list archive files
  await populateDriveSelect();   // load drive selector (once)
  initSockets();             // chat + UI‑update sockets
  initSignal();              // P2P signalling
})();
