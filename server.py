#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
FastAPI “origin + tracker + chat hub” – READ‑ONLY BUG FIXED

Only one line changed:
    the DELETE endpoint now checks READONLY_ROOMS and returns 403
    when the room is read‑only.

All other features (password lock‑out, token auth, P2P signalling,
real‑folder streaming, upload cancellation, etc.) are unchanged.
"""

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
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
CHUNK_SIZE = 4 * 1024 * 1024                     # 4 MiB – must match client
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_ROOT = BASE_DIR / "uploads"                # archive files per room
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

PERSIST_FILE = BASE_DIR / "persist.json"           # drives + passwords + readonly

MAX_PWD_ATTEMPTS = 5                            # lock‑out after N bad attempts
LOCKOUT_SECONDS = 300                           # lock‑out time (5 min)

# ----------------------------------------------------------------------
# GLOBAL STATE
# ----------------------------------------------------------------------
chat_rooms:   Dict[str, List[WebSocket]] = {}                # room → all chat sockets
chat_auth:    Dict[str, Set[WebSocket]] = {}                 # room → authenticated chat sockets
signal_rooms: Dict[str, Dict[str, WebSocket]] = {}          # room → {peer_id: WS}
update_rooms: Dict[str, List[WebSocket]] = {}                # room → update WS list
uploads_state: Dict[str, dict] = {}                          # "room/filename" → temp state

# drives: mount_name → {"path":Path,"display":str}
DRIVE_INFO: Dict[str, dict] = {}

# In‑memory cache – kept only for the REPL, auth checks read the file directly
ROOM_PASSWORDS: Dict[str, str] = {}

# read‑only rooms (persisted)
READONLY_ROOMS: Set[str] = set()

# short‑lived auth tokens (issued after a correct password)
auth_tokens: Dict[str, dict] = {}      # token → {"room": str, "expires": ts}

# lock‑out counters
pwd_attempts: Dict[str, Dict[str, dict]] = {}   # room → ip → {count, locked_until}

# ----------------------------------------------------------------------
# HELPERS – persistence
# ----------------------------------------------------------------------
def get_local_ip() -> str:
    """Return the first non‑loopback IP address of the host."""
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
    """Convenient wrapper for SHA‑256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def meta_path(file_path: Path) -> Path:
    """Side‑car JSON that stores size, chunk‑size, total‑chunks, per‑chunk hashes."""
    return file_path.with_suffix(file_path.suffix + ".meta.json")


def _sanitize_mount_name(name: str) -> str:
    name = name.strip().replace(" ", "_")
    name = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    return name or "drive"


def _load_passwords_from_file() -> Dict[str, str]:
    """Read the *password* section from persist.json (on‑demand)."""
    if not PERSIST_FILE.is_file():
        return {}
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("passwords", {})
    except Exception:
        return {}


def load_state():
    """Load drives, passwords and readonly flags from persist.json."""
    global DRIVE_INFO, ROOM_PASSWORDS, READONLY_ROOMS
    if not PERSIST_FILE.is_file():
        print("[i] No persisted state – starting fresh")
        return
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # ----- drives -----
        for name, info in data.get("drives", {}).items():
            path = Path(info["path"])
            if path.is_dir():
                DRIVE_INFO[name] = {"path": path, "display": info.get("display", name)}
                app.mount(f"/{name}", StaticFiles(directory=path), name=name)

        # ----- passwords -----
        ROOM_PASSWORDS = data.get("passwords", {})

        # ----- readonly -----
        READONLY_ROOMS = set(data.get("readonly_rooms", []))
        print("[i] Persisted state loaded.")
    except Exception as exc:
        print(f"[!] Failed to load persisted state: {exc}")


def save_state():
    """Write the current drives, passwords and readonly flags back to persist.json."""
    data = {
        "drives": {
            name: {"path": str(info["path"]), "display": info["display"]}
            for name, info in DRIVE_INFO.items()
        },
        "passwords": ROOM_PASSWORDS,
        "readonly_rooms": list(READONLY_ROOMS),
    }
    try:
        with PERSIST_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[!] Could not persist state: {exc}")


def add_drive(path: Path):
    """Mount a new external folder as a read‑only drive."""
    if not path.is_dir():
        print(f"[!] Drive path does not exist or is not a directory → {path}")
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
    """Unmount a previously added drive."""
    if name not in DRIVE_INFO:
        print(f"[!] No such drive {name}")
        return
    app.router.routes = [
        r for r in app.router.routes if not (hasattr(r, "path") and r.path.startswith(f"/{name}"))
    ]
    DRIVE_INFO.pop(name)
    print(f"[-] Unmounted drive {name}")
    save_state()


# ----------------------------------------------------------------------
# REPL – tiny console (runs in a daemon thread)
# ----------------------------------------------------------------------
def console_thread():
    help_msg = """
Commands (type the word and press ENTER):
  adddrive <path>           – mount an external folder (quotes allowed)
  rmdrive <mount_name>     – unmount a previously added drive
  listdrives               – show currently mounted drives
  setpwd <room> <pwd>      – protect a room with a password (pwd may contain spaces)
  rempwd <room>            – remove password protection from a room
  setreadonly <room>       – mark a room read‑only (uploads & deletions blocked)
  unsetreadonly <room>     – remove read‑only flag
  reload                    – reload persisted state from persist.json
  exit / quit               – stop the server
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
            print(f"[i] Password set for room “{room}”.")
            continue

        if cmd == "rempwd" and len(parts) == 2:
            ROOM_PASSWORDS.pop(parts[1], None)
            save_state()
            print(f"[i] Password removed for room “{parts[1]}”.")
            continue

        if cmd == "setreadonly" and len(parts) == 2:
            READONLY_ROOMS.add(parts[1])
            save_state()
            print(f"[i] Room “{parts[1]}” set to read‑only.")
            continue

        if cmd == "unsetreadonly" and len(parts) == 2:
            READONLY_ROOMS.discard(parts[1])
            save_state()
            print(f"[i] Read‑only flag removed from room “{parts[1]}”.")
            continue

        if cmd == "reload":
            load_state()
            print("[i] State reloaded from persist.json")
            continue

        if cmd in ("exit", "quit"):
            print("[i] Shutting down …")
            os._exit(0)

        print("[!] Unknown command.")
        print(help_msg)


# ----------------------------------------------------------------------
# FASTAPI APP & MIDDLEWARE
# ----------------------------------------------------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load persisted state before anything else
load_state()

# Mount any drives supplied via the environment variable at start‑up
env_drives = [
    Path(p).expanduser().resolve()
    for p in os.getenv("STREAM_DRIVES", "").split(os.pathsep)
    if p
]
for p in env_drives:
    add_drive(p)


# ----------------------------------------------------------------------
# 0️⃣ Health‑check (used by the UI)
# ----------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Helper – does the room need a password? (reads file every call)
# ----------------------------------------------------------------------
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


# ----------------------------------------------------------------------
# Verify password (used by UI modal)
# ----------------------------------------------------------------------
@app.get("/verify_password")
async def verify_password(room: str, pwd: str):
    passwords = _load_passwords_from_file()
    required = passwords.get(room)
    if required is None:
        return {"valid": True}
    return {"valid": required == pwd}


# ----------------------------------------------------------------------
# Password handling helpers (lock‑out, attempts)
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

    # No password required → success
    if required is None:
        _track_attempt(room, client_ip, True)
        return

    # Password required – verify lock‑out first
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

    # Correct password – reset counters
    _track_attempt(room, client_ip, True)


def verify_room(room: str, pwd: str | None, client_ip: str | None):
    passwords = _load_passwords_from_file()
    if room in passwords:
        if client_ip is None:
            raise HTTPException(status_code=400, detail="Client IP required")
        check_password(room, pwd, client_ip)


# ----------------------------------------------------------------------
# Unified auth helper – token OR password (with lock‑out)
# ----------------------------------------------------------------------
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
# 1️⃣ Chunked upload (POST /upload_chunk)
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

    # ---- read‑only guard ----
    if room in READONLY_ROOMS:
        raise HTTPException(status_code=403, detail="Room is read-only")

    if not (0 <= chunk_index < total_chunks):
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    room_dir = UPLOAD_ROOT / room
    tmp_dir = room_dir / f"{filename}.tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    data = await file.read()
    if checksum and checksum != sha256_bytes(data):
        raise HTTPException(status_code=400, detail="Checksum mismatch")

    # store chunk on disk
    chunk_path = tmp_dir / f"chunk_{chunk_index}"
    async with aiofiles.open(chunk_path, "wb") as out:
        await out.write(data)

    # update temporary in‑memory state
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

    # If we have *all* chunks → re‑assemble the final file
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

        # side‑car meta (size, chunk‑size, per‑chunk hashes)
        meta = {
            "filename": filename,
            "size": final_path.stat().st_size,
            "chunk_size": CHUNK_SIZE,
            "total_chunks": total_chunks,
            "chunk_hashes": state["hashes"],
        }
        async with aiofiles.open(meta_path(final_path), "w") as f:
            await f.write(json.dumps(meta))

        # clean‑up temp folder + in‑memory record
        shutil.rmtree(tmp_dir, ignore_errors=True)
        uploads_state.pop(key, None)

        # notify UI that the file list changed
        await notify_update(room)

    return {"status": "ok", "chunk_index": chunk_index}


# ----------------------------------------------------------------------
# 2️⃣ File metadata (archive & streams)
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


# ----------------------------------------------------------------------
# 3️⃣ Chunked download (archive) – server‑first
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


# ----------------------------------------------------------------------
# 4️⃣ Full‑file fallback (GET /download)
# ----------------------------------------------------------------------
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
# 5️⃣ Archive helpers – list, delete (meta files hidden)
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
        return {"files": []}
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

    # ---------- NEW: read‑only guard ----------
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


# ----------------------------------------------------------------------
# 6️⃣ Streams – list drives, list files, fetch data (meta hidden)
# ----------------------------------------------------------------------
@app.get("/streams/drives")
async def list_drives():
    """Return a JSON list of the mounted drives (display name + mount name + availability)."""
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


# Legacy flat‑list (kept for backward compatibility)
@app.get("/streams/list")
async def streams_list(drive: str = None):
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")
    base = DRIVE_INFO[drive]["path"]
    files = []
    for root, _, fnames in os.walk(base):
        for fn in fnames:
            full = Path(root) / fn
            rel = full.relative_to(base).as_posix()
            if fn.endswith(".meta.json"):
                continue
            files.append(
                {"name": fn, "path": rel, "size": full.stat().st_size}
            )
    return {"drive": drive, "files": files}


# NEW endpoint – real folder hierarchy (used by the UI)
@app.get("/streams/browse")
async def streams_browse(drive: str, dir_path: str = ""):
    """
    Return the contents of a directory inside the chosen drive.
    Response:
    {
        "drive": "<mount>",
        "current": "<relative_path_without_leading_slash>",   # empty string = root
        "folders": ["sub1","sub2",...],
        "files": [{"name":"file.mp4","size":12345,"path":"sub1/file.mp4"}, ...]
    }
    """
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")

    base = DRIVE_INFO[drive]["path"]
    clean = dir_path.strip("/")                 # normalise
    target = base / clean if clean else base

    if not target.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Requested path is not a directory")

    folders: List[str] = []
    files: List[dict] = []

    for entry in target.iterdir():
        if entry.is_dir():
            folders.append(entry.name)
        elif entry.is_file() and not entry.name.endswith(".meta.json"):
            rel = entry.relative_to(base).as_posix()
            files.append(
                {"name": entry.name, "size": entry.stat().st_size, "path": rel}
            )

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

    # whole‑file fallback
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


# ----------------------------------------------------------------------
# 7️⃣ Chat websocket – timestamps, broadcast to every client (incl. sender)
# ----------------------------------------------------------------------
@app.websocket("/ws/chat/{room}")
async def chat_ws(websocket: WebSocket, room: str):
    """
    *If a valid auth token is supplied via the query string* → the socket is
    automatically authenticated (no password prompt).
    *If the room has a password* → normal flow (type “pass:…”, receive token).
    *If the room is public* → socket is authenticated immediately.
    """
    client_ip = websocket.client.host
    await websocket.accept()

    # Register the socket (all sockets)
    chat_rooms.setdefault(room, []).append(websocket)
    chat_auth.setdefault(room, set())

    # ---------------------------------------------------------------
    # 1️⃣ Try token authentication (query‑param auth_token)
    # ---------------------------------------------------------------
    token = websocket.query_params.get("auth_token")
    password_required = None

    if token:
        info = auth_tokens.get(token)
        if info and info["room"] == room and info.get("expires", 0) > time.time():
            chat_auth[room].add(websocket)
            await websocket.send_text("SYSTEM: Token authentication successful.")
            password_required = False
        else:
            await websocket.send_text(
                "SYSTEM: Invalid/expired token – please type pass:YOUR_PASSWORD to unlock this room."
            )
            passwords = _load_passwords_from_file()
            password_required = room in passwords
    else:
        # ---------------------------------------------------------------
        # 2️⃣ No token → ordinary password‑required logic
        # ---------------------------------------------------------------
        passwords = _load_passwords_from_file()
        password_required = room in passwords
        if not password_required:
            chat_auth[room].add(websocket)
            await websocket.send_text(
                "SYSTEM: No password required – you are authenticated."
            )
        else:
            await websocket.send_text(
                "SYSTEM: Please type pass:YOUR_PASSWORD to unlock this room."
            )

    try:
        while True:
            raw = await websocket.receive_text()

            # ---------------------------------------------------------------
            # 1️⃣ Private‑room password handling (unchanged)
            # ---------------------------------------------------------------
            if password_required and raw.lower().startswith("pass:"):
                attempt_pwd = raw[5:].strip()
                try:
                    verify_room(room, attempt_pwd, client_ip)
                except HTTPException:
                    await websocket.send_text(
                        "SYSTEM: Invalid password or room locked."
                    )
                    continue

                # Successful login → issue a short‑lived token
                token = secrets.token_urlsafe(16)
                auth_tokens[token] = {"room": room, "expires": time.time() + 3600}
                chat_auth[room].add(websocket)

                await websocket.send_json({"type": "auth", "token": token})
                await websocket.send_text(
                    "SYSTEM: Password accepted. You may now chat, upload and download."
                )
                continue

            # ---------------------------------------------------------------
            # 2️⃣ Public‑room shortcut – no auth needed
            # ---------------------------------------------------------------
            if not password_required:
                chat_auth[room].add(websocket)

            # ---------------------------------------------------------------
            # 3️⃣ Enforce auth for private rooms only
            # ---------------------------------------------------------------
            if password_required and websocket not in chat_auth[room]:
                await websocket.send_text(
                    "SYSTEM: You must authenticate first (type pass:YOUR_PASSWORD)."
                )
                continue

            # ---------------------------------------------------------------
            # 4️⃣ Normal chat – broadcast to all *authenticated* peers
            # ---------------------------------------------------------------
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            broadcast = f"[{ts}] {raw}"
            for ws in list(chat_auth[room]):
                try:
                    await ws.send_text(broadcast)
                except Exception:
                    chat_auth[room].discard(ws)

    except WebSocketDisconnect:
        # Clean‑up
        chat_rooms[room].remove(websocket)
        chat_auth[room].discard(websocket)
        if not chat_rooms[room]:
            del chat_rooms[room]
        if not chat_auth[room]:
            del chat_auth[room]


# ----------------------------------------------------------------------
# 8️⃣ Signalling / Swarm websocket – tracks peers / progress (P2P ready)
# ----------------------------------------------------------------------
@app.websocket("/ws/signal/{room}")
async def signal_ws(websocket: WebSocket, room: str):
    client_ip = websocket.client.host
    await websocket.accept()
    pwd = websocket.query_params.get("pwd")
    # Enforce password (or token) on the signalling channel
    try:
        auth_or_pwd(room, pwd, None, request=None)
    except HTTPException:
        await websocket.close(code=1008)  # policy violation
        return

    peer_id: str | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            # ---------- JOIN ----------
            if msg.get("type") == "join":
                peer_id = msg["id"]
                signal_rooms.setdefault(room, {})[peer_id] = websocket

                others = [pid for pid in signal_rooms[room] if pid != peer_id]
                await websocket.send_json({"type": "peer_list", "peers": others})

                for pid, ws in signal_rooms[room].items():
                    if pid != peer_id:
                        await ws.send_json({"type": "new_peer", "id": peer_id})
                continue

            # ---------- DIRECT MESSAGE ----------
            target = msg.get("to")
            if target and room in signal_rooms and target in signal_rooms[room]:
                await signal_rooms[room][target].send_json(msg)
                continue

            # ---------- BROADCAST ----------
            for pid, ws in signal_rooms[room].items():
                if pid != peer_id:
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        signal_rooms[room].pop(pid, None)
    except WebSocketDisconnect:
        if peer_id and room in signal_rooms:
            signal_rooms[room].pop(peer_id, None)
            if not signal_rooms[room]:
                del signal_rooms[room]


# ----------------------------------------------------------------------
# 9️⃣ File‑list update websocket – pushes “UPDATE” when a file changes
# ----------------------------------------------------------------------
@app.websocket("/ws/{room}")
async def update_ws(websocket: WebSocket, room: str):
    await websocket.accept()
    update_rooms.setdefault(room, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep‑alive (no incoming msgs expected)
    except WebSocketDisconnect:
        update_rooms[room].remove(websocket)
        if not update_rooms[room]:
            del update_rooms[room]


async def notify_update(room: str):
    """Tell every UI listening on /ws/{room} to refresh its file list."""
    if room not in update_rooms:
        return
    for ws in update_rooms[room][:]:
        try:
            await ws.send_text("UPDATE")
        except Exception:
            update_rooms[room].remove(ws)


# ----------------------------------------------------------------------
# 10️⃣ Cancel upload endpoint – client can abort a multipart upload
# ----------------------------------------------------------------------
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
# 11️⃣ Compatibility wrappers (legacy UI)
# ----------------------------------------------------------------------
@app.post("/upload")
async def upload_compat(room: str, file: UploadFile = File(...)):
    """Legacy full‑file upload – forwards as a single chunk."""
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


@app.get("/file_info/{room}/{filename}")
async def file_info_compat(room: str, filename: str):
    return await file_info(room=room, filename=filename)


@app.get("/data/{room}/{filename}")
async def data_compat(room: str, filename: str, chunk_id: int = 0):
    return await download_chunk(room=room, filename=filename, chunk_id=chunk_id)


@app.get("/download")
async def download_compat(room: str, filename: str):
    return await download_full(room=room, filename=filename)


# ----------------------------------------------------------------------
# 12️⃣ Serve the SPA (index.html)
# ----------------------------------------------------------------------
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_root(full_path: str):
    index_file = BASE_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file, media_type="text/html")
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


# ----------------------------------------------------------------------
# 13️⃣ Run the server (starts console REPL in a daemon thread)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="FastAPI P2P Archive + External Drive Support"
    )
    parser.add_argument(
        "--drive",
        action="append",
        default=[],
        help="Path to an external folder to expose as a stream (can be used multiple times).",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default 8000).")
    args = parser.parse_args()

    # Add any drives supplied on the command line (they’ll also be persisted)
    for p in args.drive:
        add_drive(Path(p).expanduser().resolve())

    # start the REPL in a background thread
    threading.Thread(target=console_thread, daemon=True).start()

    ip = get_local_ip()
    print(f"\n🚀  Server ready → http://{ip}:{args.port} (LAN only)\n")
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
    )
