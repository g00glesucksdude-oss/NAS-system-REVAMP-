#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
FastAPI “origin + tracker + chat hub” – BROWSE FIX + LEGACY DOWNLOAD

All UI (HTML + JS) lives in ./static/ and is served as static files.
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
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Request,
)
from fastapi.responses import (
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

PERSIST_FILE = BASE_DIR / "persist.json"         # drives + passwords + readonly + settings

MAX_PWD_ATTEMPTS = 5                             # lock‑out after N bad attempts
LOCKOUT_SECONDS = 300                            # lock‑out time (5 min)

# Legacy download settings (configurable at runtime – default = 2 users)
MAX_LEGACY_USERS = 2
legacy_download_users: Set[str] = set()           # active legacy download client IPs

# ----------------------------------------------------------------------
# GLOBAL STATE
# ----------------------------------------------------------------------
chat_rooms:   Dict[str, List[WebSocket]] = {}               # room → all chat sockets
chat_auth:    Dict[str, Set[WebSocket]] = {}                # room → authenticated chat sockets
signal_rooms: Dict[str, Dict[str, WebSocket]] = {}           # room → {peer_id: WS}
update_rooms: Dict[str, List[WebSocket]] = {}                # room → update WS list
uploads_state: Dict[str, dict] = {}                          # "room/filename" → temp state

DRIVE_INFO: Dict[str, dict] = {}                              # mount_name → {"path":Path,"display":str}
ROOM_PASSWORDS: Dict[str, str] = {}                           # room → password (read from file each request)
READONLY_ROOMS: Set[str] = set()                             # immutable rooms
auth_tokens: Dict[str, dict] = {}                               # token → {"room":str,"expires":ts}
pwd_attempts: Dict[str, Dict[str, dict]] = {}               # room → ip → {count, locked_until}

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
    """Read only the passwords section from persist.json (fresh each call)."""
    if not PERSIST_FILE.is_file():
        return {}
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("passwords", {})
    except Exception:
        return {}


def load_state():
    """Load drives, passwords, readonly flags and settings from persist.json."""
    global DRIVE_INFO, ROOM_PASSWORDS, READONLY_ROOMS, MAX_LEGACY_USERS
    if not PERSIST_FILE.is_file():
        print("[i] No persisted state – starting fresh")
        return
    try:
        with PERSIST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # ---- drives -------------------------------------------------
        for name, info in data.get("drives", {}).items():
            path = Path(info["path"])
            if path.is_dir():
                DRIVE_INFO[name] = {"path": path, "display": info.get("display", name)}
                # The actual mount will be added after the FastAPI app is created
        # ---- passwords ----------------------------------------------
        ROOM_PASSWORDS = data.get("passwords", {})
        # ---- readonly -----------------------------------------------
        READONLY_ROOMS = set(data.get("readonly_rooms", []))
        # ---- settings -----------------------------------------------
        settings = data.get("settings", {})
        MAX_LEGACY_USERS = settings.get("max_legacy_users", 2)   # default 2
        print("[i] Persisted state loaded.")
    except Exception as exc:
        print(f"[!] Failed to load persisted state: {exc}")


def save_state():
    """Write the current drives, passwords, readonly flags and settings back to persist.json."""
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
    # Remove routes that start with this mount
    app.router.routes = [
        r for r in app.router.routes
        if not (hasattr(r, "path") and r.path.startswith(f"/{name}"))
    ]
    DRIVE_INFO.pop(name)
    print(f"[-] Unmounted drive {name}")
    save_state()


# ----------------------------------------------------------------------
# REPL – tiny admin console (runs in a daemon thread)
# ----------------------------------------------------------------------
def console_thread():
    help_msg = """
Commands (type the word and press ENTER):
  adddrive <path>           – mount an external folder (quotes allowed)
  rmdrive <mount_name>      – unmount a previously added drive
  listdrives                – show currently mounted drives
  setpwd <room> <pwd>       – protect a room with a password (pwd may contain spaces)
  rempwd <room>             – remove password protection from a room
  setreadonly <room>        – mark a room read‑only (uploads & deletions blocked)
  unsetreadonly <room>      – remove read‑only flag
  setlegacylimit <num>      – set max concurrent legacy downloads (default: 2)
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

        # ----- drive handling -----------------------------------------
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

        # ----- password handling ---------------------------------------
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

        # ----- read‑only handling --------------------------------------
        if cmd == "setreadonly" and len(parts) == 2:
            READONLY_ROOMS.add(parts[1])
            save_state()
            print(f'[i] Room "{parts[1]}" set to read‑only.')
            continue

        if cmd == "unsetreadonly" and len(parts) == 2:
            READONLY_ROOMS.discard(parts[1])
            save_state()
            print(f'[i] Read‑only flag removed from room "{parts[1]}".')
            continue

        # ----- legacy‑download limit ----------------------------------
        if cmd == "setlegacylimit" and len(parts) == 2:
            try:
                global MAX_LEGACY_USERS
                MAX_LEGACY_USERS = int(parts[1])
                save_state()
                print(f"[i] Max legacy download users set to {MAX_LEGACY_USERS}")
            except ValueError:
                print("[!] Invalid number")
            continue

        # ----- reload persisted state ----------------------------------
        if cmd == "reload":
            load_state()
            # Re‑mount any persisted drives (they are stored in DRIVE_INFO)
            for name, info in DRIVE_INFO.items():
                app.mount(f"/{name}", StaticFiles(directory=info["path"]), name=name)
            print("[i] State reloaded from persist.json")
            continue

        # ----- exit ----------------------------------------------------
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

# ----------------------------------------------------------------------
# Load persisted state BEFORE mounting any drives
# ----------------------------------------------------------------------
load_state()

# After loading, actually mount the drives
for name, info in DRIVE_INFO.items():
    app.mount(f"/{name}", StaticFiles(directory=info["path"]), name=name)

# ----------------------------------------------------------------------
# Mount any drives supplied via the environment variable at start‑up
# ----------------------------------------------------------------------
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
# Helper – does the room need a password? (reads file each call)
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

    # No password needed → success
    if required is None:
        _track_attempt(room, client_ip, True)
        return

    # Password required – first check lock‑out
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
        raise HTTPException(status_code=403, detail="Room is read‑only")

    if not (0 <= chunk_index < total_chunks):
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    room_dir = UPLOAD_ROOT / room
    tmp_dir = room_dir / f"{filename}.tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    data = await file.read()
    if checksum and checksum != sha256_bytes(data):
        raise HTTPException(status_code=400, detail="Checksum mismatch")

    # Store the chunk on disk
    chunk_path = tmp_dir / f"chunk_{chunk_index}"
    async with aiofiles.open(chunk_path, "wb") as out:
        await out.write(data)

    # Update in‑memory state
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

        # Write meta only if it changed (or does not exist)
        meta_file_path = meta_path(final_path)
        write_meta = True
        if meta_file_path.is_file():
            async with aiofiles.open(meta_file_path, "r") as f:
                existing = json.loads(await f.read())
            if existing.get("size") == meta["size"]:
                write_meta = False   # unchanged → skip
        if write_meta:
            async with aiofiles.open(meta_file_path, "w") as f:
                await f.write(json.dumps(meta))

        # clean‑up temp folder + in‑memory record
        shutil.rmtree(tmp_dir, ignore_errors=True)
        uploads_state.pop(key, None)

        # Notify UI that the file list changed
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
    """Return size‑information for a file inside a mounted drive."""
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
        "chunk_hashes": [],            # streams never store per‑chunk hashes on‑disk
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
# 4️⃣ Full‑file fallback (GET /download) – archive mode
# ----------------------------------------------------------------------
@app.get("/download")
async def download_full(
    room: str,
    filename: str,
    pwd: str | None = None,
    auth_token: str | None = None,
    request: Request = None,
):
    """Return the entire file (no chunking)."""
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

    # ---------- read‑only guard ----------
    if room in READONLY_ROOMS:
        raise HTTPException(status_code=403, detail="Room is read‑only")

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
    """Return a JSON list of the mounted drives."""
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


@app.get("/streams/list")
async def streams_list(drive: str = None):
    """Legacy flat list of all files in a drive (kept for backward compatibility)."""
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
            if fn.endswith(".meta.json"):
                continue
            full = Path(root) / fn
            rel = full.relative_to(base).as_posix()
            files.append({"name": fn, "path": rel, "size": full.stat().st_size})
    return {"drive": drive, "files": files}


# ----------------------------------------------------------------------
# 7️⃣ Browse – **fixed**: respects the folder you request
# ----------------------------------------------------------------------
@app.get("/streams/browse")
async def streams_browse(drive: str, dir_path: str = ""):
    """
    Return the contents of a directory inside the chosen drive.

    The endpoint accepts both the historic ``path=`` query‑parameter
    and the newer ``dir_path=`` parameter (the UI may use either).
    """
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")

    base = DRIVE_INFO[drive]["path"]

    # Normalise the incoming path – strip leading/trailing slashes
    clean = dir_path.strip("/").replace("\\", "/")
    target = base / clean if clean else base

    # Security – never allow escaping the mounted drive
    try:
        target = target.resolve()
        target.relative_to(base.resolve())
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=403, detail="Access denied: path outside drive")

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
            files.append({"name": entry.name, "size": entry.stat().st_size, "path": rel})

    return {
        "drive": drive,
        "current": clean,                 # empty string = root
        "folders": sorted(folders),
        "files": sorted(files, key=lambda f: f["name"]),
    }


# ----------------------------------------------------------------------
# 8️⃣ Legacy whole‑file download (concurrent‑user limit)
# ----------------------------------------------------------------------
@app.get("/streams/data_legacy")
async def streams_data_legacy(
    path: str,
    drive: str = None,
    request: Request = None,
):
    """
    Legacy whole‑file download.
    Limited to ``MAX_LEGACY_USERS`` concurrent downloads (default: 2).
    """
    if not DRIVE_INFO:
        raise HTTPException(status_code=404, detail="No drives mounted")
    if drive is None:
        drive = next(iter(DRIVE_INFO))
    if drive not in DRIVE_INFO:
        raise HTTPException(status_code=404, detail="Drive not found")

    client_ip = request.client.host if request else "unknown"

    # Enforce the limit
    if len(legacy_download_users) >= MAX_LEGACY_USERS and client_ip not in legacy_download_users:
        raise HTTPException(
            status_code=503,
            detail=f"Legacy download limit reached ({MAX_LEGACY_USERS} users). Try chunked download or wait.",
        )

    file_path = DRIVE_INFO[drive]["path"] / path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Register the client while the response streams
    legacy_download_users.add(client_ip)
    try:
        return FileResponse(file_path)
    finally:
        legacy_download_users.discard(client_ip)


# ----------------------------------------------------------------------
# 9️⃣ Chunked download for streams (default method)
# ----------------------------------------------------------------------
@app.get("/streams/data")
async def streams_data(path: str, drive: str = None, chunk_id: int = None):
    """Chunked download for streams (default method)."""
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
# 10️⃣ Settings endpoint (exposes server limits)
# ----------------------------------------------------------------------
@app.get("/settings")
async def get_settings():
    """Return a small JSON object that the UI can show."""
    return {
        "max_legacy_users": MAX_LEGACY_USERS,
        "current_legacy_users": len(legacy_download_users),
    }


# ----------------------------------------------------------------------
# 11️⃣ Chat websocket – timestamps, broadcast to every client (incl. sender)
# ----------------------------------------------------------------------
@app.websocket("/ws/chat/{room}")
async def chat_ws(websocket: WebSocket, room: str):
    """
    *If a valid auth token is supplied via the query string* → socket
    is automatically authenticated (no password prompt).
    *If the room has a password* → client must type ``pass:YOUR_PASSWORD``.
    *If the room is public* → socket is authenticated immediately.
    """
    client_ip = websocket.client.host
    await websocket.accept()

    # Register sockets (all + authenticated subset)
    chat_rooms.setdefault(room, []).append(websocket)
    chat_auth.setdefault(room, set())

    # ---------------------------------------------------------------
    # 1️⃣ Token authentication (query param)
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
                "SYSTEM: Invalid/expired token – please type pass:YOUR_PASSWORD"
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
            # 1️⃣ Private‑room password handling
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
# 12️⃣ Signalling / Swarm websocket – tracks peers / progress (P2P ready)
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
# 13️⃣ File‑list update websocket – pushes "UPDATE" when a file changes
# ----------------------------------------------------------------------
@app.websocket("/ws/{room}")
async def update_ws(websocket: WebSocket, room: str):
    await websocket.accept()
    update_rooms.setdefault(room, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep‑alive (no incoming messages expected)
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
# 14️⃣ Cancel upload endpoint – client can abort a multipart upload
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
# 15️⃣ Compatibility wrappers (legacy UI)
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
    """Legacy alias for /file_info."""
    return await file_info(room=room, filename=filename)


@app.get("/data/{room}/{filename}")
async def data_compat(room: str, filename: str, chunk_id: int = 0):
    """Legacy alias for /download_chunk."""
    return await download_chunk(room=room, filename=filename, chunk_id=chunk_id)


# ----------------------------------------------------------------------
# 16️⃣ Serve static UI (index.html + app.js)
# ----------------------------------------------------------------------
# All files in ./static/ are served at the root.
# `html=True` makes FastAPI return `index.html` for any path that does not match an API route.
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ----------------------------------------------------------------------
# 17️⃣ Run the server (starts console REPL in a daemon thread)
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

    # Add any drives supplied on the command line (they will also be persisted)
    for p in args.drive:
        add_drive(Path(p).expanduser().resolve())

    # start the REPL in a background daemon thread
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
