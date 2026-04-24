"""
start.py — Launch the Flask app and ngrok tunnel with one command.

Usage:
    python start.py
"""

import subprocess
import sys
import os
import time
import signal
import threading

# ── Config ──────────────────────────────────────────────────────────────────
PORT        = int(os.environ.get("PORT", 5000))
NGROK_BIN   = os.environ.get("NGROK_BIN", "ngrok")   # override if not on PATH


def stream_output(proc, prefix):
    """Forward a subprocess's stdout/stderr to our stdout with a prefix."""
    for line in iter(proc.stdout.readline, b""):
        sys.stdout.write(f"[{prefix}] {line.decode(errors='replace')}")
        sys.stdout.flush()


def main():
    procs = []

    def shutdown(sig=None, frame=None):
        print("\n[start.py] Shutting down…")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── 1. Start Flask/SocketIO app ──────────────────────────────────────
    print(f"[start.py] Starting Flask app on port {PORT}…")
    flask_env = {**os.environ, "FLASK_DEBUG": os.environ.get("FLASK_DEBUG", "1")}
    flask_proc = subprocess.Popen(
        [sys.executable, "app.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=flask_env,
    )
    procs.append(flask_proc)
    threading.Thread(target=stream_output, args=(flask_proc, "flask"), daemon=True).start()

    # Give Flask a moment to bind its port before starting ngrok
    time.sleep(2)

    # ── 2. Start ngrok tunnel ────────────────────────────────────────────
    print(f"[start.py] Starting ngrok tunnel → localhost:{PORT}…")
    ngrok_proc = subprocess.Popen(
        [NGROK_BIN, "http", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    procs.append(ngrok_proc)
    threading.Thread(target=stream_output, args=(ngrok_proc, "ngrok"), daemon=True).start()

    # ── 3. Poll ngrok API until tunnel URL is available ──────────────────
    import urllib.request
    import json

    tunnel_url = None
    for _ in range(20):          # up to ~10 s
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                data = json.loads(r.read())
                for t in data.get("tunnels", []):
                    if t.get("proto") == "https":
                        tunnel_url = t["public_url"]
                        break
        except Exception:
            pass
        if tunnel_url:
            break

    if tunnel_url:
        print(f"\n{'='*60}")
        print(f"  App running at : http://localhost:{PORT}")
        print(f"  ngrok URL      : {tunnel_url}")

        # ── Generate QR code ────────────────────────────────────────────
        try:
            import qrcode
            from datetime import datetime

            qr_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrcodes")
            os.makedirs(qr_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            qr_path   = os.path.join(qr_dir, f"ngrok_{timestamp}.png")
            latest    = os.path.join(qr_dir, "latest.png")

            img = qrcode.make(tunnel_url)
            img.save(qr_path)
            # Always overwrite 'latest.png' so it's easy to find
            img.save(latest)

            print(f"  QR code saved  : {qr_path}")
            print(f"  (also saved as): {latest}")
        except Exception as e:
            print(f"  [QR] Could not generate QR code: {e}")

        print(f"{'='*60}\n")
    else:
        print("[start.py] Could not read tunnel URL from ngrok API — check ngrok output above.")

    # ── 4. Wait until either process exits ──────────────────────────────
    while True:
        if flask_proc.poll() is not None:
            print("[start.py] Flask exited. Stopping ngrok.")
            shutdown()
        if ngrok_proc.poll() is not None:
            print("[start.py] ngrok exited. Stopping Flask.")
            shutdown()
        time.sleep(1)


if __name__ == "__main__":
    main()
