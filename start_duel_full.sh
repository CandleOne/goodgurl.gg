#!/bin/bash
# Script to start the Flask app with Cloudflare Tunnel for dueling system testing.

set -euo pipefail

# --- Configuration ---
APP_DIR="/workspaces/goodgurl.gg"
VENV_PATH="$APP_DIR/.venv/bin/activate"
APP_MODULE="app.py"
APP_PORT=5000
TUNNEL_URL="http://localhost:$APP_PORT"
MIGRATION_SCRIPT="migrations/versions/20260429_duel_enhancements.py" # Adjust if name changes

LOG_APP="/tmp/goodgurl.log"
LOG_TUNNEL="/tmp/cloudflared.log"
CLOUDFLARED_BIN="cloudflared" # Assuming cloudflared is in PATH

# --- Ensure environment is active ---
if [ -f "$VENV_PATH" ]; then
    source "$VENV_PATH"
else
    echo "Warning: Python virtual environment not found at $VENV_PATH. Running with system Python."
fi

cd "$APP_DIR"

# --- Run migrations ---
echo "Applying database migrations..."
# Note: 'flask db upgrade' assumes FLASK_APP is set and Alembic is configured.
# If you have a custom migration command, adjust this line.
export FLASK_APP="$APP_MODULE" # Explicitly set FLASK_APP for flask command
if ! flask db upgrade; then
    echo "Error: Database migration failed. Please check your Alembic setup and DB connection."
    exit 1
fi
echo "Database migrations applied."

# --- Start the Flask App ---
echo "Starting Flask app on port $APP_PORT..."
# Use nohup to ensure the process continues if the terminal closes.
# Redirect stdout and stderr to a log file.
nohup python3 "$APP_MODULE" > "$LOG_APP" 2>&1 &
APP_PID=$!
echo "Flask app started. PID: $APP_PID. Logging to $LOG_APP."

# Give the app a moment to start up
sleep 4

# Check if app is listening
if ! ss -tlnp | grep "$APP_PORT" > /dev/null; then
    echo "Error: Flask app failed to start or is not listening on port $APP_PORT."
    echo "Check $LOG_APP for details. Killing potentially orphaned processes."
    kill $APP_PID 2>/dev/null || true
    exit 1
fi
echo "Flask app is listening on port $APP_PORT."

# --- Start Cloudflare Tunnel ---
echo "Starting Cloudflare Tunnel for $TUNNEL_URL..."
if ! command -v "$CLOUDFLARED_BIN" &> /dev/null; then
    echo "Error: cloudflared command not found. Please install it or ensure it's in your PATH."
    echo "See: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
    echo "Killing Flask app (PID $APP_PID)."
    kill $APP_PID 2>/dev/null || true
    exit 1
fi

"$CLOUDFLARED_BIN" tunnel --url "$TUNNEL_URL" > "$LOG_TUNNEL" 2>&1 &
TUNNEL_PID=$!
echo "Cloudflare Tunnel started. PID: $TUNNEL_PID. Logging to $LOG_TUNNEL."

# --- Provide tunnel URL ---
echo "Waiting a few seconds for the tunnel to establish..."
sleep 5
TUNNEL_URL_DETECTED=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG_TUNNEL" | head -n 1)

if [ -z "$TUNNEL_URL_DETECTED" ]; then
    echo "Warning: Could not automatically detect Cloudflare Tunnel URL from logs."
    echo "Please check $LOG_TUNNEL and access your service via the dynamic URL."
else
    echo "✅ Tunnel is live at: $TUNNEL_URL_DETECTED"
    echo "   (This URL may change on restarts with the free tier)"

    # --- Generate QR code ---
    python3 - <<PYEOF
import sys, os
try:
    import qrcode
    from datetime import datetime
    url = "$TUNNEL_URL_DETECTED"
    qr_dir = os.path.join("$APP_DIR", "qrcodes")
    os.makedirs(qr_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    qr_path = os.path.join(qr_dir, f"cloudflare_{timestamp}.png")
    latest  = os.path.join(qr_dir, "latest.png")
    img = qrcode.make(url)
    img.save(qr_path)
    img.save(latest)
    print(f"  QR code saved  : {qr_path}")
    print(f"  (also saved as): {latest}")
except Exception as e:
    print(f"  [QR] Could not generate QR code: {e}", file=sys.stderr)
PYEOF
fi

echo "Services running in background. To stop:"
echo "  kill $APP_PID $TUNNEL_PID"
