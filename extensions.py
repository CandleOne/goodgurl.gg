from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail
from flask_migrate import Migrate
from flask_socketio import SocketIO
import os

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access that page."
login_manager.login_message_category = "info"
csrf = CSRFProtect()
limiter = Limiter(
    get_remote_address,
    default_limits=["200 per minute"],
    storage_uri=os.environ.get("LIMITER_STORAGE_URI", "memory://"),
)
mail = Mail()
migrate = Migrate()
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    # Force polling only — WebSocket upgrades fail through Cloudflare quick tunnels
    # because they're served over HTTP/2 which doesn't support standard WS upgrade
    transports=["polling"],
    ping_timeout=60,
    ping_interval=25,
)
