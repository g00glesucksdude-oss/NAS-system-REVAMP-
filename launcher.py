import time
import sys
import subprocess
import threading
import msvcrt  # Windows-only for keypress detection
from pathlib import Path

GREEN = "\033[92m"
RESET = "\033[0m"

# Flag to instantly finish current file
instant_dump = False

def key_listener():
    """Listen for keypresses to instantly dump the rest of the file."""
    global instant_dump
    while True:
        if msvcrt.kbhit():
            msvcrt.getch()  # consume the key
            instant_dump = True

def type_out_file(filepath):
    """Print file contents with typing effect unless interrupted."""
    global instant_dump
    if not Path(filepath).exists():
        print(f"{filepath} not found, skipping...")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
        if not instant_dump:
            # Normal slow typing until a key is pressed
            for char in content:
                if instant_dump:
                    # Instantly dump the rest of the file
                    sys.stdout.write(GREEN + content[content.index(char):] + RESET)
                    sys.stdout.flush()
                    break
                sys.stdout.write(GREEN + char + RESET)
                sys.stdout.flush()
                time.sleep(0.002)
        else:
            # If key pressed, dump everything at once
            sys.stdout.write(GREEN + content + RESET)
            sys.stdout.flush()
    print("\n")

import os

def loading_animation():
    print(GREEN + "=== Reading server.py ===" + RESET)
    type_out_file("server.py")
    print(GREEN + "\n=== Reading static/index.html ===" + RESET)
    type_out_file(os.path.join("static", "index.html"))
    print(GREEN + "\n=== Reading static/app.js ===" + RESET)
    type_out_file(os.path.join("static", "app.js"))
    print("\n😂 Relax, it’s just an animation!\n")

def run_server():
    subprocess.run(["uvicorn", "server:app", "--reload"])

if __name__ == "__main__":
    # Start key listener thread
    listener = threading.Thread(target=key_listener, daemon=True)
    listener.start()

    loading_animation()
    run_server()
