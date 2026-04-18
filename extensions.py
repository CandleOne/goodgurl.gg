from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail
from flask_migrate import Migrate
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
