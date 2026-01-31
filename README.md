# NAS-system-REVAMP-
a revamp of the project i made weeks ago (2 weeks)

more info:

📦 House‑Hold Archive & Streams
A self‑hosted, LAN‑only file‑vault with:

Feature	What it does
Chunked upload / download – 4 MiB chunks, resumable, cancelable	
Password‑protected rooms – one‑time pass:… entry, short‑lived auth token (≈ 1 h)	
Read‑only rooms – list & download only, uploads / deletions blocked	
Per‑room chat – WebSocket, system messages, optional token reuse	
P2P swarm – WebRTC (simple‑peer) for parallel chunk exchange (ShareIt‑style)	
Real‑folder navigation for external drives – /streams/browse gives a hierarchical view (breadcrumb, “up”, folders)	
Lock‑out after N bad password attempts (default 5 attempts → 5 min block)	
Console REPL – add/remove drives, set passwords, toggle read‑only, reload state on‑the‑fly	
Zero external build tools – plain HTML/JS UI, pure Python server	
Why “LAN‑only”?
All traffic stays in your local network (router / Wi‑Fi Direct). You get the raw Wi‑Fi 5/6 bandwidth (≈ 200‑400 Mbps) without the Internet bottleneck.

Table of Contents
Prerequisites
Installation
Configuration (persist.json)
Running the server
Using the UI
Archive tab (upload / download)
Streams tab (real‑folder view)
Chat & authentication workflow
Managing rooms via the REPL
Security notes
Known issues & troubleshooting
Contributing
License
Prerequisites
Requirement	Minimum version
Python	3.9+
pip	any recent version
OS	Windows / macOS / Linux (any platform that can run FastAPI)
Browser	Modern desktop browsers (Chrome, Edge, Firefox, Safari) – the UI relies on WebRTC, fetch, WebSocket, localStorage.
No Node.js or build tools are required – the UI is pure HTML + JavaScript.

Installation
# 1️⃣ Clone the repo (or copy the files into a folder)
git clone https://github.com/your‑org/house‑hold‑archive.git
cd house‑hold‑archive

# 2️⃣ (Optional) create a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3️⃣ Install required Python packages
pip install -r requirements.txt
requirements.txt contains:

fastapi>=0.112.0
uvicorn[standard]>=0.30.0
aiofiles>=24.1.0
python-multipart>=0.0.9
(If you prefer a one‑click installer, you can use the supplied install_requirements.cmd batch file for Windows – it upgrades pip and installs the same packages.)

Configuration – persist.json
All persistent data lives in persist.json (next to server.py).
If the file does not exist the server starts with no drives, no passwords, no read‑only rooms.

Sample persist.json
{
  "drives": {
    "downloads": {
      "path": "C:/Users/g00gl/Downloads",
      "display": "Downloads"
    },
    "movies": {
      "path": "D:/Movies",
      "display": "Movies Collection"
    }
  },

  "passwords": {
    "secretroom": "mySecret123",
    "publicroom": "open"
  },

  "readonly_rooms": [
    "secretroom"               // secretroom can be listed & downloaded, but not modified
  ]
}
Field definitions

Field	Description
drives.<mount_name>.path	Absolute path to a folder that will be exposed under the Streams tab.
drives.<mount_name>.display	Friendly name shown in the Streams drive selector.
passwords.<room>	If present, the room requires a password (pass:YOUR_PASSWORD).
readonly_rooms (array)	Rooms listed here cannot be modified – uploads and deletions always return 403 Room is read‑only. Listing and downloading still work.
Updating the config at runtime
You can edit persist.json while the server is running and then run the reload command in the built‑in REPL (see below) or simply restart the server.

Running the server
# Basic start (no extra drives)
python server.py

# Add drives at start‑up (they’ll also be persisted)
python server.py --drive "C:/Users/g00gl/Movies" --drive "D:/Music"

# Custom port
python server.py --port 9000
When the server starts you’ll see something like:

🚀  Server ready → http://192.168.1.25:8000 (LAN only)
You can now open a browser on any device in the same LAN and navigate to that address.

Environment variable STREAM_DRIVES
Alternatively you can expose drives via an env‑var:

export STREAM_DRIVES="C:/Videos;D:/Documents"
python server.py
Each path is separated by the OS‑specific path separator (; on Windows, : on POSIX).

Using the UI
Open a browser to:

http://<HOST_IP>:<PORT>/<room_name>
If you omit a room name the default is main.

Archive tab (upload / download)
Action	How
Upload	Drag‑&‑drop files onto the large dashed area or click the file selector. Files are split into 4 MiB chunks and uploaded sequentially.
Cancel upload	Click the red Cancel button next to the progress bar; the client aborts the request and tells the server to delete the temporary chunk folder.
Download	Click the P2P button next to a file. The client fetches file metadata, then tries to get each chunk from peers; missing chunks fall back to HTTP from the server.
Delete	Click the 🗑 button. If the room is listed in readonly_rooms you’ll see Room is read‑only. Otherwise the file is removed and the UI refreshes automatically.
Chat	Type a message in the box at the bottom.
Authentication	If the room requires a password you’ll see a system banner – type pass:YOUR_PASSWORD (no username prefix) and press Enter. After a successful login you’ll see a system message “Password accepted …”. The server then sends a short‑lived auth token; the client stores it in localStorage and auto‑reuses it on reloads.
Streams tab (real‑folder view)
Select a drive from the dropdown.
The UI calls /streams/browse and shows a breadcrumb (Root / folder / …).
Click a folder name to enter it; an “⬆️ Up” button appears to go back.
Files are listed with a P2P download button (same mechanism as the Archive tab).
The search box filters the files in the current folder instantly (client‑side).
Chat & authentication workflow
On a password‑protected room the server sends:

SYSTEM: Please type pass:YOUR_PASSWORD to unlock this room.
Enter the password in the chat input (or via the modal that appears on page load).

The client sends the raw string pass:myPassword.

The server validates it, creates a short‑lived token (auth_token), and sends:

{"type":"auth","token":"<token_string>"}
The client stores the token, marks the UI as authenticated, and displays a system message ✅ Password accepted ….

All further HTTP requests automatically include ?auth_token=<token>; the server checks the token instead of the password.

Tokens expire after 1 hour (configurable in server.py). After expiration the UI falls back to the password prompt again.

Managing rooms via the REPL
When server.py starts it launches a tiny REPL in a background thread. Type a command and press Enter.

Command	What it does
adddrive <path>	Mount a new external folder as a stream (adds to persist.json).
rmdrive <mount_name>	Unmount a previously added drive.
listdrives	Show all currently mounted drives.
setpwd <room> <password>	Protect a room with a password.
rempwd <room>	Remove password protection from a room.
setreadonly <room>	Mark a room as read‑only (uploads/deletes blocked).
unsetreadonly <room>	Remove the read‑only flag.
reload	Reload persist.json without restarting the server (useful after manual edits).
exit / quit	Stop the server process.
Example session

> adddrive "C:/Users/g00gl/Movies"
> setpwd secretroom mySecret123
> setreadonly secretroom
> reload
After reload, any open browsers will notice the new state on the next /list or /UPDATE request.

Security notes
Issue	Mitigation
Password storage	Passwords are stored plain‑text in persist.json. This is fine for a LAN‑only tool but you can replace _load_passwords_from_file() with a hash‑check (e.g., Argon2) if needed.
Token leakage	Tokens are saved in localStorage (per‑room). They’re only sent to the same origin (http://host:port). Do not expose the server to the public Internet.
Lock‑out	After 5 failed attempts the client IP is blocked for 5 minutes (MAX_PWD_ATTEMPTS / LOCKOUT_SECONDS).
Read‑only enforcement	The server checks READONLY_ROOMS on uploads and deletions; listing and downloading remain allowed.
CORS	The server enables allow_origins=["*"] for simplicity (the UI is served from the same origin). Tighten this if you embed the UI elsewhere.
WebRTC	P2P connections use random UDP ports; make sure your local firewall allows inbound/outbound UDP on the LAN.
Known issues & troubleshooting
Symptom	Likely cause	Fix
Password modal never appears	The room is public (no entry in passwords).	Add a password entry via persist.json or the REPL (setpwd <room> <pwd>).
After entering password, UI still asks for password	You attempted an upload or delete in a read‑only room, which returns 403; the UI treats any 403 as “need auth”.	Remove the room from readonly_rooms (unsetreadonly <room>) or just view/download files.
All uploads fail with “Room is read‑only” even though the room isn’t read‑only	The in‑memory READONLY_ROOMS set wasn’t refreshed after editing persist.json.	Run reload in the REPL or restart the server.
P2P connections never form	Firewall blocks UDP, or peers are on separate subnets.	Ensure all clients are on the same LAN segment; open UDP ports in the OS firewall.
/streams/browse returns 404 for a folder that exists	Path contains leading/trailing slashes or a typo.	Use the UI navigation (it always builds a clean path).
(again generated by ai since i is not typing this shi)
Chat messages appear duplicated	You have the page open in multiple tabs – each tab has its own WebSocket.	Expected; each tab is a separate participant.
Token expires sooner than 1 hour	System clock mismatch or manual modification of auth_tokens.	Tokens are generated with time.time() + 3600. Adjust the value in auth_or_pwd if you need a longer lifetime.
