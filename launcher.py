import time
import sys
import subprocess

def loading_animation():
    code_lines = [
        "Reading server.py...",
        "Parsing FastAPI routes...",
        "Loading dependencies...",
        "Spinning up Uvicorn..."
    ]
    for line in code_lines:
        for char in line:
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(0.05)
        print()
        time.sleep(0.3)
    print("\n😂 Just an animation, don’t worry!\n")

def run_server():
    # Launch FastAPI app from server.py
    subprocess.run(["uvicorn", "server:app", "--reload"])

if __name__ == "__main__":
    loading_animation()
    run_server()
