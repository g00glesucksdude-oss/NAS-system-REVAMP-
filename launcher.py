import time
import sys
import subprocess
from pathlib import Path

# ANSI escape code for green text
GREEN = "\033[92m"
RESET = "\033[0m"

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
                time.sleep(0.002)  # adjust speed here
            # small pause at end of each line
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
    loading_animation()
    run_server()
