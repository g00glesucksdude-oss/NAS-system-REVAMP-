import time
import sys
import subprocess
import threading
import msvcrt  # Windows-only for keypress detection
from pathlib import Path

GREEN = "\033[92m"
RESET = "\033[0m"

# Global speed multiplier
speed_factor = 1.0

def key_listener():
    """Listen for random keypresses to speed up animation."""
    global speed_factor
    while True:
        if msvcrt.kbhit():
            msvcrt.getch()  # consume the key
            speed_factor = max(0.1, speed_factor * 0.7)  # speed up typing

def type_out_file(filepath):
    """Print file contents character by character in green."""
    if not Path(filepath).exists():
        print(f"{filepath} not found, skipping...")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            for char in line:
                sys.stdout.write(GREEN + char + RESET)
                sys.stdout.flush()
                time.sleep(0.002 * speed_factor)
            time.sleep(0.05 * speed_factor)

def loading_animation():
    print(GREEN + "=== Reading server.py ===" + RESET)
    type_out_file("server.py")
    print(GREEN + "\n=== Reading index.html ===" + RESET)
    type_out_file("index.html")
    print("\n😂 Relax, it’s just an animation!\n")

def run_server():
    subprocess.run(["uvicorn", "server:app", "--reload"])

if __name__ == "__main__":
    # Start key listener thread
    listener = threading.Thread(target=key_listener, daemon=True)
    listener.start()

    loading_animation()
    run_server()
