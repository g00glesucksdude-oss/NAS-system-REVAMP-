#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FastAPI P2P File Sharing - FIXED MULTI-ROOM VERSION
Key fixes:
- WebSocket handlers properly isolated per room
- File paths correctly scoped to rooms
- Peer tracking separated by room
- State management room-aware
"""

# ----------------------------------------------------------------------
# IMPORTS
# ----------------------------------------------------------------------
import os
import json
import shutil
import socket
import threading
import shlex
import time
import secrets
import hashlib
import datetime
from pathlib import Path
from typing import Dict, List, Set

import aiofiles
from fastapi import (
    FastAPI, UploadFile, File, HTTPException, WebSocket,
    WebSocketDisconnect, Request,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_ROOT = BASE_DIR / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
PERSIST_FILE = BASE_DIR / "persist.json"

MAX_PWD_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
MAX_LEGACY_USERS = 2

# ----------------------------------------------------------------------
# GLOBAL STATE - PROPERLY STRUCTURED FOR MULTI-ROOM
# ----------------------------------------------------------------------

# WebSocket connections - all properly room-scoped
chat_rooms: Dict[str, List[WebSocket]] = {}  # room → all chat sockets
chat_auth: Dict[str, Set[WebSocket]] = {}  # room → authenticated sockets
signal_rooms: Dict[str, Dict[str, WebSocket]] = {}  # room → {peer_id: WS}
update_rooms: Dict[str, List[WebSocket]] = {}  # room → update sockets

# File state - room-scoped keys
uploads_state: Dict[str, dict] = {}  # "room/filename" → upload state

# Auth & security
auth_tokens: Dict[str, dict] = {}  # token → {"room":str, "expires":ts}
pwd_attempts: Dict[str, Dict[str, dict]] = {}  # room → ip → {count, locked}

# Server config
DRIVE_INFO: Dict[str, dict] = {}  # mount_name → {"path":Path, "display":str}
ROOM_PASSWORDS: Dict[str, str] = {}  # room → password
READONLY_ROOMS: Set[str] = set()

legacy_download_users: Set[str] = set()

# ----------------------------------------------------------------------
# UTILITY FUNCTIONS
# ----------------------------------------------------------------------

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def meta_path(file_path: Path) -> Path:
    return file_path.with_suffix(file_path.suffix + ".meta.json")

def _sanitize_mount_name(name: str) -> str:
    name = name.strip().replace(" ", "_")
    name = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    return name or "drive"

def _load_passwords_from_file() -> Dict[str, str]:
    if not PERSIST_FILE.is_file():
        return {}
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("passwords", {})
    except Exception:
        return {}

# ----------------------------------------------------------------------
# STATE PERSISTENCE
# ----------------------------------------------------------------------

def load_state():
    global DRIVE_INFO, ROOM_PASSWORDS, READONLY_ROOMS, MAX_LEGACY_USERS
    if not PERSIST_FILE.is_file():
        print("[i] No persisted state – starting fresh")
        return
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Load drives
        for name, info in data.get("drives", {}).items():
            path = Path(info["path"])
            if path.is_dir():
                DRIVE_INFO[name] = {"path": path, "display": info.get("display", name)}
        
        ROOM_PASSWORDS = data.get("passwords", {})
        READONLY_ROOMS = set(data.get("readonly_rooms", []))
        
        settings = data.get("settings", {})
        MAX_LEGACY_USERS = settings.get("max_legacy_users", 2)
        
        print("[i] Persisted state loaded.")
    except Exception as exc:
        print(f"[!] Failed to load persisted state: {exc}")

def save_state():
    data = {
        "drives": {
            n: {"path": str(info["path"]), "display": info["display"]}
            for n, info in DRIVE_INFO.items()
        },
        "passwords": ROOM_PASSWORDS,
        "readonly_rooms": list(READONLY_ROOMS),
        "settings": {"max_legacy_users": MAX_LEGACY_USERS},
    }
    try:
        with PERSIST_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[!] Could not persist state: {exc}")

def add_drive(path: Path):
    if not path.is_dir():
        print(f"[!] Drive path does not exist → {path}")
        return
    
    base_name = path.name
    mount_name = _sanitize_mount_name(base_name)
    suffix = 0
    final_name = mount_name
    while final_name in DRIVE_INFO:
        suffix += 1
        final_name = f"{mount_name}_{suffix}"
    
    DRIVE_INFO[final_name] = {"path": path, "display": base_name}
    app.mount(f"/{final_name}", StaticFiles(directory=path), name=final_name)
    print(f"[+] Mounted {path} as /{final_name}")
    save_state()

def remove_drive(name: str):
    if name not in DRIVE_INFO:
        print(f"[!] No such drive {name}")
        return
    
    app.router.routes = [
        r for r in app.router.routes
        if not (hasattr(r, "path") and r.path.startswith(f"/{name}"))
    ]
    DRIVE_INFO.pop(name)
    print(f"[-] Unmounted drive {name}")
    save_state()

# ----------------------------------------------------------------------
# ADMIN CONSOLE (REPL)
# ----------------------------------------------------------------------

def console_thread():
    help_msg = """
Commands:
  adddrive <path>      - mount external folder
  rmdrive <name>       - unmount drive
  listdrives           - show mounted drives
  setpwd <room> <pwd>  - protect room with password
  rempwd <room>        - remove password
  setreadonly <room>   - mark room read-only
  unsetreadonly <room> - remove read-only flag
  setlegacylimit <N>   - set max legacy downloads
  reload               - reload persist.json
  exit / quit          - stop server
"""
    print(help_msg)
    
    while True:
        try:
            raw = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            print(f"[!] Parsing error: {e}")
            continue
        
        if not parts:
            continue
        
        cmd = parts[0].lower()
        
        if cmd == "adddrive" and len(parts) >= 2:
            add_drive(Path(" ".join(parts[1:])).expanduser().resolve())
            continue
        
        if cmd == "rmdrive" and len(parts) == 2:
            remove_drive(parts[1])
            continue
        
        if cmd == "listdrives":
            if DRIVE_INFO:
                for n, v in DRIVE_INFO.items():
                    print(f"{n}: {v['path']}")
            else:
                print("[i] No drives mounted.")
            continue
        
        if cmd == "setpwd" and len(parts) >= 3:
            room = parts[1]
            pwd = " ".join(parts[2:])
            ROOM_PASSWORDS[room] = pwd
            save_state()
            print(f'[i] Password set for room "{room}".')
            continue
        
        if cmd == "rempwd" and len(parts) == 2:
            ROOM_PASSWORDS.pop(parts[1], None)
            save_state()
            print(f'[i] Password removed for room "{parts[1]}".')
            continue
        
        if cmd == "setreadonly" and len(parts) == 2:
            READONLY_ROOMS.add(parts[1])
            save_state()
            print(f'[i] Room "{parts[1]}" set to read-only.')
            continue
        
        if cmd == "unsetreadonly" and len(parts) == 2:
            READONLY_ROOMS.discard(parts[1])
            save_state()
            print(f'[i] Read-only flag removed from room "{parts[1]}".')
            continue
        
        if cmd == "setlegacylimit" and len(parts) == 2:
            try:
                global MAX_LEGACY_USERS
                MAX_LEGACY_USERS = int(parts[1])
                save_state()
                print(f"[i] Max legacy download users set to {MAX_LEGACY_USERS}")
            except ValueError:
                print("[!] Invalid number")
            continue
        
        if cmd == "reload":
            load_state()
            for name, info in DRIVE_INFO.items():
                app.mount(f"/{name}", StaticFiles(directory=info["path"]), name=name)
            print("[i] State reloaded from persist.json")
            continue
        
        if cmd in ("exit", "quit"):
            print("[i] Shutting down …")
            os._exit(0)
        
        print("[!] Unknown command.")
        print(help_msg)

# ----------------------------------------------------------------------
# FASTAPI APP
# ----------------------------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_state()

for name, info in DRIVE_INFO.items():
    app.mount(f"/{name}", StaticFiles(directory=info["path"]), name=name)

env_drives = [
    Path(p).expanduser().resolve()
    for p in os.getenv("STREAM_DRIVES", "").split(os.pathsep)
    if p
]
for p in env_drives:
    add_drive(p)

# ----------------------------------------------------------------------
# AUTH HELPERS
# ----------------------------------------------------------------------

def _track_attempt(room: str, client_ip: str, success: bool):
    attempts = pwd_attempts.setdefault(room, {}).setdefault(
        client_ip, {"count": 0, "locked_until": 0}
    )
    now = time.time()
    if attempts["locked_until"] > now:
        return
    if success:
        attempts["count"] = 0
        attempts["locked_until"] = 0
    else:
        attempts["count"] += 1
        if attempts["count"] >= MAX_PWD_ATTEMPTS:
            attempts["locked_until"] = now + LOCKOUT_SECONDS

def check_password(room: str, pwd: str | None, client_ip: str):
    passwords = _load_passwords_from_file()
    required = passwords.get(room)
    
    if required is None:
        _track_attempt(room, client_ip, True)
        return
    
    attempts = pwd_attempts.setdefault(room, {}).setdefault(
        client_ip, {"count": 0, "locked_until": 0}
    )
    now = time.time()
    if attempts["locked_until"] > now:
        raise HTTPException(
            status_code=403,
            detail=f"Room locked. Try again in {int(attempts['locked_until'] - now)} seconds",
        )
    
    if pwd != required:
        _track_attempt(room, client_ip, False)
        raise HTTPException(status_code=403, detail="Invalid room password")
    
    _track_attempt(room, client_ip, True)

def verify_room(room: str, pwd: str | None, client_ip: str | None):
    passwords = _load_passwords_from_file()
    if room in passwords:
        if client_ip is None:
            raise HTTPException(status_code=400, detail="Client IP required")
        check_password(room, pwd, client_ip)

def auth_or_pwd(
    room: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request | None = None,
):
    if auth_token:
        info = auth_tokens.get(auth_token)
        if not info or info["room"] != room:
            raise HTTPException(status_code=403, detail="Invalid auth token")
        if info.get("expires", 0) < time.time():
            auth_tokens.pop(auth_token, None)
            raise HTTPException(status_code=403, detail="Auth token expired")
        return
    
    client_ip = request.client.host if request else None
    verify_room(room, pwd, client_ip)

# ----------------------------------------------------------------------
# HEALTH & INFO ENDPOINTS
# ----------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/room_password")
async def room_password(room: str):
    passwords = _load_passwords_from_file()
    return {"required": room in passwords}

@app.get("/room_info")
async def room_info(room: str):
    passwords = _load_passwords_from_file()
    return {
        "password_required": room in passwords,
        "readonly": room in READONLY_ROOMS,
    }

@app.get("/verify_password")
async def verify_password(room: str, pwd: str):
    passwords = _load_passwords_from_file()
    required = passwords.get(room)
    if required is None:
        return {"valid": True}
    return {"valid": required == pwd}

# ----------------------------------------------------------------------
# UPLOAD ENDPOINT (chunked)
# ----------------------------------------------------------------------

@app.post("/upload_chunk")
async def upload_chunk(
    room: str,
    filename: str,
    chunk_index: int,
    total_chunks: int,
    file: UploadFile = File(...),
    checksum: str | None = None,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    if room in READONLY_ROOMS:
        raise HTTPException(status_code=403, detail="Room is read-only")
    
    if not (0 <= chunk_index < total_chunks):
        raise HTTPException(status_code=400, detail="Invalid chunk_index")
    
    # FIXED: Proper room directory structure
    room_dir = UPLOAD_ROOT / room
    room_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = room_dir / f"{filename}.tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    data = await file.read()
    if checksum and checksum != sha256_bytes(data):
        raise HTTPException(status_code=400, detail="Checksum mismatch")
    
    chunk_path = tmp_dir / f"chunk_{chunk_index}"
    async with aiofiles.open(chunk_path, "wb") as out:
        await out.write(data)
    
    # FIXED: Room-scoped state key
    key = f"{room}/{filename}"
    state = uploads_state.get(key)
    if not state:
        state = {
            "total_chunks": total_chunks,
            "received": set(),
            "hashes": [None] * total_chunks,
        }
        uploads_state[key] = state
    
    state["received"].add(chunk_index)
    state["hashes"][chunk_index] = sha256_bytes(data)
    
    if len(state["received"]) == total_chunks:
        final_path = room_dir / filename
        async with aiofiles.open(final_path, "wb") as out_file:
            for i in range(total_chunks):
                part_path = tmp_dir / f"chunk_{i}"
                async with aiofiles.open(part_path, "rb") as part:
                    while True:
                        block = await part.read(1024 * 1024)
                        if not block:
                            break
                        await out_file.write(block)
        
        meta = {
            "filename": filename,
            "size": final_path.stat().st_size,
            "chunk_size": CHUNK_SIZE,
            "total_chunks": total_chunks,
            "chunk_hashes": state["hashes"],
        }
        
        meta_file_path = meta_path(final_path)
        async with aiofiles.open(meta_file_path, "w") as f:
            await f.write(json.dumps(meta))
        
        shutil.rmtree(tmp_dir, ignore_errors=True)
        uploads_state.pop(key, None)
        
        await notify_update(room)
    
    return {"status": "ok", "chunk_index": chunk_index}

# ----------------------------------------------------------------------
# FILE METADATA
# ----------------------------------------------------------------------

@app.get("/file_info")
async def file_info(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    # FIXED: Proper room path
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    meta_file = meta_path(file_path)
    if meta_file.is_file():
        async with aiofiles.open(meta_file, "r") as f:
            meta = json.loads(await f.read())
        return meta
    
    size = file_path.stat().st_size
    total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return {
        "filename": filename,
        "size": size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total,
        "chunk_hashes": [],
    }

# ----------------------------------------------------------------------
# DOWNLOAD ENDPOINTS
# ----------------------------------------------------------------------

@app.get("/download_chunk")
async def download_chunk(
    room: str,
    filename: str,
    chunk_id: int,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    size = file_path.stat().st_size
    start = chunk_id * CHUNK_SIZE
    if start >= size:
        raise HTTPException(status_code=416, detail="Chunk out of range")
    
    length = min(CHUNK_SIZE, size - start)
    
    async def generator():
        async with aiofiles.open(file_path, "rb") as f:
            await f.seek(start)
            remaining = length
            while remaining:
                block = await f.read(min(1024 * 1024, remaining))
                if not block:
                    break
                yield block
                remaining -= len(block)
    
    return StreamingResponse(generator(), media_type="application/octet-stream")

@app.get("/download")
async def download_full(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

# ----------------------------------------------------------------------
# FILE MANAGEMENT
# ----------------------------------------------------------------------

@app.get("/list")
async def list_files(
    room: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    room_path = UPLOAD_ROOT / room
    if not room_path.is_dir():
        return {"files": [], "room": room}
    
    return {
        "files": [
            {"name": p.name, "size": p.stat().st_size}
            for p in room_path.iterdir()
            if p.is_file() and not p.name.endswith(".meta.json")
        ],
        "room": room,
    }

@app.delete("/delete/{room}/{filename}")
async def delete_file(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    if room in READONLY_ROOMS:
        raise HTTPException(status_code=403, detail="Room is read-only")
    
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    meta_file = meta_path(file_path)
    file_path.unlink()
    if meta_file.is_file():
        meta_file.unlink()
    
    await notify_update(room)
    return {"status": "deleted"}

@app.post("/cancel_upload")
async def cancel_upload(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    tmp_dir = UPLOAD_ROOT / room / f"{filename}.tmp"
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
    uploads_state.pop(f"{room}/{filename}", None)
    return {"status": "canceled"}

# ----------------------------------------------------------------------
# STREAMS - DRIVE MANAGEMENT
# ----------------------------------------------------------------------

@app.get("/streams/drives")
async def list_drives():
    return {
        "drives": [
            {
                "mount": name,
                "display": info["display"],
                "path": str(info["path"]),
                "available": info["path"].is_dir(),
            }
            for name, info in DRIVE_INFO.items()
        ]
    }

@app.get("/streams/file_info")
async def streams_file_info(path: str, drive: str = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    p = DRIVE_INFO[drive]["path"] / path
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    size = p.stat().st_size
    total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return {
        "filename": p.name,
        "size": size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total,
        "chunk_hashes": [],
    }

@app.get("/streams/browse")
async def streams_browse(drive: str, dir_path: str = ""):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    base = DRIVE_INFO[drive]["path"]
    clean = dir_path.strip("/").replace("\\", "/")
    target = base / clean if clean else base
    
    try:
        target = target.resolve()
        target.relative_to(base.resolve())
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not target.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    
    folders: List[str] = []
    files: List[dict] = []
    
    for entry in target.iterdir():
        if entry.is_dir():
            folders.append(entry.name)
        elif entry.is_file() and not entry.name.endswith(".meta.json"):
            rel = entry.relative_to(base).as_posix()
            files.append({"name": entry.name, "size": entry.stat().st_size, "path": rel})
    
    return {
        "drive": drive,
        "current": clean,
        "folders": sorted(folders),
        "files": sorted(files, key=lambda f: f["name"]),
    }

@app.get("/streams/data")
async def streams_data(path: str, drive: str = None, chunk_id: int = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    p = DRIVE_INFO[drive]["path"] / path
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    if chunk_id is None:
        return FileResponse(p)
    
    size = p.stat().st_size
    start = chunk_id * CHUNK_SIZE
    if start >= size:
        raise HTTPException(status_code=416, detail="Chunk out of range")
    
    length = min(CHUNK_SIZE, size - start)
    
    async def generator():
        async with aiofiles.open(p, "rb") as f:
            await f.seek(start)
            remaining = length
            while remaining:
                block = await f.read(min(1024 * 1024, remaining))
                if not block:
                    break
                yield block
                remaining -= len(block)
    
    return StreamingResponse(generator(), media_type="application/octet-stream")

@app.get("/streams/data_legacy")
async def streams_data_legacy(path: str, drive: str = None, request: Request = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    client_ip = request.client.host if request else "unknown"
    
    if len(legacy_download_users) >= MAX_LEGACY_USERS and client_ip not in legacy_download_users:
        raise HTTPException(
            status_code=503,
            detail=f"Legacy download limit reached ({MAX_LEGACY_USERS} users)",
        )
    
    file_path = DRIVE_INFO[drive]["path"] / path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    legacy_download_users.add(client_ip)
    try:
        return FileResponse(file_path)
    finally:
        legacy_download_users.discard(client_ip)

@app.get("/settings")
async def get_settings():
    return {
        "max_legacy_users": MAX_LEGACY_USERS,
        "current_legacy_users": len(legacy_download_users),
    }

# ----------------------------------------------------------------------
# WEBSOCKETS - FIXED FOR PROPER ROOM ISOLATION
# ----------------------------------------------------------------------

@app.websocket("/ws/chat/{room}")
async def chat_ws(websocket: WebSocket, room: str):
    """
    Chat WebSocket with proper room isolation.
    Each room has its own separate chat and authentication.
    """
    client_ip = websocket.client.host
    await websocket.accept()
    
    # FIXED: Register to THIS room only
    chat_rooms.setdefault(room, []).append(websocket)
    chat_auth.setdefault(room, set())
    
    # Token auth
    token = websocket.query_params.get("auth_token")
    password_required = None
    
    if token:
        info = auth_tokens.get(token)
        if info and info["room"] == room and info.get("expires", 0) > time.time():
            chat_auth[room].add(websocket)
            await websocket.send_text("SYSTEM: Token authentication successful.")
            password_required = False
        else:
            await websocket.send_text("SYSTEM: Invalid/expired token")
            passwords = _load_passwords_from_file()
            password_required = room in passwords
    else:
        passwords = _load_passwords_from_file()
        password_required = room in passwords
        
        if not password_required:
            chat_auth[room].add(websocket)
            await websocket.send_text("SYSTEM: No password required.")
        else:
            await websocket.send_text("SYSTEM: Type pass:YOUR_PASSWORD to authenticate.")
    
    try:
        while True:
            raw = await websocket.receive_text()
            
            # Password handling
            if password_required and raw.lower().startswith("pass:"):
                attempt_pwd = raw[5:].strip()
                try:
                    verify_room(room, attempt_pwd, client_ip)
                except HTTPException:
                    await websocket.send_text("SYSTEM: Invalid password or locked.")
                    continue
                
                token = secrets.token_urlsafe(16)
                auth_tokens[token] = {"room": room, "expires": time.time() + 3600}
                chat_auth[room].add(websocket)
                await websocket.send_json({"type": "auth", "token": token})
                await websocket.send_text("SYSTEM: Password accepted.")
                password_required = False
                continue
            
            # Auto-auth for public rooms
            if not password_required:
                chat_auth[room].add(websocket)
            
            # Enforce auth
            if password_required and websocket not in chat_auth[room]:
                await websocket.send_text("SYSTEM: Authenticate first.")
                continue
            
            # FIXED: Broadcast ONLY to THIS room's authenticated users
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            broadcast = f"[{ts}] {raw}"
            
            for ws in list(chat_auth.get(room, [])):
                try:
                    await ws.send_text(broadcast)
                except Exception:
                    chat_auth[room].discard(ws)
    
    except WebSocketDisconnect:
        # FIXED: Clean up only from THIS room
        if websocket in chat_rooms.get(room, []):
            chat_rooms[room].remove(websocket)
        if room in chat_auth:
            chat_auth[room].discard(websocket)
        
        if room in chat_rooms and not chat_rooms[room]:
            del chat_rooms[room]
        if room in chat_auth and not chat_auth[room]:
            del chat_auth[room]

@app.websocket("/ws/signal/{room}")
async def signal_ws(websocket: WebSocket, room: str):
    """
    P2P signalling WebSocket - properly room-scoped.
    """
    client_ip = websocket.client.host
    await websocket.accept()
    
    pwd = websocket.query_params.get("pwd")
    try:
        auth_or_pwd(room, pwd, None, request=None)
    except HTTPException:
        await websocket.close(code=1008)
        return
    
    peer_id: str | None = None
    
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            
            if msg.get("type") == "join":
                peer_id = msg["id"]
                # FIXED: Register to THIS room only
                signal_rooms.setdefault(room, {})[peer_id] = websocket
                
                others = [pid for pid in signal_rooms[room] if pid != peer_id]
                await websocket.send_json({"type": "peer_list", "peers": others})
                
                # Notify others in THIS room
                for pid, ws in signal_rooms[room].items():
                    if pid != peer_id:
                        await ws.send_json({"type": "new_peer", "id": peer_id})
                continue
            
            # Direct message
            target = msg.get("to")
            if target and room in signal_rooms and target in signal_rooms[room]:
                await signal_rooms[room][target].send_json(msg)
                continue
            
            # FIXED: Broadcast only to THIS room
            for pid, ws in signal_rooms.get(room, {}).items():
                if pid != peer_id:
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        signal_rooms[room].pop(pid, None)
    
    except WebSocketDisconnect:
        # FIXED: Clean up only from THIS room
        if peer_id and room in signal_rooms:
            signal_rooms[room].pop(peer_id, None)
            if not signal_rooms[room]:
                del signal_rooms[room]

@app.websocket("/ws/{room}")
async def update_ws(websocket: WebSocket, room: str):
    """
    File update WebSocket - properly room-scoped.
    """
    await websocket.accept()
    
    # FIXED: Register to THIS room only
    update_rooms.setdefault(room, []).append(websocket)
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        # FIXED: Clean up only from THIS room
        if room in update_rooms:
            update_rooms[room].remove(websocket)
            if not update_rooms[room]:
                del update_rooms[room]

async def notify_update(room: str):
    """Notify only the clients in THIS specific room."""
    if room not in update_rooms:
        return
    
    for ws in update_rooms[room][:]:
        try:
            await ws.send_text("UPDATE")
        except Exception:
            update_rooms[room].remove(ws)

# ----------------------------------------------------------------------
# LEGACY COMPATIBILITY
# ----------------------------------------------------------------------

@app.post("/upload")
async def upload_compat(room: str, file: UploadFile = File(...)):
    return await upload_chunk(
        room=room,
        filename=file.filename,
        chunk_index=0,
        total_chunks=1,
        file=file,
        checksum=None,
        pwd=None,
        auth_token=None,
        request=None,
    )

# ----------------------------------------------------------------------
# STATIC FILES
# ----------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="FastAPI P2P File Sharing")
    parser.add_argument("--drive", action="append", default=[])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    
    for p in args.drive:
        add_drive(Path(p).expanduser().resolve())
    
    threading.Thread(target=console_thread, daemon=True).start()
    
    ip = get_local_ip()
    print(f"\n🚀 Server ready → http://{ip}:{args.port}\n")
    
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=args.port, log_level="warning")#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FastAPI P2P File Sharing - FIXED MULTI-ROOM VERSION
Key fixes:
- WebSocket handlers properly isolated per room
- File paths correctly scoped to rooms
- Peer tracking separated by room
- State management room-aware
"""

# ----------------------------------------------------------------------
# IMPORTS
# ----------------------------------------------------------------------
import os
import json
import shutil
import socket
import threading
import shlex
import time
import secrets
import hashlib
import datetime
from pathlib import Path
from typing import Dict, List, Set

import aiofiles
from fastapi import (
    FastAPI, UploadFile, File, HTTPException, WebSocket,
    WebSocketDisconnect, Request,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_ROOT = BASE_DIR / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
PERSIST_FILE = BASE_DIR / "persist.json"

MAX_PWD_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
MAX_LEGACY_USERS = 2

# ----------------------------------------------------------------------
# GLOBAL STATE - PROPERLY STRUCTURED FOR MULTI-ROOM
# ----------------------------------------------------------------------

# WebSocket connections - all properly room-scoped
chat_rooms: Dict[str, List[WebSocket]] = {}  # room → all chat sockets
chat_auth: Dict[str, Set[WebSocket]] = {}  # room → authenticated sockets
signal_rooms: Dict[str, Dict[str, WebSocket]] = {}  # room → {peer_id: WS}
update_rooms: Dict[str, List[WebSocket]] = {}  # room → update sockets

# File state - room-scoped keys
uploads_state: Dict[str, dict] = {}  # "room/filename" → upload state

# Auth & security
auth_tokens: Dict[str, dict] = {}  # token → {"room":str, "expires":ts}
pwd_attempts: Dict[str, Dict[str, dict]] = {}  # room → ip → {count, locked}

# Server config
DRIVE_INFO: Dict[str, dict] = {}  # mount_name → {"path":Path, "display":str}
ROOM_PASSWORDS: Dict[str, str] = {}  # room → password
READONLY_ROOMS: Set[str] = set()

legacy_download_users: Set[str] = set()

# ----------------------------------------------------------------------
# UTILITY FUNCTIONS
# ----------------------------------------------------------------------

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def meta_path(file_path: Path) -> Path:
    return file_path.with_suffix(file_path.suffix + ".meta.json")

def _sanitize_mount_name(name: str) -> str:
    name = name.strip().replace(" ", "_")
    name = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    return name or "drive"

def _load_passwords_from_file() -> Dict[str, str]:
    if not PERSIST_FILE.is_file():
        return {}
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("passwords", {})
    except Exception:
        return {}

# ----------------------------------------------------------------------
# STATE PERSISTENCE
# ----------------------------------------------------------------------

def load_state():
    global DRIVE_INFO, ROOM_PASSWORDS, READONLY_ROOMS, MAX_LEGACY_USERS
    if not PERSIST_FILE.is_file():
        print("[i] No persisted state – starting fresh")
        return
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Load drives
        for name, info in data.get("drives", {}).items():
            path = Path(info["path"])
            if path.is_dir():
                DRIVE_INFO[name] = {"path": path, "display": info.get("display", name)}
        
        ROOM_PASSWORDS = data.get("passwords", {})
        READONLY_ROOMS = set(data.get("readonly_rooms", []))
        
        settings = data.get("settings", {})
        MAX_LEGACY_USERS = settings.get("max_legacy_users", 2)
        
        print("[i] Persisted state loaded.")
    except Exception as exc:
        print(f"[!] Failed to load persisted state: {exc}")

def save_state():
    data = {
        "drives": {
            n: {"path": str(info["path"]), "display": info["display"]}
            for n, info in DRIVE_INFO.items()
        },
        "passwords": ROOM_PASSWORDS,
        "readonly_rooms": list(READONLY_ROOMS),
        "settings": {"max_legacy_users": MAX_LEGACY_USERS},
    }
    try:
        with PERSIST_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[!] Could not persist state: {exc}")

def add_drive(path: Path):
    if not path.is_dir():
        print(f"[!] Drive path does not exist → {path}")
        return
    
    base_name = path.name
    mount_name = _sanitize_mount_name(base_name)
    suffix = 0
    final_name = mount_name
    while final_name in DRIVE_INFO:
        suffix += 1
        final_name = f"{mount_name}_{suffix}"
    
    DRIVE_INFO[final_name] = {"path": path, "display": base_name}
    app.mount(f"/{final_name}", StaticFiles(directory=path), name=final_name)
    print(f"[+] Mounted {path} as /{final_name}")
    save_state()

def remove_drive(name: str):
    if name not in DRIVE_INFO:
        print(f"[!] No such drive {name}")
        return
    
    app.router.routes = [
        r for r in app.router.routes
        if not (hasattr(r, "path") and r.path.startswith(f"/{name}"))
    ]
    DRIVE_INFO.pop(name)
    print(f"[-] Unmounted drive {name}")
    save_state()

# ----------------------------------------------------------------------
# ADMIN CONSOLE (REPL)
# ----------------------------------------------------------------------

def console_thread():
    help_msg = """
Commands:
  adddrive <path>      - mount external folder
  rmdrive <name>       - unmount drive
  listdrives           - show mounted drives
  setpwd <room> <pwd>  - protect room with password
  rempwd <room>        - remove password
  setreadonly <room>   - mark room read-only
  unsetreadonly <room> - remove read-only flag
  setlegacylimit <N>   - set max legacy downloads
  reload               - reload persist.json
  exit / quit          - stop server
"""
    print(help_msg)
    
    while True:
        try:
            raw = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            print(f"[!] Parsing error: {e}")
            continue
        
        if not parts:
            continue
        
        cmd = parts[0].lower()
        
        if cmd == "adddrive" and len(parts) >= 2:
            add_drive(Path(" ".join(parts[1:])).expanduser().resolve())
            continue
        
        if cmd == "rmdrive" and len(parts) == 2:
            remove_drive(parts[1])
            continue
        
        if cmd == "listdrives":
            if DRIVE_INFO:
                for n, v in DRIVE_INFO.items():
                    print(f"{n}: {v['path']}")
            else:
                print("[i] No drives mounted.")
            continue
        
        if cmd == "setpwd" and len(parts) >= 3:
            room = parts[1]
            pwd = " ".join(parts[2:])
            ROOM_PASSWORDS[room] = pwd
            save_state()
            print(f'[i] Password set for room "{room}".')
            continue
        
        if cmd == "rempwd" and len(parts) == 2:
            ROOM_PASSWORDS.pop(parts[1], None)
            save_state()
            print(f'[i] Password removed for room "{parts[1]}".')
            continue
        
        if cmd == "setreadonly" and len(parts) == 2:
            READONLY_ROOMS.add(parts[1])
            save_state()
            print(f'[i] Room "{parts[1]}" set to read-only.')
            continue
        
        if cmd == "unsetreadonly" and len(parts) == 2:
            READONLY_ROOMS.discard(parts[1])
            save_state()
            print(f'[i] Read-only flag removed from room "{parts[1]}".')
            continue
        
        if cmd == "setlegacylimit" and len(parts) == 2:
            try:
                global MAX_LEGACY_USERS
                MAX_LEGACY_USERS = int(parts[1])
                save_state()
                print(f"[i] Max legacy download users set to {MAX_LEGACY_USERS}")
            except ValueError:
                print("[!] Invalid number")
            continue
        
        if cmd == "reload":
            load_state()
            for name, info in DRIVE_INFO.items():
                app.mount(f"/{name}", StaticFiles(directory=info["path"]), name=name)
            print("[i] State reloaded from persist.json")
            continue
        
        if cmd in ("exit", "quit"):
            print("[i] Shutting down …")
            os._exit(0)
        
        print("[!] Unknown command.")
        print(help_msg)

# ----------------------------------------------------------------------
# FASTAPI APP
# ----------------------------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_state()

for name, info in DRIVE_INFO.items():
    app.mount(f"/{name}", StaticFiles(directory=info["path"]), name=name)

env_drives = [
    Path(p).expanduser().resolve()
    for p in os.getenv("STREAM_DRIVES", "").split(os.pathsep)
    if p
]
for p in env_drives:
    add_drive(p)

# ----------------------------------------------------------------------
# AUTH HELPERS
# ----------------------------------------------------------------------

def _track_attempt(room: str, client_ip: str, success: bool):
    attempts = pwd_attempts.setdefault(room, {}).setdefault(
        client_ip, {"count": 0, "locked_until": 0}
    )
    now = time.time()
    if attempts["locked_until"] > now:
        return
    if success:
        attempts["count"] = 0
        attempts["locked_until"] = 0
    else:
        attempts["count"] += 1
        if attempts["count"] >= MAX_PWD_ATTEMPTS:
            attempts["locked_until"] = now + LOCKOUT_SECONDS

def check_password(room: str, pwd: str | None, client_ip: str):
    passwords = _load_passwords_from_file()
    required = passwords.get(room)
    
    if required is None:
        _track_attempt(room, client_ip, True)
        return
    
    attempts = pwd_attempts.setdefault(room, {}).setdefault(
        client_ip, {"count": 0, "locked_until": 0}
    )
    now = time.time()
    if attempts["locked_until"] > now:
        raise HTTPException(
            status_code=403,
            detail=f"Room locked. Try again in {int(attempts['locked_until'] - now)} seconds",
        )
    
    if pwd != required:
        _track_attempt(room, client_ip, False)
        raise HTTPException(status_code=403, detail="Invalid room password")
    
    _track_attempt(room, client_ip, True)

def verify_room(room: str, pwd: str | None, client_ip: str | None):
    passwords = _load_passwords_from_file()
    if room in passwords:
        if client_ip is None:
            raise HTTPException(status_code=400, detail="Client IP required")
        check_password(room, pwd, client_ip)

def auth_or_pwd(
    room: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request | None = None,
):
    if auth_token:
        info = auth_tokens.get(auth_token)
        if not info or info["room"] != room:
            raise HTTPException(status_code=403, detail="Invalid auth token")
        if info.get("expires", 0) < time.time():
            auth_tokens.pop(auth_token, None)
            raise HTTPException(status_code=403, detail="Auth token expired")
        return
    
    client_ip = request.client.host if request else None
    verify_room(room, pwd, client_ip)

# ----------------------------------------------------------------------
# HEALTH & INFO ENDPOINTS
# ----------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/room_password")
async def room_password(room: str):
    passwords = _load_passwords_from_file()
    return {"required": room in passwords}

@app.get("/room_info")
async def room_info(room: str):
    passwords = _load_passwords_from_file()
    return {
        "password_required": room in passwords,
        "readonly": room in READONLY_ROOMS,
    }

@app.get("/verify_password")
async def verify_password(room: str, pwd: str):
    passwords = _load_passwords_from_file()
    required = passwords.get(room)
    if required is None:
        return {"valid": True}
    return {"valid": required == pwd}

# ----------------------------------------------------------------------
# UPLOAD ENDPOINT (chunked)
# ----------------------------------------------------------------------

@app.post("/upload_chunk")
async def upload_chunk(
    room: str,
    filename: str,
    chunk_index: int,
    total_chunks: int,
    file: UploadFile = File(...),
    checksum: str | None = None,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    if room in READONLY_ROOMS:
        raise HTTPException(status_code=403, detail="Room is read-only")
    
    if not (0 <= chunk_index < total_chunks):
        raise HTTPException(status_code=400, detail="Invalid chunk_index")
    
    # FIXED: Proper room directory structure
    room_dir = UPLOAD_ROOT / room
    room_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = room_dir / f"{filename}.tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    data = await file.read()
    if checksum and checksum != sha256_bytes(data):
        raise HTTPException(status_code=400, detail="Checksum mismatch")
    
    chunk_path = tmp_dir / f"chunk_{chunk_index}"
    async with aiofiles.open(chunk_path, "wb") as out:
        await out.write(data)
    
    # FIXED: Room-scoped state key
    key = f"{room}/{filename}"
    state = uploads_state.get(key)
    if not state:
        state = {
            "total_chunks": total_chunks,
            "received": set(),
            "hashes": [None] * total_chunks,
        }
        uploads_state[key] = state
    
    state["received"].add(chunk_index)
    state["hashes"][chunk_index] = sha256_bytes(data)
    
    if len(state["received"]) == total_chunks:
        final_path = room_dir / filename
        async with aiofiles.open(final_path, "wb") as out_file:
            for i in range(total_chunks):
                part_path = tmp_dir / f"chunk_{i}"
                async with aiofiles.open(part_path, "rb") as part:
                    while True:
                        block = await part.read(1024 * 1024)
                        if not block:
                            break
                        await out_file.write(block)
        
        meta = {
            "filename": filename,
            "size": final_path.stat().st_size,
            "chunk_size": CHUNK_SIZE,
            "total_chunks": total_chunks,
            "chunk_hashes": state["hashes"],
        }
        
        meta_file_path = meta_path(final_path)
        async with aiofiles.open(meta_file_path, "w") as f:
            await f.write(json.dumps(meta))
        
        shutil.rmtree(tmp_dir, ignore_errors=True)
        uploads_state.pop(key, None)
        
        await notify_update(room)
    
    return {"status": "ok", "chunk_index": chunk_index}

# ----------------------------------------------------------------------
# FILE METADATA
# ----------------------------------------------------------------------

@app.get("/file_info")
async def file_info(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    # FIXED: Proper room path
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    meta_file = meta_path(file_path)
    if meta_file.is_file():
        async with aiofiles.open(meta_file, "r") as f:
            meta = json.loads(await f.read())
        return meta
    
    size = file_path.stat().st_size
    total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return {
        "filename": filename,
        "size": size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total,
        "chunk_hashes": [],
    }

# ----------------------------------------------------------------------
# DOWNLOAD ENDPOINTS
# ----------------------------------------------------------------------

@app.get("/download_chunk")
async def download_chunk(
    room: str,
    filename: str,
    chunk_id: int,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    size = file_path.stat().st_size
    start = chunk_id * CHUNK_SIZE
    if start >= size:
        raise HTTPException(status_code=416, detail="Chunk out of range")
    
    length = min(CHUNK_SIZE, size - start)
    
    async def generator():
        async with aiofiles.open(file_path, "rb") as f:
            await f.seek(start)
            remaining = length
            while remaining:
                block = await f.read(min(1024 * 1024, remaining))
                if not block:
                    break
                yield block
                remaining -= len(block)
    
    return StreamingResponse(generator(), media_type="application/octet-stream")

@app.get("/download")
async def download_full(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

# ----------------------------------------------------------------------
# FILE MANAGEMENT
# ----------------------------------------------------------------------

@app.get("/list")
async def list_files(
    room: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    room_path = UPLOAD_ROOT / room
    if not room_path.is_dir():
        return {"files": [], "room": room}
    
    return {
        "files": [
            {"name": p.name, "size": p.stat().st_size}
            for p in room_path.iterdir()
            if p.is_file() and not p.name.endswith(".meta.json")
        ],
        "room": room,
    }

@app.delete("/delete/{room}/{filename}")
async def delete_file(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    if room in READONLY_ROOMS:
        raise HTTPException(status_code=403, detail="Room is read-only")
    
    file_path = UPLOAD_ROOT / room / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    meta_file = meta_path(file_path)
    file_path.unlink()
    if meta_file.is_file():
        meta_file.unlink()
    
    await notify_update(room)
    return {"status": "deleted"}

@app.post("/cancel_upload")
async def cancel_upload(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    auth_or_pwd(room, pwd, auth_token, request)
    
    tmp_dir = UPLOAD_ROOT / room / f"{filename}.tmp"
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    
    uploads_state.pop(f"{room}/{filename}", None)
    return {"status": "canceled"}

# ----------------------------------------------------------------------
# STREAMS - DRIVE MANAGEMENT
# ----------------------------------------------------------------------

@app.get("/streams/drives")
async def list_drives():
    return {
        "drives": [
            {
                "mount": name,
                "display": info["display"],
                "path": str(info["path"]),
                "available": info["path"].is_dir(),
            }
            for name, info in DRIVE_INFO.items()
        ]
    }

@app.get("/streams/file_info")
async def streams_file_info(path: str, drive: str = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    p = DRIVE_INFO[drive]["path"] / path
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    size = p.stat().st_size
    total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    return {
        "filename": p.name,
        "size": size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total,
        "chunk_hashes": [],
    }

@app.get("/streams/browse")
async def streams_browse(drive: str, dir_path: str = ""):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    base = DRIVE_INFO[drive]["path"]
    clean = dir_path.strip("/").replace("\\", "/")
    target = base / clean if clean else base
    
    try:
        target = target.resolve()
        target.relative_to(base.resolve())
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not target.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    
    folders: List[str] = []
    files: List[dict] = []
    
    for entry in target.iterdir():
        if entry.is_dir():
            folders.append(entry.name)
        elif entry.is_file() and not entry.name.endswith(".meta.json"):
            rel = entry.relative_to(base).as_posix()
            files.append({"name": entry.name, "size": entry.stat().st_size, "path": rel})
    
    return {
        "drive": drive,
        "current": clean,
        "folders": sorted(folders),
        "files": sorted(files, key=lambda f: f["name"]),
    }

@app.get("/streams/data")
async def streams_data(path: str, drive: str = None, chunk_id: int = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    p = DRIVE_INFO[drive]["path"] / path
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    if chunk_id is None:
        return FileResponse(p)
    
    size = p.stat().st_size
    start = chunk_id * CHUNK_SIZE
    if start >= size:
        raise HTTPException(status_code=416, detail="Chunk out of range")
    
    length = min(CHUNK_SIZE, size - start)
    
    async def generator():
        async with aiofiles.open(p, "rb") as f:
            await f.seek(start)
            remaining = length
            while remaining:
                block = await f.read(min(1024 * 1024, remaining))
                if not block:
                    break
                yield block
                remaining -= len(block)
    
    return StreamingResponse(generator(), media_type="application/octet-stream")

@app.get("/streams/data_legacy")
async def streams_data_legacy(path: str, drive: str = None, request: Request = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    
    client_ip = request.client.host if request else "unknown"
    
    if len(legacy_download_users) >= MAX_LEGACY_USERS and client_ip not in legacy_download_users:
        raise HTTPException(
            status_code=503,
            detail=f"Legacy download limit reached ({MAX_LEGACY_USERS} users)",
        )
    
    file_path = DRIVE_INFO[drive]["path"] / path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    legacy_download_users.add(client_ip)
    try:
        return FileResponse(file_path)
    finally:
        legacy_download_users.discard(client_ip)

@app.get("/settings")
async def get_settings():
    return {
        "max_legacy_users": MAX_LEGACY_USERS,
        "current_legacy_users": len(legacy_download_users),
    }

# ----------------------------------------------------------------------
# WEBSOCKETS - FIXED FOR PROPER ROOM ISOLATION
# ----------------------------------------------------------------------

@app.websocket("/ws/chat/{room}")
async def chat_ws(websocket: WebSocket, room: str):
    """
    Chat WebSocket with proper room isolation.
    Each room has its own separate chat and authentication.
    """
    client_ip = websocket.client.host
    await websocket.accept()
    
    # FIXED: Register to THIS room only
    chat_rooms.setdefault(room, []).append(websocket)
    chat_auth.setdefault(room, set())
    
    # Token auth
    token = websocket.query_params.get("auth_token")
    password_required = None
    
    if token:
        info = auth_tokens.get(token)
        if info and info["room"] == room and info.get("expires", 0) > time.time():
            chat_auth[room].add(websocket)
            await websocket.send_text("SYSTEM: Token authentication successful.")
            password_required = False
        else:
            await websocket.send_text("SYSTEM: Invalid/expired token")
            passwords = _load_passwords_from_file()
            password_required = room in passwords
    else:
        passwords = _load_passwords_from_file()
        password_required = room in passwords
        
        if not password_required:
            chat_auth[room].add(websocket)
            await websocket.send_text("SYSTEM: No password required.")
        else:
            await websocket.send_text("SYSTEM: Type pass:YOUR_PASSWORD to authenticate.")
    
    try:
        while True:
            raw = await websocket.receive_text()
            
            # Password handling
            if password_required and raw.lower().startswith("pass:"):
                attempt_pwd = raw[5:].strip()
                try:
                    verify_room(room, attempt_pwd, client_ip)
                except HTTPException:
                    await websocket.send_text("SYSTEM: Invalid password or locked.")
                    continue
                
                token = secrets.token_urlsafe(16)
                auth_tokens[token] = {"room": room, "expires": time.time() + 3600}
                chat_auth[room].add(websocket)
                await websocket.send_json({"type": "auth", "token": token})
                await websocket.send_text("SYSTEM: Password accepted.")
                password_required = False
                continue
            
            # Auto-auth for public rooms
            if not password_required:
                chat_auth[room].add(websocket)
            
            # Enforce auth
            if password_required and websocket not in chat_auth[room]:
                await websocket.send_text("SYSTEM: Authenticate first.")
                continue
            
            # FIXED: Broadcast ONLY to THIS room's authenticated users
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            broadcast = f"[{ts}] {raw}"
            
            for ws in list(chat_auth.get(room, [])):
                try:
                    await ws.send_text(broadcast)
                except Exception:
                    chat_auth[room].discard(ws)
    
    except WebSocketDisconnect:
        # FIXED: Clean up only from THIS room
        if websocket in chat_rooms.get(room, []):
            chat_rooms[room].remove(websocket)
        if room in chat_auth:
            chat_auth[room].discard(websocket)
        
        if room in chat_rooms and not chat_rooms[room]:
            del chat_rooms[room]
        if room in chat_auth and not chat_auth[room]:
            del chat_auth[room]

@app.websocket("/ws/signal/{room}")
async def signal_ws(websocket: WebSocket, room: str):
    """
    P2P signalling WebSocket - properly room-scoped.
    """
    client_ip = websocket.client.host
    await websocket.accept()
    
    pwd = websocket.query_params.get("pwd")
    try:
        auth_or_pwd(room, pwd, None, request=None)
    except HTTPException:
        await websocket.close(code=1008)
        return
    
    peer_id: str | None = None
    
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            
            if msg.get("type") == "join":
                peer_id = msg["id"]
                # FIXED: Register to THIS room only
                signal_rooms.setdefault(room, {})[peer_id] = websocket
                
                others = [pid for pid in signal_rooms[room] if pid != peer_id]
                await websocket.send_json({"type": "peer_list", "peers": others})
                
                # Notify others in THIS room
                for pid, ws in signal_rooms[room].items():
                    if pid != peer_id:
                        await ws.send_json({"type": "new_peer", "id": peer_id})
                continue
            
            # Direct message
            target = msg.get("to")
            if target and room in signal_rooms and target in signal_rooms[room]:
                await signal_rooms[room][target].send_json(msg)
                continue
            
            # FIXED: Broadcast only to THIS room
            for pid, ws in signal_rooms.get(room, {}).items():
                if pid != peer_id:
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        signal_rooms[room].pop(pid, None)
    
    except WebSocketDisconnect:
        # FIXED: Clean up only from THIS room
        if peer_id and room in signal_rooms:
            signal_rooms[room].pop(peer_id, None)
            if not signal_rooms[room]:
                del signal_rooms[room]

@app.websocket("/ws/{room}")
async def update_ws(websocket: WebSocket, room: str):
    """
    File update WebSocket - properly room-scoped.
    """
    await websocket.accept()
    
    # FIXED: Register to THIS room only
    update_rooms.setdefault(room, []).append(websocket)
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        # FIXED: Clean up only from THIS room
        if room in update_rooms:
            update_rooms[room].remove(websocket)
            if not update_rooms[room]:
                del update_rooms[room]

async def notify_update(room: str):
    """Notify only the clients in THIS specific room."""
    if room not in update_rooms:
        return
    
    for ws in update_rooms[room][:]:
        try:
            await ws.send_text("UPDATE")
        except Exception:
            update_rooms[room].remove(ws)

# ----------------------------------------------------------------------
# LEGACY COMPATIBILITY
# ----------------------------------------------------------------------

@app.post("/upload")
async def upload_compat(room: str, file: UploadFile = File(...)):
    return await upload_chunk(
        room=room,
        filename=file.filename,
        chunk_index=0,
        total_chunks=1,
        file=file,
        checksum=None,
        pwd=None,
        auth_token=None,
        request=None,
    )

# ----------------------------------------------------------------------
# CATCH-ALL ROUTE FOR ROOMS (must be last route before static mount)
# ----------------------------------------------------------------------

@app.get("/{room_name}")
async def serve_room(room_name: str):
    """
    Catch-all route that serves index.html for any room URL.
    This allows URLs like /kjnbn, /room1, /my-room to work.
    
    Note: This must be defined AFTER all other API routes but BEFORE
    the static files mount, otherwise it will intercept API calls.
    """
    # Don't intercept drive mounts
    if room_name in DRIVE_INFO:
        raise HTTPException(status_code=404)
    
    index_path = BASE_DIR / "static" / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="index.html not found")
    
    return FileResponse(index_path, media_type="text/html")

# ----------------------------------------------------------------------
# STATIC FILES (JavaScript, CSS, etc.)
# ----------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static"), name="static")

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="FastAPI P2P File Sharing")
    parser.add_argument("--drive", action="append", default=[])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    
    for p in args.drive:
        add_drive(Path(p).expanduser().resolve())
    
    threading.Thread(target=console_thread, daemon=True).start()
    
    ip = get_local_ip()
    print(f"\n🚀 Server ready → http://{ip}:{args.port}\n")
    
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=args.port, log_level="warning")
