import time
import sys
import subprocess
import threading
import msvcrt  # Windows-only for keypress detection
from pathlib import Path

GREEN = "\033[92m"
RESET = "\033[0m"

# Flag to skip typing and instantly finish line
skip_now = False

def key_listener():
    """Listen for keypresses to instantly finish current line."""
    global skip_now
    while True:
        if msvcrt.kbhit():
            msvcrt.getch()  # consume the key
            skip_now = True

def type_out_file(filepath):
    """Print file contents line by line with typing effect."""
    global skip_now
    if not Path(filepath).exists():
        print(f"{filepath} not found, skipping...")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            skip_now = False
            start_time = time.time()
            for i, char in enumerate(line):
                if skip_now:
                    # Instantly finish the line but still take ~2 seconds total
                    sys.stdout.write(GREEN + line[i:] + RESET)
                    sys.stdout.flush()
                    # Wait until 2 seconds have passed since line started
                    while time.time() - start_time < 2.0:
                        time.sleep(0.01)
                    break
                sys.stdout.write(GREEN + char + RESET)
                sys.stdout.flush()
                time.sleep(0.002)
            print()  # newline after each line
            time.sleep(0.05)

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
