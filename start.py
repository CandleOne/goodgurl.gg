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
PORT             = int(os.environ.get("PORT", 5000))
CLOUDFLARED_BIN  = os.environ.get("CLOUDFLARED_BIN", "cloudflared")  # override if not on PATH


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

    # Give Flask a moment to bind its port before starting tunnel
    time.sleep(2)

    # ── 2. Start Cloudflare tunnel ───────────────────────────────────────
    print(f"[start.py] Starting Cloudflare tunnel → localhost:{PORT}…")
    cf_log_path = "/tmp/cloudflared.log"
    cf_log_file = open(cf_log_path, "w")
    cf_proc = subprocess.Popen(
        [CLOUDFLARED_BIN, "tunnel", "--url", f"http://localhost:{PORT}"],
        stdout=cf_log_file,
        stderr=cf_log_file,
    )
    procs.append(cf_proc)

    # ── 3. Poll cloudflared log for tunnel URL ───────────────────────────
    import re

    tunnel_url = None
    for _ in range(30):          # up to ~15 s
        time.sleep(0.5)
        try:
            with open(cf_log_path) as f:
                content = f.read()
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
            if match:
                tunnel_url = match.group(0)
                break
        except Exception:
            pass

    if tunnel_url:
        print(f"\n{'='*60}")
        print(f"  App running at    : http://localhost:{PORT}")
        print(f"  Cloudflare URL    : {tunnel_url}")

        # ── Generate QR code ────────────────────────────────────────────
        try:
            import qrcode
            from datetime import datetime

            qr_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrcodes")
            os.makedirs(qr_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            qr_path   = os.path.join(qr_dir, f"cloudflare_{timestamp}.png")
            latest    = os.path.join(qr_dir, "latest.png")

            img = qrcode.make(tunnel_url)
            img.save(qr_path)
            img.save(latest)

            print(f"  QR code saved     : {qr_path}")
            print(f"  (also saved as)   : {latest}")
        except Exception as e:
            print(f"  [QR] Could not generate QR code: {e}")

        print(f"{'='*60}\n")
    else:
        print(f"[start.py] Could not detect Cloudflare tunnel URL — check {cf_log_path}")

    # ── 4. Wait until either process exits ──────────────────────────────
    while True:
        if flask_proc.poll() is not None:
            print("[start.py] Flask exited. Stopping Cloudflare tunnel.")
            shutdown()
        if cf_proc.poll() is not None:
            print("[start.py] Cloudflare tunnel exited. Stopping Flask.")
            shutdown()
        time.sleep(1)


if __name__ == "__main__":
    main()
