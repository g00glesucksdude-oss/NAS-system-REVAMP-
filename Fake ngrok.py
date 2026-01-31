import socket
import threading

# --- CONFIGURATION ---
PUBLIC_PORT = 8000       # The port the network sees (the "Golden" one)
INTERNAL_PORT = 5000     # CHANGE THIS to whatever port your NAS script uses
LOCAL_HOST = "127.0.0.1"

def handle_client(client_socket):
    # Connect to your actual NAS script running locally
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server_socket.connect((LOCAL_HOST, INTERNAL_PORT))
    except Exception as e:
        print(f"[!] Target script not running on {INTERNAL_PORT}: {e}")
        client_socket.close()
        return

    # Logic: Bi-directional data transfer (The Bridge)
    def bridge(src, dest):
        try:
            while True:
                data = src.recv(4096)
                if not data: break
                dest.sendall(data)
        except:
            pass
        finally:
            src.close()
            dest.close()

    # Start two threads to move data both ways simultaneously
    threading.Thread(target=bridge, args=(client_socket, server_socket)).start()
    threading.Thread(target=bridge, args=(server_socket, client_socket)).start()

def start_tunnel():
    # Bind to 0.0.0.0 to be visible on the network
    proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy.bind(("0.0.0.0", PUBLIC_PORT))
    proxy.listen(10)
    
    print(f"[*] Local Tunnel Active: http://192.168.1.20:{PUBLIC_PORT} -> {INTERNAL_PORT}")
    
    while True:
        client_sock, addr = proxy.accept()
        threading.Thread(target=handle_client, args=(client_sock,)).start()

if __name__ == "__main__":
    start_tunnel()
