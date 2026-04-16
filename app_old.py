import io
import math
import os
import re
import random
import secrets
import threading
import time as _time
from datetime import datetime, timedelta
from functools import wraps

from faker import Faker as _Faker
_fake = _Faker()

from multiavatar.multiavatar import multiavatar as make_avatar
import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

from urllib.parse import urlparse

from markupsafe import Markup, escape
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, abort, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "goodgurl-dev-key-change-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///goodgurl.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max upload
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm"}
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["200 per minute"],
                  storage_uri="memory://")
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access that page."
login_manager.login_message_category = "info"


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403


@app.errorhandler(500)
def server_error(e):
    return render_template("errors/500.html"), 500


AVATAR_DIR = os.path.join(app.static_folder, "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)


def generate_avatar(seed):
    """Generate a Multiavatar SVG locally and save to static/avatars/. Returns URL path."""
    svg_code = make_avatar(seed, None, None)
    filename = f"{seed}.svg"
    filepath = os.path.join(AVATAR_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(svg_code)
    return f"/static/avatars/{filename}"

# ---------------------------------------------------------------------------
# Constants & Level-Up Configuration (inspired by cjmellor/level-up)
# ---------------------------------------------------------------------------

# --- Levelling ---
STARTING_LEVEL = 1
LEVEL_CAP = 100
LEVEL_CAP_POINTS_CONTINUE = True  # keep earning XP past cap

def xp_for_level(level):
    """XP needed to reach a given level. Gentle early curve, steep late game."""
    if level <= 1:
        return 0
    return int(15 * (level - 1) ** 1.7)

# Pre-compute lookup table
LEVEL_XP_TABLE = {lvl: xp_for_level(lvl) for lvl in range(1, LEVEL_CAP + 1)}

# --- Tiers (named status brackets based on level) ---
TIERS = [
    {"name": "Novice",      "level": 1,  "color": "#9ca3af", "icon": "game-icons:rose"},
    {"name": "Apprentice",  "level": 5,  "color": "#60a5fa", "icon": "game-icons:butterfly"},
    {"name": "Adept",       "level": 15, "color": "#a78bfa", "icon": "game-icons:crystal-shine"},
    {"name": "Expert",      "level": 30, "color": "#f472b6", "icon": "game-icons:fire-gem"},
    {"name": "Elite",       "level": 50, "color": "#fbbf24", "icon": "game-icons:diamond-trophy"},
    {"name": "Legend",       "level": 75, "color": "#f43f5e", "icon": "game-icons:queen-crown"},
]

# --- Multiplier stacking strategies ---
MULTIPLIER_STACK_STRATEGY = "compound"  # compound | additive | highest

# --- Rewards ---
TASK_REWARD_XP = 25
TASK_REWARD_COINS = 10
POST_REWARD_XP = 10
LEADERBOARD_REWARD_INTERVAL_DAYS = 7
LEADERBOARD_TOP_REWARD_COINS = 500

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# Many-to-many: users liking posts
likes_table = db.Table(
    "likes",
    db.Column("user_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("post_id", db.Integer, db.ForeignKey("post.id"), primary_key=True),
)

# Tags on posts
post_tags = db.Table(
    "post_tags",
    db.Column("post_id", db.Integer, db.ForeignKey("post.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)

# Tags on forum threads
thread_tags = db.Table(
    "thread_tags",
    db.Column("thread_id", db.Integer, db.ForeignKey("forum_thread.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False)  # 'sissy' or 'master'
    is_bot = db.Column(db.Boolean, default=False)
    bio = db.Column(db.Text, default="")
    avatar_url = db.Column(db.String(256), default="")
    coins = db.Column(db.Integer, default=0)
    funded_coins = db.Column(db.Integer, default=0)  # spendable balance from investments
    flair_color = db.Column(db.String(7), default="")  # hex color for username flair
    xp = db.Column(db.Integer, default=0)
    level = db.Column(db.Integer, default=STARTING_LEVEL)
    show_wealth = db.Column(db.Boolean, default=False)
    listed_on_market = db.Column(db.Boolean, default=False)  # sissy must opt-in to market listing
    market_value_boost = db.Column(db.Integer, default=0)  # admin-set offset added to computed market value
    profile_frame = db.Column(db.String(60), default="")  # CSS class for profile frame cosmetic
    profile_title = db.Column(db.String(60), default="")  # custom title shown on profile
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    posts = db.relationship("Post", backref="author", lazy="dynamic", cascade="all, delete-orphan")
    tasks_created = db.relationship("Task", backref="creator", lazy="dynamic", foreign_keys="Task.creator_id", cascade="all, delete-orphan")
    tasks_assigned = db.relationship("Task", backref="assignee", lazy="dynamic", foreign_keys="Task.assignee_id")
    investments = db.relationship("Investment", backref="investor", lazy="dynamic", foreign_keys="Investment.investor_id", cascade="all, delete-orphan")
    liked_posts = db.relationship("Post", secondary=likes_table, backref=db.backref("liked_by", lazy="dynamic"), lazy="dynamic")
    achievements = db.relationship("UserAchievement", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    streaks = db.relationship("Streak", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    xp_audit = db.relationship("XPAudit", backref="user", lazy="dynamic", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    # ---- Level-Up: XP & Levelling ----

    def _get_active_multiplier(self):
        """Calculate combined multiplier from all active Multiplier records."""
        now = datetime.utcnow()
        active = Multiplier.query.filter(
            Multiplier.is_active == True,
            db.or_(Multiplier.starts_at == None, Multiplier.starts_at <= now),
            db.or_(Multiplier.expires_at == None, Multiplier.expires_at >= now),
        ).all()
        if not active:
            return 1.0
        values = [m.multiplier for m in active]
        if MULTIPLIER_STACK_STRATEGY == "compound":
            result = 1.0
            for v in values:
                result *= v
            return result
        elif MULTIPLIER_STACK_STRATEGY == "additive":
            return sum(values)
        elif MULTIPLIER_STACK_STRATEGY == "highest":
            return max(values)
        return 1.0

    def add_points(self, amount, reason="", multiplier=None):
        """Add XP points with multiplier support. Returns (final_xp, levelled_up)."""
        if self.role != "sissy":
            return 0, False

        # Calculate multiplier
        db_mult = self._get_active_multiplier()
        inline_mult = multiplier or 1.0
        if MULTIPLIER_STACK_STRATEGY == "compound":
            total_mult = db_mult * inline_mult
        elif MULTIPLIER_STACK_STRATEGY == "additive":
            total_mult = db_mult + inline_mult - 1.0
        else:
            total_mult = max(db_mult, inline_mult)

        final_amount = int(amount * total_mult)

        # Level cap check
        at_cap = self.level >= LEVEL_CAP
        if at_cap and not LEVEL_CAP_POINTS_CONTINUE:
            final_amount = 0

        old_level = self.level
        self.xp += final_amount

        # Audit log
        db.session.add(XPAudit(
            user_id=self.id,
            amount=final_amount,
            original_amount=amount,
            multiplier_applied=round(total_mult, 2),
            reason=reason,
            audit_type="add",
        ))

        # Check for level-up
        levelled_up = False
        if not at_cap:
            new_level = self._calculate_level()
            if new_level > old_level:
                self.level = min(new_level, LEVEL_CAP)
                levelled_up = True

        return final_amount, levelled_up

    def deduct_points(self, amount, reason=""):
        """Deduct XP from user."""
        self.xp = max(0, self.xp - amount)
        db.session.add(XPAudit(
            user_id=self.id,
            amount=-amount,
            original_amount=amount,
            multiplier_applied=1.0,
            reason=reason,
            audit_type="deduct",
        ))
        # Recalculate level downward
        self.level = max(STARTING_LEVEL, self._calculate_level())

    def _calculate_level(self):
        """Determine level from current XP."""
        lvl = STARTING_LEVEL
        for l in range(2, LEVEL_CAP + 1):
            if self.xp >= LEVEL_XP_TABLE[l]:
                lvl = l
            else:
                break
        return lvl

    def get_points(self):
        return self.xp

    def get_level(self):
        return self.level

    def next_level_at(self):
        """XP remaining until next level."""
        if self.level >= LEVEL_CAP:
            return 0
        return max(0, LEVEL_XP_TABLE[self.level + 1] - self.xp)

    @property
    def level_progress_pct(self):
        """Percentage progress through current level bracket."""
        if self.level >= LEVEL_CAP:
            return 100
        current_threshold = LEVEL_XP_TABLE.get(self.level, 0)
        next_threshold = LEVEL_XP_TABLE.get(self.level + 1, current_threshold + 1)
        span = next_threshold - current_threshold
        if span <= 0:
            return 100
        progress = self.xp - current_threshold
        return min(100, int((progress / span) * 100))

    # ---- Level-Up: Tiers ----

    @property
    def tier(self):
        """Current tier dict based on level."""
        current = TIERS[0]
        for t in TIERS:
            if self.level >= t["level"]:
                current = t
        return current

    @property
    def rank(self):
        """Tier name (backward compat)."""
        return self.tier["name"]

    def get_next_tier(self):
        idx = 0
        for i, t in enumerate(TIERS):
            if self.level >= t["level"]:
                idx = i
        if idx + 1 < len(TIERS):
            return TIERS[idx + 1]
        return None

    def next_tier_at(self):
        """Levels remaining until next tier."""
        nt = self.get_next_tier()
        if nt is None:
            return 0
        return max(0, nt["level"] - self.level)

    def tier_progress(self):
        """Percentage through current tier bracket toward next."""
        nt = self.get_next_tier()
        if nt is None:
            return 100
        current_tier_lvl = self.tier["level"]
        span = nt["level"] - current_tier_lvl
        if span <= 0:
            return 100
        return min(100, int(((self.level - current_tier_lvl) / span) * 100))

    # ---- Level-Up: Achievements ----

    def grant_achievement(self, achievement, progress=100):
        ua = UserAchievement.query.filter_by(user_id=self.id, achievement_id=achievement.id).first()
        if ua:
            ua.progress = min(100, progress)
        else:
            ua = UserAchievement(user_id=self.id, achievement_id=achievement.id, progress=min(100, progress))
            db.session.add(ua)
        return ua

    def increment_achievement_progress(self, achievement, amount=10):
        ua = UserAchievement.query.filter_by(user_id=self.id, achievement_id=achievement.id).first()
        if ua:
            ua.progress = min(100, ua.progress + amount)
        else:
            ua = UserAchievement(user_id=self.id, achievement_id=achievement.id, progress=min(100, amount))
            db.session.add(ua)
        return ua

    def has_achievement(self, achievement):
        ua = UserAchievement.query.filter_by(user_id=self.id, achievement_id=achievement.id).first()
        return ua is not None and ua.progress >= 100

    def get_user_achievements(self):
        return UserAchievement.query.filter_by(user_id=self.id).all()

    # ---- Level-Up: Streaks ----

    def record_streak(self, activity_name):
        """Record a streak for today. Returns (streak, increased)."""
        today = datetime.utcnow().date()
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        if streak:
            if streak.last_activity_date == today:
                return streak, False  # already recorded today
            yesterday = today - timedelta(days=1)
            if streak.frozen_until and streak.frozen_until >= today:
                # Frozen – don't break, just update
                streak.last_activity_date = today
                return streak, False
            if streak.last_activity_date == yesterday:
                streak.count += 1
                streak.last_activity_date = today
                return streak, True
            else:
                # Streak broken – archive and reset
                if streak.count > 1:
                    db.session.add(StreakHistory(
                        user_id=self.id, activity_name=activity_name,
                        count=streak.count, started_at=streak.started_at,
                        ended_at=datetime.utcnow(),
                    ))
                streak.count = 1
                streak.started_at = datetime.utcnow()
                streak.last_activity_date = today
                return streak, True
        else:
            streak = Streak(
                user_id=self.id, activity_name=activity_name,
                count=1, last_activity_date=today,
            )
            db.session.add(streak)
            return streak, True

    def get_current_streak_count(self, activity_name):
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        return streak.count if streak else 0

    def has_streak_today(self, activity_name):
        today = datetime.utcnow().date()
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        return streak is not None and streak.last_activity_date == today

    def freeze_streak(self, activity_name, days=1):
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        if streak:
            streak.frozen_until = (datetime.utcnow() + timedelta(days=days)).date()

    def unfreeze_streak(self, activity_name):
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        if streak:
            streak.frozen_until = None

    # ---- Market value ----

    @property
    def market_value(self):
        """Robust market value combining base stats, content quality,
        work ethic, social proof, consistency, demand, and recency."""
        post_count = self.posts.count()
        completed = Task.query.filter_by(assignee_id=self.id, status="completed").count()

        # Total likes & comments received across all posts
        total_likes = 0
        total_comments = 0
        for p in self.posts.all():
            total_likes += p.liked_by.count()
            total_comments += p.comments.count()

        # Engagement rate (avg likes per post, capped at 20)
        engagement_rate = min((total_likes / post_count) if post_count > 0 else 0, 20)

        # Investor demand
        investor_count = Investment.query.filter_by(sissy_id=self.id).count()
        total_invested = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=self.id).scalar() or 0

        # Achievement count
        achievement_count = UserAchievement.query.filter(
            UserAchievement.user_id == self.id,
            UserAchievement.progress >= 100
        ).count()

        # Streak bonus
        login_streak = self.get_current_streak_count("login")
        post_streak = self.get_current_streak_count("post")
        streak_bonus = min(login_streak, 30) * 2 + min(post_streak, 14) * 3

        # Recency: days since last post (1.0 if recent, decays to 0.7)
        latest_post = self.posts.order_by(Post.created_at.desc()).first()
        if latest_post:
            days_since = (datetime.utcnow() - latest_post.created_at).days
            recency = max(0.7, 1.0 - (days_since * 0.01))  # loses 1% per day, floor 0.7
        else:
            recency = 0.7

        # --- Component scores ---
        base = self.xp + (self.level - 1) * 10
        content = post_count * 5 + total_likes * 2 + total_comments * 3
        engagement = int(engagement_rate * 20)
        work = completed * 15
        social = self.followers.count() * 8 + investor_count * 12
        consistency = streak_bonus + achievement_count * 5
        demand = int(math.log2(1 + total_invested) * 3)

        raw = base + content + engagement + work + social + consistency + demand
        return max(0, int(raw * recency) + int(self.market_value_boost or 0))


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    media_url = db.Column(db.String(512), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    is_boosted = db.Column(db.Boolean, default=False)  # boosted posts appear first
    tags = db.relationship("Tag", secondary=post_tags, backref="posts", lazy="dynamic")

    @property
    def like_count(self):
        return self.liked_by.count() if hasattr(self, '_liked_by') or True else 0


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    assignee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    reward_coins = db.Column(db.Integer, default=TASK_REWARD_COINS)
    reward_xp = db.Column(db.Integer, default=TASK_REWARD_XP)
    status = db.Column(db.String(16), default="open")  # open, accepted, completed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class Investment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    investor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sissy_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    bought_value = db.Column(db.Integer, nullable=False)  # sissy market_value at purchase
    total_dividends = db.Column(db.Integer, default=0)  # lifetime dividends earned
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sissy = db.relationship("User", foreign_keys=[sissy_id])


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Activity(db.Model):
    """Unified activity log for every user action on the platform."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    action = db.Column(db.String(32), nullable=False)     # like, comment, post, follow, invest, gift, forum_thread, forum_reply, task_create, task_accept, task_complete, buy_coins, unfollow
    icon = db.Column(db.String(48), default="bi-activity")
    description = db.Column(db.Text, nullable=False)       # HTML-safe with <a> links
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User", backref=db.backref("activities", lazy="dynamic",
                           order_by="Activity.created_at.desc()"))


class PendingGift(db.Model):
    """Gifts that must be manually claimed by the recipient."""
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    is_claimed = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    claimed_at = db.Column(db.DateTime, nullable=True)

    sender = db.relationship("User", foreign_keys=[sender_id], backref="sent_gifts")
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref="received_gifts")


# ---------------------------------------------------------------------------
# Level-Up Models (inspired by cjmellor/level-up)
# ---------------------------------------------------------------------------

class Multiplier(db.Model):
    """XP multipliers – can be time-based or toggled manually."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    multiplier = db.Column(db.Float, nullable=False, default=2.0)
    is_active = db.Column(db.Boolean, default=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Achievement(db.Model):
    """Achievements that can be earned by sissies."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.Text, default="")
    icon = db.Column(db.String(64), default="bi-award")
    is_secret = db.Column(db.Boolean, default=False)
    xp_reward = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UserAchievement(db.Model):
    """Tracks which users have which achievements and progress."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    achievement_id = db.Column(db.Integer, db.ForeignKey("achievement.id"), nullable=False)
    progress = db.Column(db.Integer, default=0)  # 0-100%
    earned_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    achievement = db.relationship("Achievement")

    @staticmethod
    def on_progress_update(mapper, connection, target):
        if target.progress >= 100 and target.earned_at is None:
            target.earned_at = datetime.utcnow()

db.event.listen(UserAchievement, "before_update", UserAchievement.on_progress_update)
db.event.listen(UserAchievement, "before_insert", UserAchievement.on_progress_update)


class Streak(db.Model):
    """Tracks consecutive daily activity streaks."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    activity_name = db.Column(db.String(64), nullable=False)  # e.g. 'login', 'post', 'task'
    count = db.Column(db.Integer, default=0)
    last_activity_date = db.Column(db.Date, nullable=True)
    frozen_until = db.Column(db.Date, nullable=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "activity_name"),)


class StreakHistory(db.Model):
    """Archive of broken streaks."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    activity_name = db.Column(db.String(64), nullable=False)
    count = db.Column(db.Integer, nullable=False)
    started_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)


class XPAudit(db.Model):
    """Audit trail for all XP changes."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)  # final (after multiplier)
    original_amount = db.Column(db.Integer, nullable=False)
    multiplier_applied = db.Column(db.Float, default=1.0)
    reason = db.Column(db.String(256), default="")
    audit_type = db.Column(db.String(16), default="add")  # add, deduct, set
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MarketSnapshot(db.Model):
    """Per-event snapshot of a sissy's market metrics for charting."""
    id = db.Column(db.Integer, primary_key=True)
    sissy_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    market_value = db.Column(db.Integer, nullable=False)
    xp = db.Column(db.Integer, nullable=False)
    post_count = db.Column(db.Integer, nullable=False)
    task_count = db.Column(db.Integer, nullable=False)
    investor_count = db.Column(db.Integer, nullable=False)
    follower_count = db.Column(db.Integer, nullable=False, default=0)

    sissy = db.relationship("User", foreign_keys=[sissy_id])


# ---------------------------------------------------------------------------
# New Feature Models
# ---------------------------------------------------------------------------

# Many-to-many: followers
followers_table = db.Table(
    "followers",
    db.Column("follower_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("followed_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)


class Notification(db.Model):
    """In-app notifications."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    type = db.Column(db.String(32), nullable=False)  # like, comment, follow, task, invest, level_up, achievement, gift, streak_reward
    message = db.Column(db.String(512), nullable=False)
    link = db.Column(db.String(256), default="")
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User", backref=db.backref("notifications", lazy="dynamic"))


class Comment(db.Model):
    """Comments on posts."""
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User", backref=db.backref("comments", lazy="dynamic"))
    post = db.relationship("Post", backref=db.backref("comments", lazy="dynamic"))


class Message(db.Model):
    """Direct messages between users."""
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    sender = db.relationship("User", foreign_keys=[sender_id], backref=db.backref("messages_sent", lazy="dynamic"))
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref=db.backref("messages_received", lazy="dynamic"))


class DailyReward(db.Model):
    """Tracks daily login rewards already claimed."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    coins_awarded = db.Column(db.Integer, default=0)
    xp_awarded = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint("user_id", "date"),)


# ---------------------------------------------------------------------------
# Forum Models (Storyden-inspired)
# ---------------------------------------------------------------------------
class ForumCategory(db.Model):
    """Top-level forum sections."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.Text, default="")
    icon = db.Column(db.String(64), default="bi-chat-dots")
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    threads = db.relationship("ForumThread", backref="category", lazy="dynamic",
                              order_by="ForumThread.is_pinned.desc(), ForumThread.last_reply_at.desc()")


class ForumThread(db.Model):
    """A discussion thread inside a category."""
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("forum_category.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(256), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False)
    is_locked = db.Column(db.Boolean, default=False)
    view_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_reply_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    author = db.relationship("User", backref=db.backref("forum_threads", lazy="dynamic"))
    replies = db.relationship("ForumReply", backref="thread", lazy="dynamic",
                              order_by="ForumReply.created_at.asc()")
    tags = db.relationship("Tag", secondary=thread_tags, backref="threads", lazy="dynamic")

    @property
    def reply_count(self):
        return self.replies.count()

    @property
    def last_reply(self):
        return self.replies.order_by(ForumReply.created_at.desc()).first()


class ForumReply(db.Model):
    """A reply inside a thread."""
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("forum_thread.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    author = db.relationship("User", backref=db.backref("forum_replies", lazy="dynamic"))


class Report(db.Model):
    """Content reports submitted by users for moderator review."""
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("forum_thread.id"), nullable=True)
    reason = db.Column(db.String(256), nullable=False)
    status = db.Column(db.String(16), default="open")  # open, reviewed, dismissed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reporter = db.relationship("User", backref="reports_filed")
    post = db.relationship("Post", backref="reports")
    thread = db.relationship("ForumThread", backref="reports")


blocked_users = db.Table(
    "blocked_users",
    db.Column("blocker_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("blocked_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)

User.blocked = db.relationship(
    "User", secondary=blocked_users,
    primaryjoin=(blocked_users.c.blocker_id == User.id),
    secondaryjoin=(blocked_users.c.blocked_id == User.id),
    backref=db.backref("blocked_by", lazy="dynamic"), lazy="dynamic"
)


class DailyChallenge(db.Model):
    """A rotating daily challenge available to sissies."""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    goal_type = db.Column(db.String(40), nullable=False)   # posts, tasks, likes_received, comments, logins
    goal_count = db.Column(db.Integer, nullable=False, default=1)
    reward_coins = db.Column(db.Integer, nullable=False, default=5)
    reward_xp = db.Column(db.Integer, nullable=False, default=10)


class UserDailyChallenge(db.Model):
    """Tracks a user's progress on a daily challenge."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("daily_challenge.id"), nullable=False)
    progress = db.Column(db.Integer, default=0)
    completed = db.Column(db.Boolean, default=False)
    claimed = db.Column(db.Boolean, default=False)

    user = db.relationship("User", backref="daily_challenges")
    challenge = db.relationship("DailyChallenge", backref="participants")
    __table_args__ = (db.UniqueConstraint("user_id", "challenge_id"),)


class ShopItem(db.Model):
    """An item available for purchase in the cosmetic shop."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(300), default="")
    category = db.Column(db.String(40), nullable=False)  # frame, title, flair
    css_class = db.Column(db.String(60), default="")     # CSS class or value applied
    price = db.Column(db.Integer, nullable=False, default=50)
    level_required = db.Column(db.Integer, default=1)
    icon = db.Column(db.String(60), default="bi-bag")


class UserPurchase(db.Model):
    """Tracks items a user has purchased."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("shop_item.id"), nullable=False)
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="purchases")
    item = db.relationship("ShopItem", backref="buyers")
    __table_args__ = (db.UniqueConstraint("user_id", "item_id"),)


# ---------------------------------------------------------------------------
# Daily challenge generation & progress helpers
# ---------------------------------------------------------------------------
_CHALLENGE_TEMPLATES = [
    ("Post Power", "Create {n} post(s) today", "posts", [1, 2, 3]),
    ("Task Grinder", "Complete {n} task(s) today", "tasks", [1, 2]),
    ("Social Butterfly", "Receive {n} like(s) on your posts today", "likes_received", [3, 5, 10]),
    ("Commentator", "Leave {n} comment(s) today", "comments", [2, 3, 5]),
    ("Forum Contributor", "Post {n} forum reply(ies) today", "forum_replies", [1, 2, 3]),
]


def generate_daily_challenges(date=None):
    """Generate 3 random challenges for the given date (idempotent)."""
    date = date or datetime.utcnow().date()
    existing = DailyChallenge.query.filter_by(date=date).count()
    if existing >= 3:
        return
    rng = random.Random(str(date))
    picks = rng.sample(_CHALLENGE_TEMPLATES, min(3, len(_CHALLENGE_TEMPLATES)))
    for title, desc_tpl, goal_type, counts in picks:
        n = rng.choice(counts)
        ch = DailyChallenge(
            date=date, title=title,
            description=desc_tpl.format(n=n),
            goal_type=goal_type, goal_count=n,
            reward_coins=n * 3, reward_xp=n * 5,
        )
        db.session.add(ch)
    db.session.commit()


def bump_daily_challenge(user, goal_type):
    """Increment progress for the user on today's challenge matching goal_type."""
    today = datetime.utcnow().date()
    challenges = DailyChallenge.query.filter_by(date=today, goal_type=goal_type).all()
    for ch in challenges:
        uc = UserDailyChallenge.query.filter_by(user_id=user.id, challenge_id=ch.id).first()
        if not uc:
            uc = UserDailyChallenge(user_id=user.id, challenge_id=ch.id, progress=0)
            db.session.add(uc)
        if uc.completed:
            continue
        uc.progress += 1
        if uc.progress >= ch.goal_count:
            uc.completed = True


# Add follow relationships to User
User.followed = db.relationship(
    "User", secondary=followers_table,
    primaryjoin=(followers_table.c.follower_id == User.id),
    secondaryjoin=(followers_table.c.followed_id == User.id),
    backref=db.backref("followers", lazy="dynamic"), lazy="dynamic"
)


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------
def notify(user_id, type, message, link=""):
    """Create a notification for a user."""
    n = Notification(user_id=user_id, type=type, message=message, link=link)
    db.session.add(n)
    return n


# Activity icon map
_ACTIVITY_ICONS = {
    "like": "bi-heart-fill text-danger",
    "comment": "bi-chat-dots-fill text-info",
    "post": "bi-pencil-square text-purple",
    "follow": "bi-person-plus-fill text-success",
    "unfollow": "bi-person-dash-fill text-secondary",
    "invest": "bi-graph-up-arrow text-warning",
    "gift": "bi-gift-fill text-pink",
    "forum_thread": "bi-chat-square-text-fill text-info",
    "forum_reply": "bi-reply-fill text-info",
    "task_create": "bi-clipboard-plus text-warning",
    "task_accept": "bi-clipboard-check text-info",
    "task_complete": "bi-clipboard2-check-fill text-success",
    "buy_coins": "bi-coin text-warning",
}


def log_activity(user_id, action, description):
    """Record a user activity event with auto-assigned icon."""
    icon = _ACTIVITY_ICONS.get(action, "bi-activity")
    a = Activity(user_id=user_id, action=action, icon=icon, description=description)
    db.session.add(a)
    return a


def record_market_snapshot(sissy, timestamp=None):
    """Insert a new MarketSnapshot for a sissy (one per event)."""
    investor_count = Investment.query.filter_by(sissy_id=sissy.id).count()
    post_count = sissy.posts.count()
    task_count = Task.query.filter_by(assignee_id=sissy.id, status="completed").count()
    follower_count = sissy.followers.count()
    snap = MarketSnapshot(
        sissy_id=sissy.id, timestamp=timestamp or datetime.utcnow(),
        market_value=sissy.market_value, xp=sissy.xp,
        post_count=post_count, task_count=task_count,
        investor_count=investor_count, follower_count=follower_count,
    )
    db.session.add(snap)


def distribute_dividends(sissy, coins_earned):
    """Distribute 10% of a sissy's coin earnings to their investors,
    proportional to each investor's share. Top investor (sponsor) gets +50% bonus."""
    if coins_earned <= 0:
        return
    dividend_pool = max(1, int(coins_earned * 0.10))  # 10% of earnings
    investments = Investment.query.filter_by(sissy_id=sissy.id).all()
    if not investments:
        return
    total_invested = sum(inv.amount for inv in investments)
    if total_invested <= 0:
        return
    # Find sponsor (top investor)
    sponsor_inv = max(investments, key=lambda i: i.amount)
    for inv in investments:
        share = inv.amount / total_invested
        payout = max(1, int(dividend_pool * share))
        # Sponsor gets 50% bonus
        if inv.id == sponsor_inv.id and len(investments) > 1:
            payout = int(payout * 1.5)
        investor = User.query.get(inv.investor_id)
        if investor:
            investor.coins += payout
            inv.total_dividends += payout
            db.session.add(Transaction(
                user_id=investor.id, amount=payout,
                description=f"Dividend from {sissy.username} ({coins_earned} earned)"
            ))


def get_sponsor(sissy):
    """Return the User who has the highest total investment in this sissy, or None."""
    inv = Investment.query.filter_by(sissy_id=sissy.id).order_by(Investment.amount.desc()).first()
    if inv:
        return User.query.get(inv.investor_id)
    return None


# ---------------------------------------------------------------------------
# Achievement checker — call after actions that might trigger an achievement
# ---------------------------------------------------------------------------
_ACHIEVEMENT_RULES = None  # lazy-loaded after DB init


def _get_achievement_rules():
    """Build achievement rules map from DB. Cached after first call."""
    global _ACHIEVEMENT_RULES
    if _ACHIEVEMENT_RULES is not None:
        return _ACHIEVEMENT_RULES
    _ACHIEVEMENT_RULES = {}
    for ach in Achievement.query.all():
        _ACHIEVEMENT_RULES[ach.name] = ach
    return _ACHIEVEMENT_RULES


def check_achievements(user):
    """Check and award any newly-earned achievements for a user."""
    if user.role != "sissy":
        return
    rules = _get_achievement_rules()
    if not rules:
        return

    post_count = user.posts.count()
    task_count = Task.query.filter_by(assignee_id=user.id, status="completed").count()
    total_likes = sum(p.liked_by.count() for p in user.posts)
    streak = user.get_current_streak_count("login")

    checks = [
        ("First Post", post_count >= 1, min(post_count * 100, 100)),
        ("5 Posts", post_count >= 5, min(post_count * 20, 100)),
        ("25 Posts", post_count >= 25, min(post_count * 4, 100)),
        ("First Task", task_count >= 1, min(task_count * 100, 100)),
        ("Task Master", task_count >= 10, min(task_count * 10, 100)),
        ("Level 5", user.level >= 5, min(user.level * 20, 100)),
        ("Level 10", user.level >= 10, min(user.level * 10, 100)),
        ("Level 25", user.level >= 25, min(user.level * 4, 100)),
        ("Level 50", user.level >= 50, min(user.level * 2, 100)),
        ("3-Day Streak", streak >= 3, min(int(streak / 3 * 100), 100)),
        ("7-Day Streak", streak >= 7, min(int(streak / 7 * 100), 100)),
        ("30-Day Streak", streak >= 30, min(int(streak / 30 * 100), 100)),
        ("Popular", total_likes >= 10, min(total_likes * 10, 100)),
    ]

    for name, earned, progress in checks:
        ach = rules.get(name)
        if not ach:
            continue
        if user.has_achievement(ach):
            continue
        if earned:
            user.grant_achievement(ach, 100)
            if ach.xp_reward:
                user.add_points(ach.xp_reward, reason=f"Achievement: {name}")
            notify(user.id, "achievement",
                   f"Achievement unlocked: {name}!",
                   "/account")
        else:
            user.increment_achievement_progress(ach, max(0, progress))


# ---------------------------------------------------------------------------
# Streak & Daily Reward Constants
# ---------------------------------------------------------------------------
STREAK_BONUS_TABLE = {
    3: {"coins": 5, "xp": 10},
    7: {"coins": 15, "xp": 30},
    14: {"coins": 30, "xp": 60},
    30: {"coins": 75, "xp": 150},
    60: {"coins": 150, "xp": 300},
    100: {"coins": 300, "xp": 600},
}
DAILY_LOGIN_COINS = 2
DAILY_LOGIN_XP = 5


# ---------------------------------------------------------------------------
# Login manager
# ---------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Utility decorators
# ---------------------------------------------------------------------------
def role_required(role):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role != role:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "sissy")

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))

        if role not in ("sissy", "master", "admin"):
            flash("Invalid role.", "danger")
            return redirect(url_for("register"))

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already taken.", "danger")
            return redirect(url_for("register"))

        user = User(username=username, email=email, role=role,
                    avatar_url=generate_avatar(username))
        user.set_password(password)
        if role in ("master", "admin"):
            user.coins = 100  # starter coins for masters/admins
        db.session.add(user)
        db.session.flush()
        # Record login streak
        if role == "sissy":
            user.record_streak("login")
        db.session.commit()
        login_user(user)
        flash("Welcome to goodgurl.gg!", "success")
        return redirect(url_for("feed"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            # Record login streak for sissies
            if user.role == "sissy":
                streak, increased = user.record_streak("login")
                if increased and streak.count > 1:
                    flash(f"Login streak: {streak.count} days!", "info")

                # Daily login reward
                today = datetime.utcnow().date()
                already_claimed = DailyReward.query.filter_by(user_id=user.id, date=today).first()
                if not already_claimed:
                    coins_reward = DAILY_LOGIN_COINS
                    xp_reward = DAILY_LOGIN_XP

                    # Streak milestone bonus
                    if increased and streak.count in STREAK_BONUS_TABLE:
                        bonus = STREAK_BONUS_TABLE[streak.count]
                        coins_reward += bonus["coins"]
                        xp_reward += bonus["xp"]
                        notify(user.id, "streak_reward",
                               f"Streak milestone! {streak.count}-day streak bonus: +{bonus['coins']} coins, +{bonus['xp']} XP!")
                        flash(f"Streak milestone bonus! +{bonus['coins']} coins, +{bonus['xp']} XP!", "warning")

                    user.coins += coins_reward
                    user.add_points(xp_reward, reason="Daily login reward")
                    db.session.add(DailyReward(user_id=user.id, date=today, coins_awarded=coins_reward, xp_awarded=xp_reward))
                    db.session.add(Transaction(user_id=user.id, amount=coins_reward, description="Daily login reward"))
                    flash(f"Daily reward: +{coins_reward} coins, +{xp_reward} XP!", "success")

                check_achievements(user)
                generate_daily_challenges()
                db.session.commit()
            return redirect(request.args.get("next") or url_for("feed"))
        flash("Invalid credentials.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Reels / Vertical scroll feed
# ---------------------------------------------------------------------------
@app.route("/reels")
def reels():
    return render_template("reels.html")


@app.route("/api/reels")
def api_reels():
    """Return a page of posts as JSON for infinite scroll."""
    page = request.args.get("page", 1, type=int)
    per_page = 10
    tab = request.args.get("tab", "all")

    if current_user.is_authenticated and tab == "following" and current_user.followed.count() > 0:
        followed_ids = [u.id for u in current_user.followed.all()]
        q = Post.query.filter(Post.author_id.in_(followed_ids))
    else:
        q = Post.query

    posts = q.order_by(Post.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    data = []
    for p in posts:
        media_type = "image"
        if p.media_url:
            if p.media_url.endswith((".mp4", ".webm")):
                media_type = "video"
            elif p.media_url.endswith(".gif"):
                media_type = "gif"
        data.append({
            "id": p.id,
            "title": p.title,
            "body": p.body[:200],
            "media_url": p.media_url or "",
            "media_type": media_type,
            "author": p.author.username,
            "author_avatar": p.author.avatar_url or url_for("static", filename="default_avatar.png"),
            "author_rank": p.author.rank,
            "author_is_bot": p.author.is_bot,
            "likes": p.liked_by.count(),
            "comments": p.comments.count(),
            "tags": [t.name for t in p.tags.all()[:5]],
            "date": p.created_at.strftime("%b %d"),
            "url": url_for("view_post", post_id=p.id),
            "like_url": url_for("like_post", post_id=p.id),
            "author_url": url_for("profile", username=p.author.username),
        })
    return jsonify({"posts": data, "has_more": len(posts) == per_page})


# ---------------------------------------------------------------------------
# Feed / Front page
# ---------------------------------------------------------------------------
@app.route("/")
def feed():
    page = request.args.get("page", 1, type=int)
    tab = request.args.get("tab", "all")  # all | following

    if current_user.is_authenticated and tab == "following":
        followed_ids = [u.id for u in current_user.followed.all()]
        if followed_ids:
            all_posts = Post.query.filter(Post.author_id.in_(followed_ids)).order_by(Post.created_at.desc()).limit(30).all()
        else:
            all_posts = []
    elif current_user.is_authenticated:
        # Curated feed: posts by people the user has liked before float up, plus recent
        liked_post_ids = db.session.query(likes_table.c.post_id).filter(
            likes_table.c.user_id == current_user.id
        ).subquery()
        liked_tag_ids = db.session.query(post_tags.c.tag_id).filter(
            post_tags.c.post_id.in_(db.session.query(liked_post_ids))
        ).subquery()

        curated = Post.query.filter(
            Post.tags.any(Tag.id.in_(db.session.query(liked_tag_ids)))
        ).order_by(Post.created_at.desc())

        recent = Post.query.order_by(Post.created_at.desc())

        curated_ids = {p.id for p in curated.limit(20).all()}
        all_posts = list(curated.limit(20).all())
        for p in recent.limit(40).all():
            if p.id not in curated_ids:
                all_posts.append(p)
        all_posts = all_posts[:30]
    else:
        # Guest: just show recent posts
        all_posts = Post.query.order_by(Post.created_at.desc()).limit(30).all()

    # Sort: boosted posts first, then by date
    all_posts.sort(key=lambda p: (p.is_boosted, p.created_at), reverse=True)

    return render_template("feed.html", posts=all_posts, tab=tab)


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Hashtag helpers
# ---------------------------------------------------------------------------
_HASHTAG_RE = re.compile(r"#(\w{1,64})", re.UNICODE)


def extract_hashtags(text):
    """Return a list of unique lowercase hashtag names found in *text*."""
    return list(dict.fromkeys(m.lower() for m in _HASHTAG_RE.findall(text)))


def apply_hashtags(text, get_or_create_tag_fn):
    """Extract hashtags from text and return Tag objects."""
    tags = []
    for name in extract_hashtags(text):
        tag = Tag.query.filter_by(name=name).first()
        if not tag:
            tag = get_or_create_tag_fn(name)
        tags.append(tag)
    return tags


@app.template_filter("hashtagify")
def hashtagify_filter(text):
    """Convert #hashtag in text to clickable links. Auto-escapes HTML."""
    safe_text = str(escape(text))
    def _repl(m):
        tag_name = m.group(1).lower()
        return f'<a href="/hashtag/{tag_name}" class="text-pink text-decoration-none fw-bold">#{m.group(1)}</a>'
    return Markup(_HASHTAG_RE.sub(_repl, safe_text))


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/post/new", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per minute")
@role_required("sissy")
def new_post():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        tag_names = [t.strip().lower() for t in request.form.get("tags", "").split(",") if t.strip()]
        # Also extract #hashtags from body text
        tag_names = list(dict.fromkeys(tag_names + extract_hashtags(body)))

        if not title or not body:
            flash("Title and body are required.", "danger")
            return redirect(url_for("new_post"))

        # Media is required for feed posts (forum is for text-only)
        media_url = ""
        file = request.files.get("media_file")
        if file and file.filename and allowed_file(file.filename):
            # Generate a unique filename to prevent overwrites/collisions
            ext = file.filename.rsplit(".", 1)[1].lower()
            safe_name = secrets.token_hex(16) + "." + ext
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], safe_name))
            media_url = url_for("uploaded_file", filename=safe_name)
        elif file and file.filename:
            flash("File type not allowed. Accepted: png, jpg, jpeg, gif, webp, mp4, webm.", "warning")
            return redirect(url_for("new_post"))

        # Fall back to pasted URL if no file uploaded
        if not media_url:
            media_url = request.form.get("media_url", "").strip()
            if media_url:
                parsed = urlparse(media_url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    flash("Invalid media URL. Only http/https URLs are allowed.", "danger")
                    return redirect(url_for("new_post"))

        if not media_url:
            flash("Media is required for feed posts. Upload a file or paste a URL. Use the Forum for text-only posts.", "danger")
            return redirect(url_for("new_post"))

        post = Post(author_id=current_user.id, title=title, body=body, media_url=media_url)

        for tname in tag_names:
            tag = Tag.query.filter_by(name=tname).first()
            if not tag:
                tag = Tag(name=tname)
                db.session.add(tag)
            post.tags.append(tag)

        db.session.add(post)
        db.session.flush()  # get post.id
        final_xp, levelled_up = current_user.add_points(POST_REWARD_XP, reason=f"Posted: {title}")
        current_user.record_streak("post")

        # Notify followers
        for follower in current_user.followers:
            notify(follower.id, "follow",
                   f"{current_user.username} made a new post: \"{title}\"",
                   url_for("view_post", post_id=post.id))

        if levelled_up:
            notify(current_user.id, "level_up",
                   f"You reached level {current_user.level}!",
                   url_for("account"))

        record_market_snapshot(current_user)
        check_achievements(current_user)
        bump_daily_challenge(current_user, "posts")
        log_activity(current_user.id, "post",
                     f'Created post <a href="{url_for("view_post", post_id=post.id)}">{escape(title)}</a>')
        db.session.commit()
        msg = f"Post created! +{final_xp} XP"
        if levelled_up:
            msg += f" Level up! You are now level {current_user.level}!"
        flash(msg, "success")
        return redirect(url_for("feed"))
    return render_template("new_post.html")


@app.route("/post/<int:post_id>")
def view_post(post_id):
    post = Post.query.get_or_404(post_id)
    return render_template("view_post.html", post=post)


@app.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user in post.liked_by.all():
        post.liked_by.remove(current_user)
    else:
        post.liked_by.append(current_user)
        # Check if post author earned 'Popular' achievement
        if post.author.role == "sissy":
            check_achievements(post.author)
            bump_daily_challenge(post.author, "likes_received")
        log_activity(current_user.id, "like",
                     f'Liked <a href="{url_for("view_post", post_id=post.id)}">{escape(post.title)}</a> by <a href="{url_for("profile", username=post.author.username)}">{escape(post.author.username)}</a>')
        if post.author_id != current_user.id:
            notify(post.author_id, "like",
                   f"{current_user.username} liked your post \"{post.title}\"",
                   url_for("view_post", post_id=post.id))
    db.session.commit()
    return redirect(request.referrer or url_for("feed"))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@app.route("/tasks")
@login_required
def tasks():
    if current_user.role == "master":
        my_tasks = Task.query.filter_by(creator_id=current_user.id).order_by(Task.created_at.desc()).all()
    else:
        my_tasks = Task.query.filter(
            (Task.assignee_id == current_user.id) | (Task.status == "open")
        ).order_by(Task.created_at.desc()).all()
    return render_template("tasks.html", tasks=my_tasks)


@app.route("/task/new", methods=["GET", "POST"])
@login_required
@role_required("master")
def new_task():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        reward_coins = request.form.get("reward_coins", TASK_REWARD_COINS, type=int)
        reward_xp = request.form.get("reward_xp", TASK_REWARD_XP, type=int)
        assignee_username = request.form.get("assignee", "").strip()

        if not title or not description:
            flash("Title and description required.", "danger")
            return redirect(url_for("new_task"))

        if reward_coins > current_user.coins:
            flash("Not enough coins.", "danger")
            return redirect(url_for("new_task"))

        assignee = None
        if assignee_username:
            assignee = User.query.filter_by(username=assignee_username, role="sissy").first()

        task = Task(
            creator_id=current_user.id,
            assignee_id=assignee.id if assignee else None,
            title=title,
            description=description,
            reward_coins=reward_coins,
            reward_xp=reward_xp,
            status="accepted" if assignee else "open",
        )
        current_user.coins -= reward_coins  # escrow
        db.session.add(task)
        db.session.flush()
        log_activity(current_user.id, "task_create",
                     f'Created task "{escape(title)}" with {reward_coins} coin reward')
        db.session.commit()
        flash("Task created!", "success")
        return redirect(url_for("tasks"))
    sissies = User.query.filter_by(role="sissy").order_by(User.username).all()
    return render_template("new_task.html", sissies=sissies)


@app.route("/task/<int:task_id>/accept", methods=["POST"])
@login_required
@role_required("sissy")
def accept_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.status != "open":
        flash("Task is no longer open.", "warning")
        return redirect(url_for("tasks"))
    task.assignee_id = current_user.id
    task.status = "accepted"
    log_activity(current_user.id, "task_accept",
                 f'Accepted task "{escape(task.title)}" from <a href="{url_for("profile", username=task.creator.username)}">{escape(task.creator.username)}</a>')
    notify(task.creator_id, "task",
           f"{current_user.username} accepted your task \"{task.title}\"",
           url_for("tasks"))
    db.session.commit()
    flash("Task accepted!", "success")
    return redirect(url_for("tasks"))


@app.route("/task/<int:task_id>/complete", methods=["POST"])
@login_required
@role_required("sissy")
def complete_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.assignee_id != current_user.id or task.status != "accepted":
        abort(403)
    task.status = "completed"
    task.completed_at = datetime.utcnow()
    final_xp, levelled_up = current_user.add_points(task.reward_xp, reason=f"Task completed: {task.title}")
    current_user.coins += task.reward_coins
    current_user.record_streak("task")
    distribute_dividends(current_user, task.reward_coins)
    db.session.add(Transaction(user_id=current_user.id, amount=task.reward_coins, description=f"Task completed: {task.title}"))
    log_activity(current_user.id, "task_complete",
                 f'Completed task "{escape(task.title)}" — earned {task.reward_coins} coins + {task.reward_xp} XP')
    notify(task.creator_id, "task",
           f"{current_user.username} completed your task \"{task.title}\"",
           url_for("tasks"))
    if levelled_up:
        notify(current_user.id, "level_up",
               f"You reached level {current_user.level}!",
               url_for("account"))
    record_market_snapshot(current_user)
    check_achievements(current_user)
    bump_daily_challenge(current_user, "tasks")
    db.session.commit()
    msg = f"Task completed! +{final_xp} XP, +{task.reward_coins} coins"
    if levelled_up:
        msg += f" LEVEL UP! Now level {current_user.level}!"
    flash(msg, "success")
    return redirect(url_for("tasks"))


# ---------------------------------------------------------------------------
# Daily Challenges
# ---------------------------------------------------------------------------
@app.route("/challenges")
@login_required
def challenges():
    today = datetime.utcnow().date()
    generate_daily_challenges(today)
    day_challenges = DailyChallenge.query.filter_by(date=today).all()
    user_progress = {}
    for ch in day_challenges:
        uc = UserDailyChallenge.query.filter_by(user_id=current_user.id, challenge_id=ch.id).first()
        user_progress[ch.id] = uc
    return render_template("challenges.html", challenges=day_challenges, user_progress=user_progress, today=today)


@app.route("/challenges/claim/<int:challenge_id>", methods=["POST"])
@login_required
def claim_challenge(challenge_id):
    ch = DailyChallenge.query.get_or_404(challenge_id)
    uc = UserDailyChallenge.query.filter_by(user_id=current_user.id, challenge_id=ch.id).first()
    if not uc or not uc.completed or uc.claimed:
        flash("Cannot claim this challenge.", "warning")
        return redirect(url_for("challenges"))
    uc.claimed = True
    current_user.coins += ch.reward_coins
    if current_user.role == "sissy":
        current_user.add_points(ch.reward_xp, reason=f"Daily challenge: {ch.title}")
    db.session.add(Transaction(user_id=current_user.id, amount=ch.reward_coins,
                               description=f"Daily challenge reward: {ch.title}"))
    notify(current_user.id, "challenge", f"Challenge complete: {ch.title}! +{ch.reward_coins} coins, +{ch.reward_xp} XP")
    db.session.commit()
    flash(f"Challenge claimed! +{ch.reward_coins} coins, +{ch.reward_xp} XP!", "success")
    return redirect(url_for("challenges"))


# ---------------------------------------------------------------------------
# Shop / Cosmetics
# ---------------------------------------------------------------------------
def seed_shop_items():
    """Seed default shop items (idempotent)."""
    if ShopItem.query.count() > 0:
        return
    items = [
        ShopItem(name="Pink Glow Frame", description="A glowing pink border around your avatar", category="frame", css_class="frame-pink-glow", price=50, level_required=3, icon="bi-circle"),
        ShopItem(name="Gold Frame", description="A prestigious golden avatar frame", category="frame", css_class="frame-gold", price=150, level_required=10, icon="bi-circle"),
        ShopItem(name="Rainbow Frame", description="An animated rainbow avatar frame", category="frame", css_class="frame-rainbow", price=300, level_required=20, icon="bi-circle"),
        ShopItem(name="Neon Frame", description="Neon-lit avatar frame", category="frame", css_class="frame-neon", price=200, level_required=15, icon="bi-circle"),
        ShopItem(name="Princess", description="Display 'Princess' title on your profile", category="title", css_class="Princess", price=75, level_required=5, icon="bi-stars"),
        ShopItem(name="Queen", description="Display 'Queen' title on your profile", category="title", css_class="Queen", price=200, level_required=15, icon="bi-stars"),
        ShopItem(name="Goddess", description="Display 'Goddess' title on your profile", category="title", css_class="Goddess", price=500, level_required=25, icon="bi-stars"),
        ShopItem(name="Pink Flair", description="Your username glows pink", category="flair", css_class="#ff69b4", price=30, level_required=2, icon="bi-palette"),
        ShopItem(name="Purple Flair", description="Your username glows purple", category="flair", css_class="#9b59b6", price=30, level_required=2, icon="bi-palette"),
        ShopItem(name="Gold Flair", description="Your username glows gold", category="flair", css_class="#ffd700", price=100, level_required=10, icon="bi-palette"),
    ]
    db.session.add_all(items)
    db.session.commit()


@app.route("/shop")
@login_required
def shop():
    items = ShopItem.query.order_by(ShopItem.category, ShopItem.price).all()
    owned_ids = {p.item_id for p in UserPurchase.query.filter_by(user_id=current_user.id).all()}
    return render_template("shop.html", items=items, owned_ids=owned_ids)


@app.route("/shop/buy/<int:item_id>", methods=["POST"])
@login_required
def buy_shop_item(item_id):
    item = ShopItem.query.get_or_404(item_id)
    if UserPurchase.query.filter_by(user_id=current_user.id, item_id=item.id).first():
        flash("You already own this item.", "info")
        return redirect(url_for("shop"))
    if current_user.level < item.level_required:
        flash(f"You need level {item.level_required} to buy this.", "warning")
        return redirect(url_for("shop"))
    if current_user.coins < item.price:
        flash("Not enough coins!", "danger")
        return redirect(url_for("shop"))
    current_user.coins -= item.price
    db.session.add(UserPurchase(user_id=current_user.id, item_id=item.id))
    db.session.add(Transaction(user_id=current_user.id, amount=-item.price,
                               description=f"Bought: {item.name}"))
    db.session.commit()
    flash(f"Purchased {item.name}!", "success")
    return redirect(url_for("shop"))


@app.route("/shop/equip/<int:item_id>", methods=["POST"])
@login_required
def equip_shop_item(item_id):
    item = ShopItem.query.get_or_404(item_id)
    if not UserPurchase.query.filter_by(user_id=current_user.id, item_id=item.id).first():
        flash("You don't own this item.", "danger")
        return redirect(url_for("shop"))
    if item.category == "frame":
        current_user.profile_frame = item.css_class
    elif item.category == "title":
        current_user.profile_title = item.css_class
    elif item.category == "flair":
        current_user.flair_color = item.css_class
    db.session.commit()
    flash(f"Equipped {item.name}!", "success")
    return redirect(url_for("shop"))


# ---------------------------------------------------------------------------
# Admin-required decorator (used by forum admin controls + admin panel)
# ---------------------------------------------------------------------------
def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Forum (Storyden-inspired)
# ---------------------------------------------------------------------------
@app.route("/forum")
@login_required
def forum_index():
    """Forum home — list all categories with thread/reply counts."""
    categories = ForumCategory.query.order_by(ForumCategory.sort_order, ForumCategory.id).all()
    cat_stats = []
    for cat in categories:
        thread_count = cat.threads.count()
        reply_count = ForumReply.query.join(ForumThread).filter(ForumThread.category_id == cat.id).count()
        latest_thread = cat.threads.order_by(ForumThread.last_reply_at.desc()).first()
        cat_stats.append({
            "category": cat,
            "threads": thread_count,
            "replies": reply_count,
            "latest": latest_thread,
        })
    return render_template("forum/index.html", categories=cat_stats,
                           new_threads=ForumThread.query.order_by(ForumThread.created_at.desc()).limit(8).all())


@app.route("/forum/<int:category_id>")
@login_required
def forum_category(category_id):
    """List threads in a category."""
    category = ForumCategory.query.get_or_404(category_id)
    page = request.args.get("page", 1, type=int)
    threads = category.threads.paginate(page=page, per_page=20, error_out=False)
    return render_template("forum/category.html", category=category, threads=threads)


@app.route("/forum/<int:category_id>/new", methods=["GET", "POST"])
@login_required
def forum_new_thread(category_id):
    """Create a new thread."""
    category = ForumCategory.query.get_or_404(category_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        if not title or not body:
            flash("Title and body are required.", "danger")
            return redirect(url_for("forum_new_thread", category_id=category_id))
        thread = ForumThread(
            category_id=category.id,
            author_id=current_user.id,
            title=title,
            body=body,
        )
        db.session.add(thread)
        db.session.flush()
        # Extract hashtags from body
        for tag_name in extract_hashtags(body):
            tag = Tag.query.filter_by(name=tag_name).first()
            if not tag:
                tag = Tag(name=tag_name)
                db.session.add(tag)
            thread.tags.append(tag)
        # Award XP for sissies
        if current_user.role == "sissy":
            current_user.add_points(5, reason=f"Forum thread: {title}")
            record_market_snapshot(current_user)
        log_activity(current_user.id, "forum_thread",
                     f'Created forum thread <a href="{url_for("forum_thread", thread_id=thread.id)}">{escape(title)}</a> in {escape(category.name)}')
        db.session.commit()
        flash("Thread created!", "success")
        return redirect(url_for("forum_thread", thread_id=thread.id))
    return render_template("forum/new_thread.html", category=category)


@app.route("/forum/thread/<int:thread_id>", methods=["GET", "POST"])
@login_required
def forum_thread(thread_id):
    """View a thread and its replies. POST adds a reply."""
    thread = ForumThread.query.get_or_404(thread_id)
    thread.view_count += 1

    if request.method == "POST":
        if not current_user.is_authenticated:
            flash("Please log in to reply.", "info")
            return redirect(url_for("login"))
        if thread.is_locked:
            flash("This thread is locked.", "warning")
            return redirect(url_for("forum_thread", thread_id=thread.id))
        body = request.form.get("body", "").strip()
        if not body:
            flash("Reply cannot be empty.", "danger")
            return redirect(url_for("forum_thread", thread_id=thread.id))
        reply = ForumReply(
            thread_id=thread.id,
            author_id=current_user.id,
            body=body,
        )
        thread.last_reply_at = datetime.utcnow()
        db.session.add(reply)
        # Extract hashtags from reply and attach to the parent thread
        existing_tag_names = {t.name for t in thread.tags}
        for tag_name in extract_hashtags(body):
            if tag_name not in existing_tag_names:
                tag = Tag.query.filter_by(name=tag_name).first()
                if not tag:
                    tag = Tag(name=tag_name)
                    db.session.add(tag)
                thread.tags.append(tag)
                existing_tag_names.add(tag_name)
        # Notify thread author
        if thread.author_id != current_user.id:
            notify(thread.author_id, "forum_reply",
                   f"{current_user.username} replied to \"{thread.title}\"",
                   url_for("forum_thread", thread_id=thread.id))
        # XP for sissies
        if current_user.role == "sissy":
            current_user.add_points(2, reason=f"Forum reply in: {thread.title}")
        bump_daily_challenge(current_user, "forum_replies")
        log_activity(current_user.id, "forum_reply",
                     f'Replied to <a href="{url_for("forum_thread", thread_id=thread.id)}">{escape(thread.title)}</a>')
        db.session.commit()
        flash("Reply posted!", "success")
        return redirect(url_for("forum_thread", thread_id=thread.id))

    page = request.args.get("page", 1, type=int)
    replies = thread.replies.paginate(page=page, per_page=25, error_out=False)
    db.session.commit()  # save view_count
    return render_template("forum/thread.html", thread=thread, replies=replies)


@app.route("/forum/thread/<int:thread_id>/pin", methods=["POST"])
@login_required
@admin_required
def forum_pin_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    thread.is_pinned = not thread.is_pinned
    db.session.commit()
    flash(f"Thread {'pinned' if thread.is_pinned else 'unpinned'}.", "info")
    return redirect(url_for("forum_thread", thread_id=thread.id))


@app.route("/forum/thread/<int:thread_id>/lock", methods=["POST"])
@login_required
@admin_required
def forum_lock_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    thread.is_locked = not thread.is_locked
    db.session.commit()
    flash(f"Thread {'locked' if thread.is_locked else 'unlocked'}.", "info")
    return redirect(url_for("forum_thread", thread_id=thread.id))


@app.route("/forum/thread/<int:thread_id>/delete", methods=["POST"])
@login_required
@admin_required
def forum_delete_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    cat_id = thread.category_id
    ForumReply.query.filter_by(thread_id=thread.id).delete()
    db.session.delete(thread)
    db.session.commit()
    flash("Thread deleted.", "warning")
    return redirect(url_for("forum_category", category_id=cat_id))


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------
@app.route("/leaderboard")
@login_required
def leaderboard():
    tab = request.args.get("tab", "sissies")
    top_sissies = User.query.filter_by(role="sissy").order_by(User.xp.desc()).limit(50).all()

    # Whales: masters ranked by total portfolio value (coins + current investment worth)
    masters = User.query.filter_by(role="master").all()
    whale_list = []
    for m in masters:
        investments = Investment.query.filter_by(investor_id=m.id).all()
        portfolio_value = 0
        for inv in investments:
            if inv.bought_value > 0:
                portfolio_value += int(inv.amount * (inv.sissy.market_value / inv.bought_value))
            else:
                portfolio_value += inv.amount
        whale_list.append({
            "user": m,
            "portfolio_value": portfolio_value,
            "total_wealth": m.coins + portfolio_value,
            "num_investments": len(investments),
            "coins": m.coins,
        })
    whale_list.sort(key=lambda w: w["total_wealth"], reverse=True)
    whale_list = whale_list[:50]

    return render_template("leaderboard.html", users=top_sissies, whales=whale_list,
                           tiers=TIERS, level_cap=LEVEL_CAP, tab=tab)


# ---------------------------------------------------------------------------
# Sissy Market
# ---------------------------------------------------------------------------
@app.route("/market")
@login_required
def market():
    sissies = User.query.filter_by(role="sissy", listed_on_market=True).filter(User.level >= 5).order_by(User.xp.desc()).all()
    market_data = []
    now = datetime.utcnow()
    for s in sissies:
        total_invested = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=s.id).scalar() or 0
        investors = Investment.query.filter_by(sissy_id=s.id).count()
        current_mv = s.market_value

        # --- Actual Growth: % change from earliest snapshot ---
        first_snap = MarketSnapshot.query.filter_by(sissy_id=s.id).order_by(MarketSnapshot.timestamp.asc()).first()
        if first_snap and first_snap.market_value > 0:
            actual_growth = round((current_mv - first_snap.market_value) / first_snap.market_value * 100, 1)
        else:
            actual_growth = 0.0

        # --- Expected Growth: project next period from recent momentum ---
        recent_snaps = MarketSnapshot.query.filter_by(sissy_id=s.id).order_by(
            MarketSnapshot.timestamp.desc()
        ).limit(5).all()
        if len(recent_snaps) >= 2:
            oldest = recent_snaps[-1]
            newest = recent_snaps[0]
            delta_val = newest.market_value - oldest.market_value
            hours = max((newest.timestamp - oldest.timestamp).total_seconds() / 3600, 0.1)
            rate_per_hour = delta_val / hours
            # Project 24h forward as a % of current value
            if current_mv > 0:
                expected_growth = round(rate_per_hour * 24 / current_mv * 100, 1)
            else:
                expected_growth = 0.0
        else:
            expected_growth = 0.0

        market_data.append({
            "user": s,
            "value": current_mv,
            "total_invested": total_invested,
            "investors": investors,
            "actual_growth": actual_growth,
            "expected_growth": expected_growth,
        })
    # Sort by market value desc
    market_data.sort(key=lambda x: x["value"], reverse=True)

    my_investments = []
    if current_user.is_authenticated and current_user.role == "master":
        my_investments = Investment.query.filter_by(investor_id=current_user.id).all()

    return render_template("market.html", market_data=market_data, my_investments=my_investments)


@app.route("/market/invest/<int:sissy_id>", methods=["POST"])
@login_required
@role_required("master")
def invest(sissy_id):
    sissy = User.query.get_or_404(sissy_id)
    if sissy.role != "sissy":
        abort(400)
    if sissy.level < 5:
        flash(f"{sissy.username} must reach level 5 before receiving investments.", "danger")
        return redirect(url_for("market"))
    amount = request.form.get("amount", 0, type=int)
    if amount <= 0:
        flash("Invalid amount.", "danger")
        return redirect(url_for("market"))
    if amount > current_user.coins:
        flash("Not enough coins.", "danger")
        return redirect(url_for("market"))

    inv = Investment(
        investor_id=current_user.id,
        sissy_id=sissy.id,
        amount=amount,
        bought_value=sissy.market_value,
    )
    current_user.coins -= amount
    # 25% of investment goes to sissy's funded balance
    funding = int(amount * 0.25)
    sissy.funded_coins += funding
    db.session.add(Transaction(user_id=sissy.id, amount=funding,
                               description=f"Funded by {current_user.username}'s investment"))
    db.session.add(inv)
    db.session.add(Transaction(user_id=current_user.id, amount=-amount, description=f"Invested in {sissy.username}"))
    log_activity(current_user.id, "invest",
                 f'Invested {amount} coins in <a href="{url_for("profile", username=sissy.username)}">{escape(sissy.username)}</a>')
    notify(sissy.id, "invest",
           f"{current_user.username} invested {amount} coins in you! +{funding} funded coins.",
           url_for("wallet"))
    record_market_snapshot(sissy)
    db.session.commit()
    flash(f"Invested {amount} coins in {sissy.username}!", "success")
    return redirect(url_for("market"))


@app.route("/market/sell/<int:investment_id>", methods=["POST"])
@login_required
@role_required("master")
def sell_investment(investment_id):
    inv = Investment.query.get_or_404(investment_id)
    if inv.investor_id != current_user.id:
        abort(403)
    sissy = User.query.get(inv.sissy_id)
    # Calculate return: proportional to how much sissy has grown
    if inv.bought_value > 0:
        growth = sissy.market_value / inv.bought_value
    else:
        growth = 1.0
    payout = int(inv.amount * growth)
    current_user.coins += payout
    profit = payout - inv.amount
    db.session.add(Transaction(user_id=current_user.id, amount=payout, description=f"Sold investment in {sissy.username} (profit: {profit})"))
    log_activity(current_user.id, "invest",
                 f'Sold investment in <a href="{url_for("profile", username=sissy.username)}">{escape(sissy.username)}</a> for {payout} coins (profit: {profit:+d})')
    db.session.delete(inv)
    record_market_snapshot(sissy)
    db.session.commit()
    flash(f"Sold investment for {payout} coins (profit: {profit:+d})!", "success")
    return redirect(url_for("market"))


# ---------------------------------------------------------------------------
# Wallet (Sissy funded balance)
# ---------------------------------------------------------------------------
FLAIR_COLORS = {
    "pink": "#ff4da6",
    "gold": "#ffd700",
    "purple": "#a855f7",
    "cyan": "#22d3ee",
    "red": "#ef4444",
    "green": "#22c55e",
    "white": "#ffffff",
}
FLAIR_COST = 50
BOOST_COST = 30

@app.route("/wallet")
@login_required
@role_required("sissy")
def wallet():
    investments_in_me = Investment.query.filter_by(sissy_id=current_user.id).all()
    total_funded = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=current_user.id).scalar() or 0
    sponsor = get_sponsor(current_user)
    my_posts = current_user.posts.order_by(Post.created_at.desc()).limit(20).all()
    return render_template("wallet.html",
        investments=investments_in_me, total_funded=total_funded,
        sponsor=sponsor, my_posts=my_posts,
        flair_colors=FLAIR_COLORS, flair_cost=FLAIR_COST, boost_cost=BOOST_COST)


@app.route("/wallet/boost/<int:post_id>", methods=["POST"])
@login_required
@role_required("sissy")
def boost_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.author_id != current_user.id:
        abort(403)
    if current_user.funded_coins < BOOST_COST:
        flash(f"Need {BOOST_COST} funded coins to boost. You have {current_user.funded_coins}.", "danger")
        return redirect(url_for("wallet"))
    if post.is_boosted:
        flash("Post is already boosted.", "info")
        return redirect(url_for("wallet"))
    current_user.funded_coins -= BOOST_COST
    post.is_boosted = True
    db.session.add(Transaction(user_id=current_user.id, amount=-BOOST_COST,
                               description=f"Boosted post: {post.title}"))
    log_activity(current_user.id, "boost",
                 f'Boosted post "<a href="{url_for("view_post", post_id=post.id)}">{escape(post.title)}</a>"')
    db.session.commit()
    flash(f"Post boosted! It will appear at the top of the feed.", "success")
    return redirect(url_for("wallet"))


@app.route("/wallet/flair", methods=["POST"])
@login_required
@role_required("sissy")
def buy_flair():
    color_name = request.form.get("color", "").strip().lower()
    if color_name not in FLAIR_COLORS:
        flash("Invalid flair color.", "danger")
        return redirect(url_for("wallet"))
    if current_user.funded_coins < FLAIR_COST:
        flash(f"Need {FLAIR_COST} funded coins for flair. You have {current_user.funded_coins}.", "danger")
        return redirect(url_for("wallet"))
    current_user.funded_coins -= FLAIR_COST
    current_user.flair_color = FLAIR_COLORS[color_name]
    db.session.add(Transaction(user_id=current_user.id, amount=-FLAIR_COST,
                               description=f"Bought {color_name} username flair"))
    log_activity(current_user.id, "flair",
                 f'Bought <span style="color:{FLAIR_COLORS[color_name]}">{color_name}</span> username flair')
    db.session.commit()
    flash(f"Username flair set to {color_name}!", "success")
    return redirect(url_for("wallet"))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results_users = []
    results_posts = []
    if q:
        results_users = User.query.filter(User.username.ilike(f"%{q}%")).limit(20).all()
        results_posts = Post.query.filter(
            Post.title.ilike(f"%{q}%") | Post.body.ilike(f"%{q}%")
        ).order_by(Post.created_at.desc()).limit(20).all()
    return render_template("search.html", q=q, users=results_users, posts=results_posts)


# ---------------------------------------------------------------------------
# Hashtag Pages (Instagram-style)
# ---------------------------------------------------------------------------
@app.route("/hashtag/<tag_name>")
def hashtag_page(tag_name):
    tag_name = tag_name.lower().strip()
    tag = Tag.query.filter_by(name=tag_name).first()
    posts = []
    threads = []
    if tag:
        posts = Post.query.filter(Post.tags.any(Tag.id == tag.id)).order_by(Post.created_at.desc()).limit(50).all()
        threads = ForumThread.query.filter(ForumThread.tags.any(Tag.id == tag.id)).order_by(ForumThread.created_at.desc()).limit(50).all()
    return render_template("hashtag.html", tag_name=tag_name, posts=posts, threads=threads)


# ---------------------------------------------------------------------------
# Account / Profile
# ---------------------------------------------------------------------------
@app.route("/account")
@login_required
def account():
    transactions = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.created_at.desc()).limit(20).all()
    activities = Activity.query.filter_by(user_id=current_user.id).order_by(Activity.created_at.desc()).limit(50).all()
    xp_history = XPAudit.query.filter_by(user_id=current_user.id).order_by(XPAudit.created_at.desc()).limit(20).all()
    user_achievements = current_user.get_user_achievements() if current_user.role == "sissy" else []
    all_achievements = Achievement.query.all() if current_user.role == "sissy" else []
    login_streak = current_user.get_current_streak_count("login") if current_user.role == "sissy" else 0
    post_streak = current_user.get_current_streak_count("post") if current_user.role == "sissy" else 0
    active_multipliers = Multiplier.query.filter(Multiplier.is_active == True).all()
    return render_template("account.html", transactions=transactions, activities=activities, tiers=TIERS,
                           xp_history=xp_history, user_achievements=user_achievements,
                           all_achievements=all_achievements, login_streak=login_streak,
                           post_streak=post_streak, active_multipliers=active_multipliers,
                           level_xp_table=LEVEL_XP_TABLE)


@app.route("/account/edit", methods=["POST"])
@login_required
def edit_account():
    bio = request.form.get("bio", "").strip()
    current_user.bio = bio
    if current_user.role == "master":
        current_user.show_wealth = bool(request.form.get("show_wealth"))
    if current_user.role == "sissy":
        current_user.listed_on_market = bool(request.form.get("listed_on_market"))
    db.session.commit()
    flash("Profile updated!", "success")
    return redirect(url_for("account"))


@app.route("/account/new_avatar", methods=["POST"])
@login_required
def new_avatar():
    """Generate a new random Multiavatar."""
    seed = f"{current_user.username}-{secrets.token_hex(4)}"
    current_user.avatar_url = generate_avatar(seed)
    db.session.commit()
    flash("New avatar generated!", "success")
    return redirect(url_for("account"))


@app.route("/account/buy_coins", methods=["POST"])
@login_required
@role_required("master")
def buy_coins():
    # Proof of concept – just add coins instantly (no real payment)
    amount = request.form.get("amount", 0, type=int)
    if amount <= 0 or amount > 10000:
        flash("Invalid amount (1-10000).", "danger")
        return redirect(url_for("account"))
    current_user.coins += amount
    db.session.add(Transaction(user_id=current_user.id, amount=amount, description=f"Purchased {amount} coins"))
    log_activity(current_user.id, "buy_coins",
                 f'Purchased {amount} coins')
    db.session.commit()
    flash(f"Added {amount} coins to your account!", "success")
    return redirect(url_for("account"))


@app.route("/profile/<string:username>")
def profile(username):
    if not current_user.is_authenticated:
        flash("To view user profiles you must log in.", "info")
        return redirect(url_for("login", next=request.url))
    user = User.query.filter_by(username=username).first_or_404()
    posts = user.posts.order_by(Post.created_at.desc()).limit(20).all() if user.role == "sissy" else []
    threads = ForumThread.query.filter_by(author_id=user.id).order_by(ForumThread.created_at.desc()).limit(20).all()
    completed_tasks = Task.query.filter_by(assignee_id=user.id, status="completed").count() if user.role == "sissy" else 0
    user_achievements = user.get_user_achievements() if user.role == "sissy" else []
    login_streak = user.get_current_streak_count("login") if user.role == "sissy" else 0
    sponsor = get_sponsor(user) if user.role == "sissy" else None
    return render_template("profile.html", user=user, posts=posts, threads=threads,
                           completed_tasks=completed_tasks,
                           user_achievements=user_achievements, login_streak=login_streak,
                           sponsor=sponsor)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def add_comment(post_id):
    post = Post.query.get_or_404(post_id)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment cannot be empty.", "danger")
        return redirect(url_for("view_post", post_id=post.id))
    comment = Comment(post_id=post.id, author_id=current_user.id, body=body)
    db.session.add(comment)
    log_activity(current_user.id, "comment",
                 f'Commented on <a href="{url_for("view_post", post_id=post.id)}">{escape(post.title)}</a> by <a href="{url_for("profile", username=post.author.username)}">{escape(post.author.username)}</a>')
    if post.author_id != current_user.id:
        notify(post.author_id, "comment",
               f"{current_user.username} commented on \"{post.title}\"",
               url_for("view_post", post_id=post.id))
    # Award XP for commenting (sissy only)
    if current_user.role == "sissy":
        current_user.add_points(3, reason=f"Commented on: {post.title}")
        record_market_snapshot(current_user)
    bump_daily_challenge(current_user, "comments")
    db.session.commit()
    flash("Comment added!", "success")
    return redirect(url_for("view_post", post_id=post.id))


# ---------------------------------------------------------------------------
# Follow / Unfollow
# ---------------------------------------------------------------------------
@app.route("/follow/<string:username>", methods=["POST"])
@login_required
def follow_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    if user.id == current_user.id:
        flash("You can't follow yourself.", "warning")
        return redirect(url_for("profile", username=username))
    if current_user.followed.filter(followers_table.c.followed_id == user.id).count() == 0:
        current_user.followed.append(user)
        log_activity(current_user.id, "follow",
                     f'Followed <a href="{url_for("profile", username=user.username)}">{escape(user.username)}</a>')
        notify(user.id, "follow",
               f"{current_user.username} started following you!",
               url_for("profile", username=current_user.username))
        if user.role == "sissy":
            record_market_snapshot(user)
        db.session.commit()
        flash(f"You are now following {user.username}!", "success")
    else:
        flash(f"Already following {user.username}.", "info")
    return redirect(url_for("profile", username=username))


@app.route("/unfollow/<string:username>", methods=["POST"])
@login_required
def unfollow_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    if current_user.followed.filter(followers_table.c.followed_id == user.id).count() > 0:
        current_user.followed.remove(user)
        log_activity(current_user.id, "unfollow",
                     f'Unfollowed <a href="{url_for("profile", username=user.username)}">{escape(user.username)}</a>')
        if user.role == "sissy":
            record_market_snapshot(user)
        db.session.commit()
        flash(f"Unfollowed {user.username}.", "info")
    return redirect(url_for("profile", username=username))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
@app.route("/notifications")
@login_required
def notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return render_template("notifications.html", notifications=notifs)


@app.route("/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    flash("All notifications marked as read.", "info")
    return redirect(url_for("notifications"))


@app.route("/notifications/count")
@login_required
def notification_count():
    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({"count": count})


@app.route("/messages/unread_count")
@login_required
def messages_unread_count():
    count = Message.query.filter_by(recipient_id=current_user.id, is_read=False).count()
    return jsonify({"count": count})


# ---------------------------------------------------------------------------
# Direct Messages
# ---------------------------------------------------------------------------
@app.route("/messages")
@login_required
def messages_inbox():
    # Get unique conversations
    sent_to = db.session.query(Message.recipient_id).filter_by(sender_id=current_user.id).distinct()
    received_from = db.session.query(Message.sender_id).filter_by(recipient_id=current_user.id).distinct()
    partner_ids = set()
    for (uid,) in sent_to:
        partner_ids.add(uid)
    for (uid,) in received_from:
        partner_ids.add(uid)
    conversations = []
    for pid in partner_ids:
        partner = db.session.get(User, pid)
        last_msg = Message.query.filter(
            db.or_(
                db.and_(Message.sender_id == current_user.id, Message.recipient_id == pid),
                db.and_(Message.sender_id == pid, Message.recipient_id == current_user.id),
            )
        ).order_by(Message.created_at.desc()).first()
        unread = Message.query.filter_by(sender_id=pid, recipient_id=current_user.id, is_read=False).count()
        conversations.append({"partner": partner, "last_msg": last_msg, "unread": unread})
    conversations.sort(key=lambda c: c["last_msg"].created_at if c["last_msg"] else datetime.min, reverse=True)
    return render_template("messages_inbox.html", conversations=conversations)


@app.route("/messages/<string:username>", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per minute")
def message_thread(username):
    partner = User.query.filter_by(username=username).first_or_404()
    if partner.id == current_user.id:
        return redirect(url_for("messages_inbox"))

    # Block check
    if current_user.blocked.filter(blocked_users.c.blocked_id == partner.id).count() > 0:
        flash(f"You have blocked {partner.username}. Unblock to message them.", "warning")
        return redirect(url_for("messages_inbox"))
    if partner.blocked.filter(blocked_users.c.blocked_id == current_user.id).count() > 0:
        flash("You cannot message this user.", "warning")
        return redirect(url_for("messages_inbox"))

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body:
            msg = Message(sender_id=current_user.id, recipient_id=partner.id, body=body)
            db.session.add(msg)
            notify(partner.id, "message",
                   f"New message from {current_user.username}",
                   url_for("message_thread", username=current_user.username))
            db.session.commit()
        return redirect(url_for("message_thread", username=username))

    # Mark messages from partner as read
    Message.query.filter_by(sender_id=partner.id, recipient_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()

    msgs = Message.query.filter(
        db.or_(
            db.and_(Message.sender_id == current_user.id, Message.recipient_id == partner.id),
            db.and_(Message.sender_id == partner.id, Message.recipient_id == current_user.id),
        )
    ).order_by(Message.created_at.asc()).limit(100).all()
    return render_template("message_thread.html", partner=partner, messages=msgs)


# ---------------------------------------------------------------------------
# Report content
# ---------------------------------------------------------------------------
@app.route("/report/post/<int:post_id>", methods=["POST"])
@login_required
def report_post(post_id):
    post = Post.query.get_or_404(post_id)
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Please provide a reason for reporting.", "danger")
        return redirect(url_for("view_post", post_id=post_id))
    existing = Report.query.filter_by(reporter_id=current_user.id, post_id=post_id, status="open").first()
    if existing:
        flash("You have already reported this post.", "info")
        return redirect(url_for("view_post", post_id=post_id))
    db.session.add(Report(reporter_id=current_user.id, post_id=post_id, reason=reason))
    db.session.commit()
    flash("Report submitted. A moderator will review it.", "success")
    return redirect(url_for("view_post", post_id=post_id))


@app.route("/report/thread/<int:thread_id>", methods=["POST"])
@login_required
def report_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Please provide a reason for reporting.", "danger")
        return redirect(url_for("forum_thread", thread_id=thread_id))
    existing = Report.query.filter_by(reporter_id=current_user.id, thread_id=thread_id, status="open").first()
    if existing:
        flash("You have already reported this thread.", "info")
        return redirect(url_for("forum_thread", thread_id=thread_id))
    db.session.add(Report(reporter_id=current_user.id, thread_id=thread_id, reason=reason))
    db.session.commit()
    flash("Report submitted. A moderator will review it.", "success")
    return redirect(url_for("forum_thread", thread_id=thread_id))


# ---------------------------------------------------------------------------
# Block / Unblock users
# ---------------------------------------------------------------------------
@app.route("/block/<string:username>", methods=["POST"])
@login_required
def block_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    if user.id == current_user.id:
        flash("You can't block yourself.", "warning")
        return redirect(url_for("profile", username=username))
    if current_user.blocked.filter(blocked_users.c.blocked_id == user.id).count() == 0:
        current_user.blocked.append(user)
        db.session.commit()
        flash(f"{user.username} has been blocked.", "info")
    else:
        flash(f"{user.username} is already blocked.", "info")
    return redirect(url_for("profile", username=username))


@app.route("/unblock/<string:username>", methods=["POST"])
@login_required
def unblock_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    if current_user.blocked.filter(blocked_users.c.blocked_id == user.id).count() > 0:
        current_user.blocked.remove(user)
        db.session.commit()
        flash(f"{user.username} has been unblocked.", "info")
    return redirect(url_for("profile", username=username))


# ---------------------------------------------------------------------------
# Gifting / Tipping
# ---------------------------------------------------------------------------
@app.route("/gift/<string:username>", methods=["POST"])
@login_required
def gift_coins(username):
    recipient = User.query.filter_by(username=username).first_or_404()
    if recipient.id == current_user.id:
        flash("You can't gift yourself.", "warning")
        return redirect(url_for("profile", username=username))

    amount = request.form.get("amount", 0, type=int)
    if amount <= 0:
        flash("Invalid amount.", "danger")
        return redirect(url_for("profile", username=username))
    if amount > current_user.coins:
        flash("Not enough coins.", "danger")
        return redirect(url_for("profile", username=username))

    current_user.coins -= amount
    db.session.add(Transaction(user_id=current_user.id, amount=-amount, description=f"Gift to {recipient.username}"))
    db.session.add(PendingGift(sender_id=current_user.id, recipient_id=recipient.id, amount=amount))
    log_activity(current_user.id, "gift",
                 f'Gifted {amount} coins to <a href="{url_for("profile", username=recipient.username)}">{escape(recipient.username)}</a>')
    notify(recipient.id, "gift",
           f"{current_user.username} sent you {amount} coins! Open your gift chest to claim.",
           url_for("gifts"))
    db.session.commit()
    flash(f"Sent {amount} coins to {recipient.username}! They must claim it from their chest.", "success")
    return redirect(url_for("profile", username=username))


# ---------------------------------------------------------------------------
# Gift Chest – view & claim pending gifts
# ---------------------------------------------------------------------------
@app.route("/gifts")
@login_required
def gifts():
    pending = PendingGift.query.filter_by(recipient_id=current_user.id, is_claimed=False)\
        .order_by(PendingGift.created_at.desc()).all()
    claimed = PendingGift.query.filter_by(recipient_id=current_user.id, is_claimed=True)\
        .order_by(PendingGift.claimed_at.desc()).limit(50).all()
    return render_template("gifts.html", pending=pending, claimed=claimed)


@app.route("/gifts/claim/<int:gift_id>", methods=["POST"])
@login_required
def claim_gift(gift_id):
    gift = PendingGift.query.get_or_404(gift_id)
    if gift.recipient_id != current_user.id:
        abort(403)
    if gift.is_claimed:
        flash("Already claimed.", "info")
        return redirect(url_for("gifts"))
    gift.is_claimed = True
    gift.claimed_at = datetime.utcnow()
    current_user.coins += gift.amount
    distribute_dividends(current_user, gift.amount)
    db.session.add(Transaction(user_id=current_user.id, amount=gift.amount,
                               description=f"Gift claimed from {gift.sender.username}"))
    log_activity(current_user.id, "gift",
                 f'Claimed {gift.amount} coins gifted by <a href="{url_for("profile", username=gift.sender.username)}">{escape(gift.sender.username)}</a>')
    db.session.commit()
    flash(f"Claimed {gift.amount} coins from {gift.sender.username}!", "success")
    return redirect(url_for("gifts"))


@app.route("/gifts/claim-all", methods=["POST"])
@login_required
def claim_all_gifts():
    pending = PendingGift.query.filter_by(recipient_id=current_user.id, is_claimed=False).all()
    if not pending:
        flash("No gifts to claim.", "info")
        return redirect(url_for("gifts"))
    total = 0
    for gift in pending:
        gift.is_claimed = True
        gift.claimed_at = datetime.utcnow()
        total += gift.amount
    current_user.coins += total
    distribute_dividends(current_user, total)
    db.session.add(Transaction(user_id=current_user.id, amount=total,
                               description=f"Claimed {len(pending)} gifts ({total} coins)"))
    log_activity(current_user.id, "gift",
                 f'Claimed {len(pending)} gifts totalling {total} coins')
    db.session.commit()
    flash(f"Claimed {len(pending)} gifts for {total} coins!", "success")
    return redirect(url_for("gifts"))


# ---------------------------------------------------------------------------
# Admin / Moderation Panel
# ---------------------------------------------------------------------------


@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    users = User.query.order_by(User.created_at.desc()).all()
    posts = Post.query.order_by(Post.created_at.desc()).limit(50).all()
    open_reports = Report.query.filter_by(status="open").order_by(Report.created_at.desc()).all()
    return render_template("admin.html", users=users, posts=posts, open_reports=open_reports)


@app.route("/admin/delete_post/<int:post_id>", methods=["POST"])
@login_required
@admin_required
def admin_delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    notify(post.author_id, "moderation",
           f"Your post \"{post.title}\" was removed by a moderator.",
           url_for("feed"))
    # Remove likes, comments, tags
    post.liked_by = []
    Comment.query.filter_by(post_id=post.id).delete()
    post.tags = []
    db.session.delete(post)
    db.session.commit()
    flash(f"Post #{post_id} deleted.", "warning")
    return redirect(url_for("admin_panel"))


@app.route("/admin/ban_user/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_ban_user(user_id):
    user = db.session.get(User, user_id)
    if user and user.role != "admin":
        user.bio = "[BANNED] " + (user.bio or "")
        flash(f"User {user.username} banned.", "warning")
        db.session.commit()
    return redirect(url_for("admin_panel"))


@app.route("/admin/rankup", methods=["POST"])
@login_required
@admin_required
def admin_rankup():
    username = request.form.get("username", "").strip()
    target_level = request.form.get("target_level", 0, type=int)
    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f"User '{username}' not found.", "danger")
        return redirect(url_for("admin_panel"))
    target_level = max(1, min(target_level, LEVEL_CAP))
    if target_level <= user.level:
        flash(f"{user.username} is already level {user.level}.", "warning")
        return redirect(url_for("admin_panel"))
    needed_xp = LEVEL_XP_TABLE.get(target_level, 0)
    if user.xp < needed_xp:
        xp_grant = needed_xp - user.xp
        user.xp = needed_xp
    else:
        xp_grant = 0
    user.level = target_level
    db.session.commit()
    flash(f"{user.username} ranked up to level {target_level} (+{xp_grant} XP).", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/set_market_value", methods=["POST"])
@login_required
@admin_required
def admin_set_market_value():
    username = request.form.get("username", "").strip()
    new_value = request.form.get("market_value", 0, type=int)
    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f"User '{username}' not found.", "danger")
        return redirect(url_for("admin_panel"))
    if user.role != "sissy":
        flash(f"{user.username} is not a sissy — only sissies have a market value.", "warning")
        return redirect(url_for("admin_panel"))
    old_value = user.market_value
    # Set boost so that computed + boost = target
    user.market_value_boost = new_value - (old_value - int(user.market_value_boost or 0))
    record_market_snapshot(user)
    db.session.commit()
    flash(f"{user.username} market value adjusted: {old_value} → {user.market_value} (boost: {int(user.market_value_boost or 0):+d}).", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/report/<int:report_id>/dismiss", methods=["POST"])
@login_required
@admin_required
def admin_dismiss_report(report_id):
    report = Report.query.get_or_404(report_id)
    report.status = "dismissed"
    db.session.commit()
    flash("Report dismissed.", "info")
    return redirect(url_for("admin_panel"))


@app.route("/admin/report/<int:report_id>/action", methods=["POST"])
@login_required
@admin_required
def admin_action_report(report_id):
    """Mark report reviewed and delete the offending content."""
    report = Report.query.get_or_404(report_id)
    report.status = "reviewed"
    if report.post:
        post = report.post
        notify(post.author_id, "moderation",
               f"Your post \"{post.title}\" was removed after a report.",
               url_for("feed"))
        post.liked_by = []
        Comment.query.filter_by(post_id=post.id).delete()
        post.tags = []
        db.session.delete(post)
    if report.thread:
        thread = report.thread
        ForumReply.query.filter_by(thread_id=thread.id).delete()
        thread.tags = []
        db.session.delete(thread)
    db.session.commit()
    flash("Content removed and report resolved.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/grant_multiplier", methods=["POST"])
@login_required
@admin_required
def admin_grant_multiplier():
    name = request.form.get("name", "Bonus Event").strip()
    mult_value = request.form.get("multiplier", 2.0, type=float)
    hours = request.form.get("hours", 24, type=int)
    m = Multiplier(
        name=name, multiplier=mult_value,
        starts_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=hours),
    )
    db.session.add(m)
    db.session.commit()
    flash(f"Multiplier '{name}' ({mult_value}x) active for {hours}h.", "success")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Context processor: inject unread notification count into all templates
# ---------------------------------------------------------------------------
@app.context_processor
def inject_notification_count():
    if current_user.is_authenticated:
        unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        unread_msgs = Message.query.filter_by(recipient_id=current_user.id, is_read=False).count()
        unclaimed = PendingGift.query.filter_by(recipient_id=current_user.id, is_claimed=False).count()
        return {"unread_notifications": unread, "unread_messages": unread_msgs, "unclaimed_gifts": unclaimed}
    return {"unread_notifications": 0, "unread_messages": 0, "unclaimed_gifts": 0}


# ---------------------------------------------------------------------------
# Market Metrics & Chart
# ---------------------------------------------------------------------------
@app.route("/market/metrics/<int:sissy_id>")
@login_required
def market_metrics(sissy_id):
    sissy = User.query.get_or_404(sissy_id)
    if sissy.role != "sissy":
        abort(404)
    snapshots = MarketSnapshot.query.filter_by(sissy_id=sissy.id).order_by(MarketSnapshot.timestamp).all()
    completed_tasks = Task.query.filter_by(assignee_id=sissy.id, status="completed").count()
    total_invested = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=sissy.id).scalar() or 0
    investor_count = Investment.query.filter_by(sissy_id=sissy.id).count()
    return render_template(
        "market_metrics.html",
        sissy=sissy,
        snapshots=snapshots,
        completed_tasks=completed_tasks,
        total_invested=total_invested,
        investor_count=investor_count,
    )


@app.route("/admin/chart/growth.png")
@login_required
@admin_required
def admin_growth_chart():
    """Generate a site-wide growth chart across all key metrics."""
    tf = request.args.get("tf", "30d")
    tf_map = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "all": None,
    }
    window = tf_map.get(tf, timedelta(days=30))
    now = datetime.utcnow()
    cutoff = (now - window) if window else datetime(2000, 1, 1)

    # --- Gather cumulative counts bucketed by time ---
    # Choose bucket size based on timeframe
    if tf in ("1h", "6h"):
        bucket_fmt = "%Y-%m-%d %H:00"
        freq = "h"
    elif tf == "1d":
        bucket_fmt = "%Y-%m-%d %H:00"
        freq = "h"
    else:
        bucket_fmt = "%Y-%m-%d"
        freq = "D"

    # Query raw created_at timestamps from each model
    def _dates(model, date_col="created_at"):
        col = getattr(model, date_col)
        return [r[0] for r in db.session.query(col).filter(col >= cutoff).all() if r[0]]

    user_dates = _dates(User)
    post_dates = _dates(Post)
    comment_dates = _dates(Comment)
    invest_dates = _dates(Investment)
    thread_dates = _dates(ForumThread)
    like_rows = db.session.query(Post.created_at).join(
        likes_table, likes_table.c.post_id == Post.id
    ).filter(Post.created_at >= cutoff).all()
    # likes don't have their own date, approximate with post date
    # Instead, count total likes per bucket via posts created in range
    # Better: count distinct like events — but likes_table has no timestamp.
    # We'll track investments and follows instead as engagement proxies.
    follow_count_total = db.session.query(db.func.count()).select_from(followers_table).scalar() or 0

    # Build a unified timeline
    all_dates = sorted(set(
        [d.strftime(bucket_fmt) for d in user_dates] +
        [d.strftime(bucket_fmt) for d in post_dates] +
        [d.strftime(bucket_fmt) for d in comment_dates] +
        [d.strftime(bucket_fmt) for d in invest_dates] +
        [d.strftime(bucket_fmt) for d in thread_dates]
    ))

    if not all_dates:
        # Return a minimal "no data" image
        fig, ax = plt.subplots(figsize=(10, 4), dpi=100, facecolor="#1a1033")
        ax.set_facecolor("#0f0c29")
        ax.text(0.5, 0.5, "No data yet", ha="center", va="center", color="#888", fontsize=16, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return Response(buf.getvalue(), mimetype="image/png", headers={"Cache-Control": "no-cache"})

    def _cumulative_by_bucket(dates_list):
        counts = {}
        for d in dates_list:
            key = d.strftime(bucket_fmt)
            counts[key] = counts.get(key, 0) + 1
        cumulative = []
        total = 0
        for bucket in all_dates:
            total += counts.get(bucket, 0)
            cumulative.append(total)
        return cumulative

    users_cum = _cumulative_by_bucket(user_dates)
    posts_cum = _cumulative_by_bucket(post_dates)
    comments_cum = _cumulative_by_bucket(comment_dates)
    investments_cum = _cumulative_by_bucket(invest_dates)
    threads_cum = _cumulative_by_bucket(thread_dates)

    date_index = pd.to_datetime(all_dates)

    tf_labels = {"1h": "1 Hour", "6h": "6 Hours", "1d": "24 Hours",
                 "7d": "7 Days", "30d": "30 Days", "all": "All Time"}

    # Choose axis formatting
    if tf in ("1h", "6h", "1d"):
        date_fmt = "%H:%M"
        if tf == "1h":
            locator = mdates.MinuteLocator(interval=10)
        elif tf == "6h":
            locator = mdates.HourLocator(interval=1)
        else:
            locator = mdates.HourLocator(interval=3)
    else:
        date_fmt = "%b %d"
        locator = mdates.DayLocator(interval=1 if tf == "7d" else 5)

    # --- Draw chart ---
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), dpi=100, facecolor="#1a1033",
                                   gridspec_kw={"height_ratios": [3, 2]})
    for ax in (ax1, ax2):
        ax.set_facecolor("#0f0c29")
        ax.tick_params(colors="#aaa", labelsize=9)
        ax.spines["bottom"].set_color("#333")
        ax.spines["left"].set_color("#333")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
        ax.xaxis.set_major_locator(locator)
        ax.grid(axis="y", color="#222", linewidth=0.5)

    # Top chart: Users & Posts (primary growth metrics)
    ax1.plot(date_index, users_cum, color="#38bdf8", linewidth=2.5, marker="o", markersize=3, label="Users")
    ax1.fill_between(date_index, users_cum, alpha=0.10, color="#38bdf8")
    ax1.plot(date_index, posts_cum, color="#ff4da6", linewidth=2, marker=".", markersize=3, label="Posts")
    ax1.fill_between(date_index, posts_cum, alpha=0.10, color="#ff4da6")
    ax1.set_ylabel("Cumulative Count", color="#aaa")
    ax1.legend(facecolor="#1a1033", edgecolor="#333", labelcolor="#ccc", loc="upper left")
    ax1.set_title(f"Site Growth — {tf_labels.get(tf, tf)}", color="#e0e0e0", fontsize=14, pad=10)

    # Bottom chart: Engagement metrics (comments, investments, forum threads)
    ax2.plot(date_index, comments_cum, color="#fbbf24", linewidth=2, marker=".", markersize=3, label="Comments")
    ax2.plot(date_index, investments_cum, color="#34d399", linewidth=2, marker=".", markersize=3, label="Investments")
    ax2.plot(date_index, threads_cum, color="#a855f7", linewidth=2, marker=".", markersize=3, label="Forum Threads")
    ax2.set_ylabel("Cumulative Count", color="#aaa")
    ax2.legend(facecolor="#1a1033", edgecolor="#333", labelcolor="#ccc", loc="upper left")

    fig.autofmt_xdate()
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png", headers={"Cache-Control": "no-cache"})


@app.route("/market/chart/<int:sissy_id>.png")
@login_required
def market_chart(sissy_id):
    sissy = User.query.get_or_404(sissy_id)
    if sissy.role != "sissy":
        abort(404)

    # Timeframe filtering
    tf = request.args.get("tf", "30d")
    tf_map = {
        "5m": timedelta(minutes=5),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "all": None,
    }
    window = tf_map.get(tf, timedelta(days=30))
    query = MarketSnapshot.query.filter_by(sissy_id=sissy.id)
    if window is not None:
        cutoff = datetime.utcnow() - window
        query = query.filter(MarketSnapshot.timestamp >= cutoff)
    snapshots = query.order_by(MarketSnapshot.timestamp).all()

    if not snapshots:
        abort(404)

    df = pd.DataFrame([{
        "date": s.timestamp,
        "market_value": s.market_value,
        "xp": s.xp,
        "posts": s.post_count,
        "tasks": s.task_count,
        "followers": s.follower_count,
    } for s in snapshots])
    df["date"] = pd.to_datetime(df["date"])

    # Choose axis formatting based on timeframe
    if tf in ("5m", "30m", "1h"):
        date_fmt = "%H:%M"
        locator = mdates.MinuteLocator(interval=1 if tf == "5m" else 5 if tf == "30m" else 10)
    elif tf in ("6h", "1d"):
        date_fmt = "%H:%M"
        locator = mdates.HourLocator(interval=1 if tf == "6h" else 3)
    else:
        date_fmt = "%b %d"
        locator = mdates.DayLocator(interval=1 if tf == "7d" else 5)

    tf_labels = {"5m": "5 Minutes", "30m": "30 Minutes", "1h": "1 Hour",
                 "6h": "6 Hours", "1d": "24 Hours", "7d": "7 Days",
                 "30d": "30 Days", "all": "All Time"}

    # Dark theme chart
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), dpi=100, facecolor="#1a1033")
    for ax in (ax1, ax2):
        ax.set_facecolor("#0f0c29")
        ax.tick_params(colors="#aaa")
        ax.spines["bottom"].set_color("#333")
        ax.spines["left"].set_color("#333")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
        ax.xaxis.set_major_locator(locator)

    # Top chart: Market value + XP
    ax1.plot(df["date"], df["market_value"], color="#ff4da6", linewidth=2, marker="o", markersize=3, label="Market Value")
    ax1.fill_between(df["date"], df["market_value"], alpha=0.15, color="#ff4da6")
    ax1.plot(df["date"], df["xp"], color="#38bdf8", linewidth=1.5, linestyle="--", marker=".", markersize=3, label="XP")
    ax1.set_ylabel("Value", color="#aaa")
    ax1.legend(facecolor="#1a1033", edgecolor="#333", labelcolor="#ccc")
    ax1.set_title(f"{sissy.username} — {tf_labels.get(tf, tf)}", color="#e0e0e0", fontsize=14, pad=10)

    # Bottom chart: Posts, Tasks & Followers
    bar_width = 0.02 if tf in ("5m", "30m", "1h", "6h", "1d") else 0.8
    ax2.bar(df["date"], df["posts"], color="#a855f7", alpha=0.7, width=bar_width, label="Posts")
    ax2.bar(df["date"], df["tasks"], bottom=df["posts"], color="#fbbf24", alpha=0.7, width=bar_width, label="Tasks")
    ax2.plot(df["date"], df["followers"], color="#34d399", linewidth=2, marker="o", markersize=4, label="Followers")
    ax2.set_ylabel("Count", color="#aaa")
    ax2.legend(facecolor="#1a1033", edgecolor="#333", labelcolor="#ccc")

    fig.autofmt_xdate()
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Bot Simulation Toolkit
# ---------------------------------------------------------------------------
# Scan media banks for bot posts (photos, videos, and gifs)
_BOT_PHOTO_DIR = os.path.join(os.path.dirname(__file__), "static", "bot_post_photo_bank")
_BOT_VIDEO_DIR = os.path.join(os.path.dirname(__file__), "static", "assets", "bot_post_video_bank")
_BOT_GIF_DIR = os.path.join(os.path.dirname(__file__), "static", "assets", "bot_post_gif_bank")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".webm"}

def _scan_bot_media():
    """Return list of URL paths for available bot media files."""
    media = []
    if os.path.isdir(_BOT_PHOTO_DIR):
        for f in os.listdir(_BOT_PHOTO_DIR):
            if os.path.splitext(f)[1].lower() in _IMAGE_EXTS:
                media.append(f"/static/bot_post_photo_bank/{f}")
    if os.path.isdir(_BOT_VIDEO_DIR):
        for f in os.listdir(_BOT_VIDEO_DIR):
            if os.path.splitext(f)[1].lower() in _VIDEO_EXTS:
                media.append(f"/static/assets/bot_post_video_bank/{f}")
    if os.path.isdir(_BOT_GIF_DIR):
        for f in os.listdir(_BOT_GIF_DIR):
            if os.path.splitext(f)[1].lower() == ".gif":
                media.append(f"/static/assets/bot_post_gif_bank/{f}")
    return media



# ---------------------------------------------------------------------------
# Bot Personality & Content Generation System (Faker-powered)
# ---------------------------------------------------------------------------

# Archetypes define how each bot behaves: posting frequency, engagement style,
# social tendencies, and content flavour.
BOT_ARCHETYPES = {
    "influencer": {
        "weight": 15,
        "post_rate": (3, 6), "comment_rate": (4, 8), "like_rate": (6, 12),
        "follow_rate": (5, 10), "invest_rate": (0, 1), "gift_rate": (1, 3),
        "task_rate": (0, 1), "forum_rate": (1, 2),
        "bio_templates": [
            "✨ {catchphrase} | Content creator & dreamer 💫",
            "🌸 Living my best life | {city} | she/her 💕",
            "👑 Full-time slaying | DM for collabs | {emoji}",
            "🦋 {first_name}'s world | {catchphrase}",
            "💖 Spreading positivity one post at a time | {city}",
        ],
        "post_styles": ["glamour", "lifestyle", "motivation"],
        "emoji_density": "high",
    },
    "newbie": {
        "weight": 30,
        "post_rate": (0, 2), "comment_rate": (1, 3), "like_rate": (3, 8),
        "follow_rate": (4, 8), "invest_rate": (0, 0), "gift_rate": (0, 1),
        "task_rate": (1, 3), "forum_rate": (0, 2),
        "bio_templates": [
            "Just got here! Still figuring things out 😅",
            "New to the community 🌱 be gentle!",
            "Hi I'm {first_name}! {city} based 🏠",
            "Day {day_count} of my journey ✨",
            "{first_name} | learning & growing every day 🌿",
        ],
        "post_styles": ["casual", "question"],
        "emoji_density": "low",
    },
    "veteran": {
        "weight": 10,
        "post_rate": (2, 4), "comment_rate": (3, 6), "like_rate": (4, 10),
        "follow_rate": (2, 5), "invest_rate": (2, 5), "gift_rate": (2, 4),
        "task_rate": (0, 1), "forum_rate": (2, 4),
        "bio_templates": [
            "🏆 Been here since day one | Level {level}+ | {catchphrase}",
            "OG member 💎 | Mentor & investor | {city}",
            "📈 Market guru | Portfolio: diverse | {emoji}",
            "{first_name} | Community pillar since forever 🌟",
            "Veteran vibes 👑 | Ask me anything | {city}",
        ],
        "post_styles": ["advice", "motivation", "showcase"],
        "emoji_density": "medium",
    },
    "lurker": {
        "weight": 25,
        "post_rate": (0, 1), "comment_rate": (0, 2), "like_rate": (5, 15),
        "follow_rate": (3, 7), "invest_rate": (1, 3), "gift_rate": (0, 1),
        "task_rate": (1, 2), "forum_rate": (0, 1),
        "bio_templates": [
            "👀",
            "...",
            "{first_name}",
            "🤫 Quiet but watching everything",
            "{city} | {emoji}",
        ],
        "post_styles": ["casual"],
        "emoji_density": "none",
    },
    "socialite": {
        "weight": 20,
        "post_rate": (1, 3), "comment_rate": (5, 12), "like_rate": (8, 20),
        "follow_rate": (6, 15), "invest_rate": (1, 2), "gift_rate": (3, 6),
        "task_rate": (0, 1), "forum_rate": (2, 5),
        "bio_templates": [
            "💬 I never shut up lol | {city} | {catchphrase}",
            "🎉 Here for the vibes & the people! | {emoji}",
            "Social butterfly 🦋 | {first_name} | DM me anything",
            "❤️ Spreading love everywhere I go | {city}",
            "Addicted to this community tbh 😍 | {catchphrase}",
        ],
        "post_styles": ["social", "lifestyle", "casual"],
        "emoji_density": "high",
    },
}

# Master archetypes
BOT_MASTER_ARCHETYPES = {
    "strict": {
        "weight": 30,
        "task_rate": (3, 7), "invest_rate": (2, 5), "follow_rate": (2, 4),
        "gift_rate": (1, 2), "like_rate": (2, 5), "comment_rate": (1, 3), "forum_rate": (1, 3),
        "bio_templates": [
            "👊 No excuses. Results only. | {city}",
            "🎯 High standards, high rewards | {catchphrase}",
            "Discipline is freedom. | {first_name} | {emoji}",
            "💪 Pushing sissies to greatness since day 1",
            "Results-driven. Don't waste my time. | {city}",
        ],
        "task_styles": ["discipline", "routine"],
    },
    "supportive": {
        "weight": 40,
        "task_rate": (2, 5), "invest_rate": (3, 6), "follow_rate": (4, 8),
        "gift_rate": (3, 6), "like_rate": (5, 10), "comment_rate": (3, 7), "forum_rate": (2, 4),
        "bio_templates": [
            "💖 Here to lift everyone up | {city}",
            "🌟 Your biggest cheerleader | {catchphrase}",
            "Nurturing greatness one task at a time | {emoji}",
            "{first_name} | Empowerment through guidance 🌸",
            "Supporting your journey every step of the way 💕",
        ],
        "task_styles": ["selfcare", "creative"],
    },
    "investor": {
        "weight": 30,
        "task_rate": (1, 2), "invest_rate": (5, 10), "follow_rate": (3, 6),
        "gift_rate": (1, 3), "like_rate": (3, 6), "comment_rate": (2, 4), "forum_rate": (1, 2),
        "bio_templates": [
            "📈 Market whale | Portfolio diversified | {city}",
            "💰 Smart money moves only | {catchphrase}",
            "Investing in potential since day 1 | {emoji}",
            "{first_name} | Venture capitalist of goodgurl.gg 🦈",
            "Show me a rising star and I'll show you my wallet 💎",
        ],
        "task_styles": ["challenge"],
    },
}

# Content templates by style — much richer than static arrays
_POST_TEMPLATES = {
    "glamour": [
        ("New look, who dis? 💅", "Spent all {day} perfecting this look. {feeling} about the result! #{tag1} #{tag2}"),
        ("Serving {mood} today", "The mirror was my best friend this morning. {quote} #{tag1} #{tag2} #{tag3}"),
        ("Glow up check ✨", "Comparison is the thief of joy but honestly? I'm proud of how far I've come. #{tag1} #{tag2}"),
        ("{season} transformation", "This {season} is hitting different. New vibes, new energy, new me. #{tag1} #{tag2}"),
    ],
    "lifestyle": [
        ("{day} vibes", "Just {activity} and feeling {feeling}. Life is good when you stop overthinking it. #{tag1} #{tag2}"),
        ("Morning routine check ☀️", "Started the day with {activity}. How are you all spending your {day}? #{tag1} #{tag2}"),
        ("Self-care {day}", "Reminder that taking care of yourself isn't selfish, it's necessary. {quote} #{tag1} #{tag2}"),
        ("Weekend energy 🌈", "No plans, no stress. Just vibes and {activity}. #{tag1}"),
    ],
    "motivation": [
        ("Day {number} — still going 💪", "{quote} Every day I show up is a win. #{tag1} #{tag2} #{tag3}"),
        ("You can do hard things", "To everyone struggling right now: {encouragement}. #{tag1} #{tag2}"),
        ("Progress, not perfection", "I used to compare myself to everyone else. Now I only compete with yesterday's me. #{tag1} #{tag2}"),
        ("Keep pushing 🔥", "{encouragement}. That's it. That's the post. #{tag1}"),
    ],
    "casual": [
        ("{mood} kinda {day}", "Honestly just {activity} and scrolling. Anyone else having a {mood} day? #{tag1}"),
        ("Random thought", "{thought}. Anyway how's everyone doing? #{tag1} #{tag2}"),
        ("lol oops", "Tried {activity} and it did NOT go as planned 😂 #{tag1}"),
        ("hi 👋", "Just popping in to say hi! Haven't posted in a while. {feeling} to be back. #{tag1}"),
    ],
    "question": [
        ("Need advice!", "So I've been {activity} and I'm not sure if I'm doing it right. Any tips? #{tag1} #{tag2}"),
        ("How do you all...", "Genuine question: how do you stay {mood} when things get tough? #{tag1}"),
        ("Recommendations?", "Looking for suggestions on {topic}. Drop your faves below! #{tag1} #{tag2}"),
    ],
    "advice": [
        ("Pro tip for new members", "{advice}. Trust me, I learned this the hard way. #{tag1} #{tag2} #{tag3}"),
        ("Things I wish I knew earlier", "After being here for a while, here's my biggest takeaway: {advice}. #{tag1} #{tag2}"),
        ("{topic} guide", "A lot of people ask me about this so here goes: {advice}. #{tag1} #{tag2}"),
    ],
    "showcase": [
        ("Level {number} unlocked! 🏆", "Officially hit level {number}. The grind was real but so worth it. #{tag1} #{tag2}"),
        ("Portfolio update 📈", "My investments are looking {mood}. If you're not on the market yet, what are you waiting for? #{tag1}"),
        ("Achievement unlocked", "Just earned a new achievement! The community here is {feeling}. #{tag1} #{tag2}"),
    ],
    "social": [
        ("Shoutout to {mention}!", "Seriously go follow them if you haven't already. {encouragement}! #{tag1} #{tag2}"),
        ("This community though 😭❤️", "I'm getting emotional. You all are {feeling} and I don't say that enough. #{tag1}"),
        ("Who's online? 👀", "Drop a {emoji} if you're scrolling right now! Let's interact! #{tag1}"),
    ],
}

_COMMENT_TEMPLATES = {
    "enthusiastic": [
        "OBSESSED with this 😍🔥", "YESSS {author}!! 💖", "This is EVERYTHING 🙌",
        "I literally screamed!! {emoji}", "Ok you ATE this up 👑💅",
        "Saving this forever 📌✨", "The energy in this post!! 🔥🔥",
    ],
    "supportive": [
        "So proud of you! 💕", "You're doing amazing, keep going! ✨",
        "This made my whole day better 🌸", "Love seeing your growth {emoji}",
        "You inspire me every single day 💖", "Keep shining! 🌟",
    ],
    "casual": [
        "nice 👍", "love it", "same tbh 😅", "this ^^",
        "real talk", "goals", "mood 💯", "big vibes",
    ],
    "curious": [
        "How did you do this?? 👀", "Wait what's your routine??",
        "Need a tutorial ASAP! 📝", "Ok spill the secrets! 🍵",
        "Where did you get that?? 😍",
    ],
    "minimal": [
        "💖", "🔥", "👑", "✨", "😍", "💕", "🙌", "💯", "❤️", "🦋",
    ],
}

_TASK_TEMPLATES = {
    "discipline": [
        ("Write a 200-word reflection", "Sit down, no distractions, and write honestly about your progress this week. No fluff."),
        ("Complete a 30-min workout", "Any exercise counts — just push through the full 30 minutes without stopping."),
        ("Wake up before 7am for 3 days", "Screenshot your alarm as proof. Consistency builds character."),
        ("No social media for 24 hours", "Log your feelings throughout the day. What did you notice?"),
        ("Practice posture for 1 hour", "Set a timer. Shoulders back, chin up. Take progress photos."),
    ],
    "selfcare": [
        ("Do a full skincare routine", "Cleanser, toner, moisturizer — the works! Take before & after selfies. 🧴"),
        ("Take a relaxing bath", "Light some candles, put on music, and unwind. You deserve it. 🛁"),
        ("Journal about 3 things you love about yourself", "Be specific and genuine. This is about self-appreciation. 📝"),
        ("Try a new hairstyle", "Watch a tutorial, get creative! Post the results! 💇‍♀️"),
        ("Meditate for 15 minutes", "Use an app or just sit quietly. Notice your breath. 🧘"),
    ],
    "creative": [
        ("Create a mood board", "Use magazines, Pinterest, or apps. Theme: your ideal version of yourself. 🎨"),
        ("Style 3 outfits from your wardrobe", "Mix & match things you've never paired before. Photo each look!"),
        ("Record a 60-second intro video", "Introduce yourself to the community! Be authentic. 🎬"),
        ("Write a poem about your journey", "It doesn't have to rhyme. Just be honest. ✍️"),
        ("Design your dream avatar look", "Describe or sketch it. What does your ideal self look like?"),
    ],
    "challenge": [
        ("Get 5 likes on a single post", "Create something the community will love. Quality over quantity!"),
        ("Comment on 10 different posts", "Genuine comments only — no copy-paste. Connect with people!"),
        ("Earn 50 XP today", "Mix of posts, tasks, and engagement. Show me what you've got!"),
        ("Follow 5 new members and introduce yourself", "Send a comment or DM. Build your network!"),
        ("Invest in 3 different sissies", "Research the market, pick wisely. Diversify your portfolio!"),
    ],
}

_FORUM_TEMPLATES = {
    "discussion": [
        ("What's your unpopular opinion about {topic}?", "I'll go first: {thought}. Don't come for me 😅 #{tag1} #{tag2}"),
        ("Weekly check-in: how's everyone doing?", "Drop your wins, struggles, anything. This is a safe space. #{tag1}"),
        ("Let's talk about {topic}", "I've been thinking about this a lot lately. {thought}. What do you all think? #{tag1} #{tag2}"),
    ],
    "advice": [
        ("Best tips for {topic}?", "I'm looking to improve in this area. What worked for you? #{tag1} #{tag2}"),
        ("{topic} — mistakes to avoid", "I made some early mistakes and want to help others skip them. {advice}. #{tag1}"),
        ("Mentorship thread: ask me anything about {topic}", "I've been around for a while and want to give back. Fire away! #{tag1}"),
    ],
    "fun": [
        ("If you could change one thing about the platform...", "What would it be? I'll start: {thought}. #{tag1}"),
        ("Caption contest! 📸", "Best caption for this week's top post gets bragging rights. Go! #{tag1}"),
        ("Rate my profile! 🌟", "Be honest but kind! I'll return the favor for anyone who comments. #{tag1}"),
    ],
}

_FORUM_REPLY_TEMPLATES = [
    "Great point! I completely agree. {emoji}",
    "Hmm interesting take. I think {thought}.",
    "This! So much this! {emoji}{emoji}",
    "I had the exact same experience! {emoji}",
    "Thanks for sharing! Really helpful. {emoji}",
    "Adding on to this: {thought}.",
    "Disagree slightly — I think {thought}. But respect your view!",
    "Needed to hear this today. {emoji}",
    "Been thinking about this too. {thought}.",
    "10/10 thread. Following for more! {emoji}",
    "New here but this resonated with me! {emoji}",
    "Can confirm, this works! {emoji}",
]

_FILL_WORDS = {
    "feeling": ["amazing", "incredible", "grateful", "blessed", "overwhelmed", "happy",
                "motivated", "confident", "proud", "excited", "thrilled", "alive"],
    "mood": ["chill", "hyped", "cozy", "productive", "energetic", "reflective",
             "creative", "fierce", "calm", "chaotic", "dreamy", "bold"],
    "activity": ["journaling", "doing skincare", "trying on outfits", "working out",
                 "meditating", "reading", "cooking", "stretching", "decluttering",
                 "experimenting with makeup", "practicing my walk", "organizing my space"],
    "day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "season": ["Spring", "Summer", "Fall", "Winter"],
    "topic": ["self-care", "confidence", "style", "the market", "fitness", "beauty",
              "community building", "daily routines", "motivation", "investing"],
    "encouragement": ["You are stronger than you think", "Every step forward counts",
                      "Your only competition is yesterday's you", "The comeback is always stronger",
                      "Don't let a bad day make you feel like you have a bad life",
                      "You're not behind — you're on your own timeline",
                      "Believe in yourself even when nobody else does"],
    "quote": ["'Be yourself; everyone else is already taken'",
              "'The only way out is through'",
              "'She believed she could, so she did'",
              "'Progress, not perfection'",
              "'You are enough, exactly as you are'",
              "'Difficult roads lead to beautiful destinations'"],
    "advice": ["Focus on consistency over intensity",
               "Don't compare chapter 1 to someone else's chapter 20",
               "Invest in yourself before investing in others",
               "Small daily improvements are the key to staggering long-term results",
               "The best time to start was yesterday. The second best time is now"],
    "thought": ["sometimes the smallest step is the biggest win",
                "we really underestimate how much energy self-doubt takes",
                "community support changes everything",
                "consistency beats motivation every time",
                "being vulnerable is actually the bravest thing you can do",
                "not every day has to be productive to be meaningful"],
    "catchphrase": ["Living loud", "Unapologetically me", "Main character energy",
                    "Glow from within", "Chaos with a crown", "Soft but strong"],
    "emoji": ["✨", "💖", "🔥", "👑", "🌸", "🦋", "💕", "🌟", "💫", "🎀", "🌈", "💎", "🌺"],
    "tag1": ["selfcare", "progress", "motivation", "goals", "vibes", "growth",
             "community", "confidence", "journey", "lifestyle"],
    "tag2": ["daily", "challenge", "beauty", "fitness", "gratitude", "wellness",
             "positivity", "selflove", "transformation", "mindset"],
    "tag3": ["inspiration", "grind", "hustle", "aesthetic", "glowup", "sissy",
             "queen", "slay", "energy", "blessed"],
}


def _fill_template(template, extra_vars=None):
    """Fill a template string with random contextual words."""
    context = {}
    for key, values in _FILL_WORDS.items():
        context[key] = random.choice(values)
    context["number"] = random.randint(3, 50)
    context["day_count"] = random.randint(1, 120)
    context["level"] = random.randint(5, 30)
    context["first_name"] = _fake.first_name_female()
    context["city"] = _fake.city()
    if extra_vars:
        context.update(extra_vars)
    # Handle {mention} separately — will be replaced at call site if needed
    if "{mention}" in template:
        context["mention"] = context.get("mention", "someone amazing")
    try:
        return template.format_map(context)
    except (KeyError, IndexError):
        return template


def _pick_archetype(archetypes_dict):
    """Weighted random selection from archetype dict."""
    names = list(archetypes_dict.keys())
    weights = [archetypes_dict[n]["weight"] for n in names]
    return random.choices(names, weights=weights, k=1)[0]


def _generate_username(role):
    """Generate a realistic-sounding username."""
    styles = [
        lambda: f"{_fake.first_name_female().lower()}_{_fake.word()}",
        lambda: f"{_fake.first_name_female().lower()}{random.randint(10,99)}",
        lambda: f"xx_{_fake.first_name_female().lower()}_xx",
        lambda: f"{_fake.word()}{_fake.first_name_female().lower()}",
        lambda: f"{_fake.first_name_female().lower()}.{_fake.last_name().lower()}",
        lambda: f"the_{_fake.first_name_female().lower()}",
        lambda: f"{_fake.first_name_female().lower()}_{random.choice(['xo','bb','qt','gg','vip'])}",
    ]
    if role == "master":
        styles += [
            lambda: f"Dom_{_fake.first_name().lower()}",
            lambda: f"Master{_fake.last_name()}",
            lambda: f"{_fake.first_name().lower()}_authority",
        ]
    for _ in range(20):
        username = random.choice(styles)()
        username = re.sub(r'[^a-zA-Z0-9_.]', '', username)[:20]
        if len(username) >= 3 and not User.query.filter_by(username=username).first():
            return username
    # Fallback to guaranteed unique
    tag = secrets.token_hex(3)
    prefix = "bot_s_" if role == "sissy" else "bot_m_"
    return f"{prefix}{tag}"


def _generate_bio(archetype_dict, archetype_name):
    """Generate a bio from archetype templates filled with Faker data."""
    templates = archetype_dict[archetype_name]["bio_templates"]
    return _fill_template(random.choice(templates))


def _generate_post(post_styles):
    """Generate a post title+body from style-specific templates."""
    style = random.choice(post_styles)
    templates = _POST_TEMPLATES.get(style, _POST_TEMPLATES["casual"])
    title_tpl, body_tpl = random.choice(templates)
    return _fill_template(title_tpl), _fill_template(body_tpl)


def _generate_comment(archetype_name):
    """Pick a comment style based on archetype personality."""
    style_map = {
        "influencer": ["enthusiastic", "enthusiastic", "supportive"],
        "newbie": ["supportive", "casual", "curious"],
        "veteran": ["supportive", "casual", "curious"],
        "lurker": ["minimal", "minimal", "casual"],
        "socialite": ["enthusiastic", "supportive", "casual"],
        "strict": ["casual", "casual"],
        "supportive": ["supportive", "enthusiastic", "supportive"],
        "investor": ["casual", "casual", "curious"],
    }
    styles = style_map.get(archetype_name, ["casual"])
    style = random.choice(styles)
    templates = _COMMENT_TEMPLATES.get(style, _COMMENT_TEMPLATES["casual"])
    return _fill_template(random.choice(templates))


def _generate_task(master_archetype):
    """Generate a task from archetype-appropriate templates."""
    arch = BOT_MASTER_ARCHETYPES[master_archetype]
    style = random.choice(arch["task_styles"])
    templates = _TASK_TEMPLATES.get(style, _TASK_TEMPLATES["creative"])
    title, desc = random.choice(templates)
    return title, desc


def _generate_forum(style=None):
    """Generate a forum thread title + body."""
    if not style:
        style = random.choice(list(_FORUM_TEMPLATES.keys()))
    templates = _FORUM_TEMPLATES.get(style, _FORUM_TEMPLATES["discussion"])
    title_tpl, body_tpl = random.choice(templates)
    return _fill_template(title_tpl), _fill_template(body_tpl)


def _generate_forum_reply():
    """Generate a forum reply."""
    return _fill_template(random.choice(_FORUM_REPLY_TEMPLATES))


# ---------------------------------------------------------------------------
# Social Graph Engine — builds realistic relationship clusters
# ---------------------------------------------------------------------------

def _build_social_graph(all_bots, sissy_bots, master_bots):
    """Build weighted affinity scores between bots for realistic engagement patterns.
    Returns a dict: {bot_id: [(target_id, weight), ...]} sorted by weight desc.
    Popular bots (influencers) attract more engagement from others.
    """
    graph = {}
    # Pre-compute popularity scores (influencers & veterans get a boost)
    pop_score = {}
    for bot in all_bots:
        arch = getattr(bot, '_sim_archetype', 'newbie')
        base = {"influencer": 5, "veteran": 4, "socialite": 3, "newbie": 1, "lurker": 1,
                "strict": 3, "supportive": 4, "investor": 2}.get(arch, 2)
        pop_score[bot.id] = base + random.uniform(0, 2)

    for bot in all_bots:
        targets = []
        for other in all_bots:
            if other.id == bot.id:
                continue
            # Base affinity from target's popularity
            w = pop_score.get(other.id, 1)
            # Social clustering: same archetype type bonds more
            if getattr(bot, '_sim_archetype', '') == getattr(other, '_sim_archetype', ''):
                w *= 1.5
            # Cross-role affinity (masters like sissies and vice versa)
            if bot.role != other.role:
                w *= 1.3
            # Random jitter for variety
            w *= random.uniform(0.5, 1.5)
            targets.append((other.id, w))
        # Normalize to probabilities
        total = sum(t[1] for t in targets) or 1
        targets = [(tid, tw / total) for tid, tw in targets]
        targets.sort(key=lambda x: -x[1])
        graph[bot.id] = targets
    return graph


def _pick_target(graph, bot_id, exclude_ids=None):
    """Pick a weighted-random target from the social graph."""
    targets = graph.get(bot_id, [])
    if exclude_ids:
        targets = [(tid, w) for tid, w in targets if tid not in exclude_ids]
    if not targets:
        return None
    ids, weights = zip(*targets)
    return random.choices(ids, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# Core Simulation (instant mode)
# ---------------------------------------------------------------------------

def run_simulation(num_sissies, num_masters, actions_min, actions_max):
    """Create bot accounts with personalities and simulate realistic platform activity.

    Features:
    - Faker-generated usernames, bios, and content
    - 5 sissy archetypes + 3 master archetypes with different behavior patterns
    - Social graph determines who interacts with whom (popularity weighting)
    - Engagement cascading: popular posts attract more likes/comments
    - Varied content styles per archetype
    """
    summary = {
        "sissies_created": 0, "masters_created": 0,
        "posts": 0, "likes": 0, "comments": 0,
        "tasks_created": 0, "tasks_completed": 0,
        "investments": 0, "follows": 0, "gifts": 0,
        "forum_threads": 0, "forum_replies": 0,
    }

    now = datetime.utcnow()

    # --- Pre-cache the XP multiplier once ---
    active_mults = Multiplier.query.filter(
        Multiplier.is_active == True,
        db.or_(Multiplier.starts_at == None, Multiplier.starts_at <= now),
        db.or_(Multiplier.expires_at == None, Multiplier.expires_at >= now),
    ).all()
    cached_mult = 1.0
    if active_mults:
        mult_values = [m.multiplier for m in active_mults]
        if MULTIPLIER_STACK_STRATEGY == "compound":
            for v in mult_values:
                cached_mult *= v
        elif MULTIPLIER_STACK_STRATEGY == "additive":
            cached_mult = sum(mult_values)
        elif MULTIPLIER_STACK_STRATEGY == "highest":
            cached_mult = max(mult_values)

    bot_password_hash = generate_password_hash("bot-no-login")
    tag_cache = {t.name: t for t in Tag.query.all()}

    def get_or_create_tag(name):
        if name not in tag_cache:
            t = Tag(name=name)
            db.session.add(t)
            tag_cache[name] = t
        return tag_cache[name]

    def fast_add_points(user, amount, reason=""):
        if user.role != "sissy":
            return
        final_amount = int(amount * cached_mult)
        at_cap = user.level >= LEVEL_CAP
        if at_cap and not LEVEL_CAP_POINTS_CONTINUE:
            final_amount = 0
        old_level = user.level
        user.xp += final_amount
        db.session.add(XPAudit(
            user_id=user.id, amount=final_amount, original_amount=amount,
            multiplier_applied=round(cached_mult, 2), reason=reason, audit_type="add",
        ))
        if not at_cap:
            new_level = user._calculate_level()
            if new_level > old_level:
                user.level = min(new_level, LEVEL_CAP)

    liked_pairs = set()
    follow_pairs = set()
    pending_likes = []
    pending_follows = []
    pending_notifications = []
    snapshot_needed = set()

    bot_sissies = []
    bot_masters = []

    # --- Phase 0: Create bot accounts with personalities ---
    for i in range(num_sissies):
        archetype = _pick_archetype(BOT_ARCHETYPES)
        username = _generate_username("sissy")
        bio = _generate_bio(BOT_ARCHETYPES, archetype)
        user = User(
            username=username, email=f"{username}@bot.gg", role="sissy", is_bot=True,
            bio=bio, avatar_url=generate_avatar(username),
        )
        user.password_hash = bot_password_hash
        user._sim_archetype = archetype  # runtime attribute for sim logic
        db.session.add(user)
        bot_sissies.append(user)
        summary["sissies_created"] += 1

    for i in range(num_masters):
        archetype = _pick_archetype(BOT_MASTER_ARCHETYPES)
        username = _generate_username("master")
        bio = _generate_bio(BOT_MASTER_ARCHETYPES, archetype)
        user = User(
            username=username, email=f"{username}@bot.gg", role="master", is_bot=True,
            coins=100, bio=bio, avatar_url=generate_avatar(username),
        )
        user.password_hash = bot_password_hash
        user._sim_archetype = archetype
        db.session.add(user)
        bot_masters.append(user)
        summary["masters_created"] += 1

    db.session.flush()
    all_bots = bot_sissies + bot_masters

    # Build social graph for weighted interactions
    social_graph = _build_social_graph(all_bots, bot_sissies, bot_masters)

    all_sissies = User.query.filter_by(role="sissy").all()
    all_users = User.query.all()
    sissy_by_id = {u.id: u for u in all_sissies}
    user_by_id = {u.id: u for u in all_users}

    # --- Phase 1: Content creation ---
    bot_media_pool = _scan_bot_media()
    all_categories = ForumCategory.query.all()
    created_threads = []

    # Sissies create posts based on archetype post_rate
    for bot in bot_sissies:
        if not bot_media_pool:
            break
        arch = BOT_ARCHETYPES[bot._sim_archetype]
        num_posts = random.randint(*arch["post_rate"])
        for _ in range(num_posts):
            title, body = _generate_post(arch["post_styles"])
            media = random.choice(bot_media_pool)
            post = Post(author_id=bot.id, title=title, body=body, media_url=media)
            db.session.add(post)
            db.session.flush()
            for tag_name in extract_hashtags(body + " " + title):
                post.tags.append(get_or_create_tag(tag_name))
            fast_add_points(bot, POST_REWARD_XP, reason=f"Bot post: {title[:40]}")
            summary["posts"] += 1
        snapshot_needed.add(bot.id)

    # Forum threads — archetype determines rate
    if all_categories:
        for bot in all_bots:
            arch = (BOT_ARCHETYPES if bot.role == "sissy" else BOT_MASTER_ARCHETYPES).get(
                getattr(bot, '_sim_archetype', ''), {})
            num_threads = random.randint(*arch.get("forum_rate", (0, 1)))
            for _ in range(num_threads):
                cat = random.choice(all_categories)
                title, body = _generate_forum()
                thread = ForumThread(category_id=cat.id, author_id=bot.id, title=title, body=body)
                db.session.add(thread)
                db.session.flush()
                for tag_name in extract_hashtags(title + " " + body):
                    thread.tags.append(get_or_create_tag(tag_name))
                if bot.role == "sissy":
                    fast_add_points(bot, 5, reason=f"Bot forum: {title[:30]}")
                    snapshot_needed.add(bot.id)
                created_threads.append(thread)
                summary["forum_threads"] += 1

        # Replies to threads
        for bot in all_bots:
            if not created_threads:
                break
            for _ in range(random.randint(0, 3)):
                thread = random.choice(created_threads)
                reply_body = _generate_forum_reply()
                reply = ForumReply(thread_id=thread.id, author_id=bot.id, body=reply_body)
                thread.last_reply_at = datetime.utcnow()
                db.session.add(reply)
                for tag_name in extract_hashtags(reply_body):
                    existing = {t.name for t in thread.tags}
                    if tag_name not in existing:
                        thread.tags.append(get_or_create_tag(tag_name))
                if bot.role == "sissy":
                    fast_add_points(bot, 2, reason=f"Bot reply in: {thread.title[:30]}")
                    snapshot_needed.add(bot.id)
                summary["forum_replies"] += 1

    # Masters create tasks based on archetype
    for bot in bot_masters:
        arch = BOT_MASTER_ARCHETYPES[bot._sim_archetype]
        num_tasks = random.randint(*arch["task_rate"])
        for _ in range(num_tasks):
            title, desc = _generate_task(bot._sim_archetype)
            reward_coins = random.choice([5, 10, 15, 20, 25, 30, 40, 50])
            if bot.coins >= reward_coins:
                task = Task(
                    creator_id=bot.id, title=title, description=desc,
                    reward_coins=reward_coins, reward_xp=TASK_REWARD_XP, status="open",
                )
                bot.coins -= reward_coins
                db.session.add(task)
                db.session.add(Transaction(user_id=bot.id, amount=-reward_coins, description=f"Task escrow: {title[:30]}"))
                summary["tasks_created"] += 1

    db.session.flush()

    # --- Pre-interaction: list eligible sissies on market ---
    for bot in bot_sissies:
        if bot.level >= 5:
            bot.listed_on_market = True
    db.session.flush()

    # --- Phase 2: Interactions (socially weighted) ---
    all_posts = Post.query.all()
    open_tasks = list(Task.query.filter_by(status="open").all())
    market_sissies = [s for s in all_sissies if s.listed_on_market and s.level >= 5]

    # Build post popularity scores for engagement cascading
    post_pop = {}
    for p in all_posts:
        post_pop[p.id] = random.uniform(0.5, 2.0)  # base random appeal

    def _pick_popular_post(posts_list):
        """Pick a post weighted by popularity — popular posts get more engagement."""
        if not posts_list:
            return None
        weights = [post_pop.get(p.id, 1.0) for p in posts_list]
        return random.choices(posts_list, weights=weights, k=1)[0]

    # Each bot performs actions based on their archetype rates
    for bot in all_bots:
        arch = (BOT_ARCHETYPES if bot.role == "sissy" else BOT_MASTER_ARCHETYPES).get(
            getattr(bot, '_sim_archetype', ''), {})

        # Likes
        num_likes = random.randint(*arch.get("like_rate", (1, 5)))
        for _ in range(num_likes):
            post = _pick_popular_post(all_posts)
            if post:
                pair = (bot.id, post.id)
                if pair not in liked_pairs:
                    liked_pairs.add(pair)
                    pending_likes.append({"user_id": bot.id, "post_id": post.id})
                    post_pop[post.id] = post_pop.get(post.id, 1) + 0.3  # cascade
                    if post.author_id != bot.id:
                        pending_notifications.append(dict(
                            user_id=post.author_id, type="like",
                            message=f"{bot.username} liked your post \"{post.title[:30]}\"",
                            link=f"/post/{post.id}",
                        ))
                    summary["likes"] += 1

        # Comments
        num_comments = random.randint(*arch.get("comment_rate", (0, 2)))
        for _ in range(num_comments):
            post = _pick_popular_post(all_posts)
            if post:
                comment_text = _generate_comment(getattr(bot, '_sim_archetype', 'casual'))
                db.session.add(Comment(post_id=post.id, author_id=bot.id, body=comment_text))
                post_pop[post.id] = post_pop.get(post.id, 1) + 0.5  # cascade
                if post.author_id != bot.id:
                    pending_notifications.append(dict(
                        user_id=post.author_id, type="comment",
                        message=f"{bot.username} commented on \"{post.title[:30]}\"",
                        link=f"/post/{post.id}",
                    ))
                if bot.role == "sissy":
                    fast_add_points(bot, 3, reason=f"Bot comment on post #{post.id}")
                    snapshot_needed.add(bot.id)
                summary["comments"] += 1

        # Follows (socially weighted)
        num_follows = random.randint(*arch.get("follow_rate", (1, 3)))
        already_followed = set()
        for _ in range(num_follows):
            target_id = _pick_target(social_graph, bot.id, exclude_ids=already_followed)
            if target_id:
                pair = (bot.id, target_id)
                if pair not in follow_pairs:
                    follow_pairs.add(pair)
                    already_followed.add(target_id)
                    target = user_by_id.get(target_id)
                    if target:
                        pending_follows.append({"follower_id": bot.id, "followed_id": target_id})
                        pending_notifications.append(dict(
                            user_id=target_id, type="follow",
                            message=f"{bot.username} started following you!",
                            link=f"/profile/{bot.username}",
                        ))
                        if target_id in sissy_by_id:
                            snapshot_needed.add(target_id)
                        summary["follows"] += 1

        # Tasks (sissies accept)
        if bot.role == "sissy":
            num_task_accepts = random.randint(*arch.get("task_rate", (0, 1)))
            for _ in range(num_task_accepts):
                candidates = [t for t in open_tasks if t.creator_id != bot.id and t.assignee_id is None]
                if candidates:
                    task = random.choice(candidates)
                    task.assignee_id = bot.id
                    task.status = "completed"
                    task.completed_at = datetime.utcnow()
                    fast_add_points(bot, task.reward_xp, reason=f"Bot completed: {task.title[:30]}")
                    bot.coins += task.reward_coins
                    db.session.add(Transaction(user_id=bot.id, amount=task.reward_coins, description=f"Task reward: {task.title[:30]}"))
                    pending_notifications.append(dict(
                        user_id=task.creator_id, type="task",
                        message=f"{bot.username} completed your task \"{task.title[:30]}\"",
                        link="/tasks",
                    ))
                    snapshot_needed.add(bot.id)
                    open_tasks.remove(task)
                    summary["tasks_completed"] += 1

        # Investments (masters + veterans invest, weighted by social graph)
        if (bot.role == "master" or getattr(bot, '_sim_archetype', '') == "veteran") and market_sissies:
            num_invests = random.randint(*arch.get("invest_rate", (0, 2)))
            for _ in range(num_invests):
                target_id = _pick_target(social_graph, bot.id)
                sissy = sissy_by_id.get(target_id) if (target_id and sissy_by_id.get(target_id, None) and sissy_by_id[target_id].listed_on_market) else None
                if not sissy:
                    sissy = random.choice(market_sissies)
                if sissy and sissy.id != bot.id:
                    amount = random.choice([5, 10, 15, 20, 25, 30, 50])
                    if bot.coins >= amount:
                        inv = Investment(investor_id=bot.id, sissy_id=sissy.id, amount=amount, bought_value=sissy.xp)
                        bot.coins -= amount
                        db.session.add(inv)
                        db.session.add(Transaction(user_id=bot.id, amount=-amount, description=f"Invested in {sissy.username}"))
                        pending_notifications.append(dict(
                            user_id=sissy.id, type="invest",
                            message=f"{bot.username} invested {amount} coins in you!",
                            link="/market",
                        ))
                        snapshot_needed.add(sissy.id)
                        summary["investments"] += 1

        # Gifts (socially weighted)
        num_gifts = random.randint(*arch.get("gift_rate", (0, 1)))
        for _ in range(num_gifts):
            target_id = _pick_target(social_graph, bot.id)
            sissy = sissy_by_id.get(target_id) if target_id else None
            if not sissy:
                sissy = random.choice(all_sissies) if all_sissies else None
            if sissy and sissy.id != bot.id:
                amount = random.choice([1, 2, 3, 5, 10])
                if bot.coins >= amount:
                    bot.coins -= amount
                    db.session.add(Transaction(user_id=bot.id, amount=-amount, description=f"Gift to {sissy.username}"))
                    db.session.add(PendingGift(sender_id=bot.id, recipient_id=sissy.id, amount=amount))
                    pending_notifications.append(dict(
                        user_id=sissy.id, type="gift",
                        message=f"{bot.username} sent you {amount} coins!",
                        link="/gifts",
                    ))
                    summary["gifts"] += 1

    # --- Phase 3: Bulk inserts ---
    if pending_likes:
        db.session.execute(likes_table.insert(), pending_likes)
    if pending_follows:
        db.session.execute(followers_table.insert(), pending_follows)
    if pending_notifications:
        db.session.bulk_insert_mappings(Notification, pending_notifications)

    # --- Phase 4: Finalize ---
    db.session.flush()
    for sid in snapshot_needed:
        sissy = sissy_by_id.get(sid) or db.session.get(User, sid)
        if sissy and sissy.role == "sissy":
            record_market_snapshot(sissy)

    for bot in bot_sissies:
        if bot.level >= 5:
            bot.listed_on_market = True

    db.session.commit()
    return summary


# ---------------------------------------------------------------------------
# Timed Simulation (background thread, drip-feeds over real minutes)
# ---------------------------------------------------------------------------
_timed_sim = {"running": False, "phase": "", "progress": 0, "total": 0, "summary": None, "error": None}


def start_timed_simulation(num_sissies, num_masters, actions_min, actions_max, sim_minutes):
    """Spawn a background thread that drip-feeds simulation actions over real time."""
    global _timed_sim
    if _timed_sim["running"]:
        return False

    _timed_sim.update(running=True, phase="Starting", progress=0, total=0, summary=None, error=None)

    def worker():
        global _timed_sim
        try:
            with app.app_context():
                _run_timed_sim_inner(num_sissies, num_masters, actions_min, actions_max, sim_minutes)
        except Exception as exc:
            _timed_sim["error"] = str(exc)
        finally:
            _timed_sim["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return True


def _run_timed_sim_inner(num_sissies, num_masters, actions_min, actions_max, sim_minutes):
    """Timed simulation — same personality system but spread across real time."""
    global _timed_sim
    total_seconds = sim_minutes * 60

    summary = {
        "sissies_created": 0, "masters_created": 0,
        "posts": 0, "likes": 0, "comments": 0,
        "tasks_created": 0, "tasks_completed": 0,
        "investments": 0, "follows": 0, "gifts": 0,
        "forum_threads": 0, "forum_replies": 0,
    }

    acct_secs = total_seconds * 0.10
    content_secs = total_seconds * 0.35
    interact_secs = total_seconds * 0.55

    now = datetime.utcnow()
    active_mults = Multiplier.query.filter(
        Multiplier.is_active == True,
        db.or_(Multiplier.starts_at == None, Multiplier.starts_at <= now),
        db.or_(Multiplier.expires_at == None, Multiplier.expires_at >= now),
    ).all()
    cached_mult = 1.0
    if active_mults:
        mult_values = [m.multiplier for m in active_mults]
        if MULTIPLIER_STACK_STRATEGY == "compound":
            for v in mult_values:
                cached_mult *= v
        elif MULTIPLIER_STACK_STRATEGY == "additive":
            cached_mult = sum(mult_values)
        elif MULTIPLIER_STACK_STRATEGY == "highest":
            cached_mult = max(mult_values)

    bot_password_hash = generate_password_hash("bot-no-login")
    tag_cache = {t.name: t for t in Tag.query.all()}

    def get_or_create_tag(name):
        if name not in tag_cache:
            t = Tag(name=name)
            db.session.add(t)
            tag_cache[name] = t
        return tag_cache[name]

    def fast_add_points(user, amount, reason=""):
        if user.role != "sissy":
            return
        final_amount = int(amount * cached_mult)
        at_cap = user.level >= LEVEL_CAP
        if at_cap and not LEVEL_CAP_POINTS_CONTINUE:
            final_amount = 0
        old_level = user.level
        user.xp += final_amount
        db.session.add(XPAudit(
            user_id=user.id, amount=final_amount, original_amount=amount,
            multiplier_applied=round(cached_mult, 2), reason=reason, audit_type="add",
        ))
        if not at_cap:
            new_level = user._calculate_level()
            if new_level > old_level:
                user.level = min(new_level, LEVEL_CAP)

    # ---- PHASE 0: Create accounts ----
    _timed_sim["phase"] = "Creating accounts"
    total_bots = num_sissies + num_masters
    _timed_sim["total"] = total_bots
    _timed_sim["progress"] = 0
    acct_delay = acct_secs / max(total_bots, 1)

    bot_sissies = []
    bot_masters = []

    for i in range(num_sissies):
        archetype = _pick_archetype(BOT_ARCHETYPES)
        username = _generate_username("sissy")
        bio = _generate_bio(BOT_ARCHETYPES, archetype)
        user = User(
            username=username, email=f"{username}@bot.gg", role="sissy", is_bot=True,
            bio=bio, avatar_url=generate_avatar(username),
        )
        user.password_hash = bot_password_hash
        user._sim_archetype = archetype
        db.session.add(user)
        db.session.flush()
        bot_sissies.append(user)
        summary["sissies_created"] += 1
        _timed_sim["progress"] = i + 1
        if acct_delay > 0 and total_bots > 1:
            db.session.commit()
            _time.sleep(acct_delay)

    for i in range(num_masters):
        archetype = _pick_archetype(BOT_MASTER_ARCHETYPES)
        username = _generate_username("master")
        bio = _generate_bio(BOT_MASTER_ARCHETYPES, archetype)
        user = User(
            username=username, email=f"{username}@bot.gg", role="master", is_bot=True,
            coins=100, bio=bio, avatar_url=generate_avatar(username),
        )
        user.password_hash = bot_password_hash
        user._sim_archetype = archetype
        db.session.add(user)
        db.session.flush()
        bot_masters.append(user)
        summary["masters_created"] += 1
        _timed_sim["progress"] = num_sissies + i + 1
        if acct_delay > 0 and total_bots > 1:
            db.session.commit()
            _time.sleep(acct_delay)

    db.session.commit()
    all_bots = bot_sissies + bot_masters
    social_graph = _build_social_graph(all_bots, bot_sissies, bot_masters)

    # ---- PHASE 1: Content creation ----
    _timed_sim["phase"] = "Creating content"
    bot_media_pool = _scan_bot_media()
    all_categories = ForumCategory.query.all()

    content_plan = []
    for bot in bot_sissies:
        if bot_media_pool:
            arch = BOT_ARCHETYPES[bot._sim_archetype]
            num_posts = random.randint(*arch["post_rate"])
            for _ in range(num_posts):
                content_plan.append(("post", bot))
    for bot in all_bots:
        arch = (BOT_ARCHETYPES if bot.role == "sissy" else BOT_MASTER_ARCHETYPES).get(bot._sim_archetype, {})
        for _ in range(random.randint(*arch.get("forum_rate", (0, 1)))):
            content_plan.append(("forum_thread", bot))
    for bot in bot_masters:
        arch = BOT_MASTER_ARCHETYPES[bot._sim_archetype]
        for _ in range(random.randint(*arch["task_rate"])):
            content_plan.append(("task", bot))

    random.shuffle(content_plan)
    _timed_sim["total"] = len(content_plan)
    _timed_sim["progress"] = 0
    content_delay = content_secs / max(len(content_plan), 1)
    created_threads = []

    for idx, (ctype, bot) in enumerate(content_plan):
        if ctype == "post" and bot_media_pool:
            arch = BOT_ARCHETYPES.get(bot._sim_archetype, BOT_ARCHETYPES["newbie"])
            title, body = _generate_post(arch["post_styles"])
            media = random.choice(bot_media_pool)
            post = Post(author_id=bot.id, title=title, body=body, media_url=media)
            db.session.add(post)
            db.session.flush()
            for tag_name in extract_hashtags(body + " " + title):
                post.tags.append(get_or_create_tag(tag_name))
            fast_add_points(bot, POST_REWARD_XP, reason=f"Bot post: {title[:40]}")
            summary["posts"] += 1

        elif ctype == "forum_thread" and all_categories:
            cat = random.choice(all_categories)
            title, body = _generate_forum()
            thread = ForumThread(category_id=cat.id, author_id=bot.id, title=title, body=body)
            db.session.add(thread)
            db.session.flush()
            for tag_name in extract_hashtags(title + " " + body):
                thread.tags.append(get_or_create_tag(tag_name))
            if bot.role == "sissy":
                fast_add_points(bot, 5, reason=f"Bot forum: {title[:30]}")
            created_threads.append(thread)
            summary["forum_threads"] += 1

        elif ctype == "task":
            title, desc = _generate_task(bot._sim_archetype)
            reward_coins = random.choice([5, 10, 15, 20, 25, 30, 40, 50])
            if bot.coins >= reward_coins:
                task = Task(
                    creator_id=bot.id, title=title, description=desc,
                    reward_coins=reward_coins, reward_xp=TASK_REWARD_XP, status="open",
                )
                bot.coins -= reward_coins
                db.session.add(task)
                db.session.add(Transaction(user_id=bot.id, amount=-reward_coins, description=f"Task escrow: {title[:30]}"))
                summary["tasks_created"] += 1

        db.session.commit()
        _timed_sim["progress"] = idx + 1
        if content_delay > 0:
            _time.sleep(content_delay)

    # Forum replies burst
    if created_threads:
        for bot in all_bots:
            for _ in range(random.randint(0, 3)):
                thread = random.choice(created_threads)
                reply_body = _generate_forum_reply()
                reply = ForumReply(thread_id=thread.id, author_id=bot.id, body=reply_body)
                thread.last_reply_at = datetime.utcnow()
                db.session.add(reply)
                for tag_name in extract_hashtags(reply_body):
                    existing = {t.name for t in thread.tags}
                    if tag_name not in existing:
                        thread.tags.append(get_or_create_tag(tag_name))
                if bot.role == "sissy":
                    fast_add_points(bot, 2, reason=f"Bot reply in: {thread.title[:30]}")
                summary["forum_replies"] += 1
        db.session.commit()

    # ---- Pre-interaction: list eligible sissies on market ----
    for bot in bot_sissies:
        if bot.level >= 5:
            bot.listed_on_market = True
    db.session.flush()

    # ---- PHASE 2: Interactions ----
    _timed_sim["phase"] = "Running interactions"
    all_sissies = User.query.filter_by(role="sissy").all()
    all_users = User.query.all()
    sissy_by_id = {u.id: u for u in all_sissies}
    user_by_id = {u.id: u for u in all_users}
    all_posts = Post.query.all()
    open_tasks = list(Task.query.filter_by(status="open").all())
    market_sissies = [s for s in all_sissies if s.listed_on_market and s.level >= 5]
    liked_pairs = set()
    follow_pairs = set()
    snapshot_needed = set()

    post_pop = {p.id: random.uniform(0.5, 2.0) for p in all_posts}

    def _pick_popular_post():
        if not all_posts:
            return None
        weights = [post_pop.get(p.id, 1.0) for p in all_posts]
        return random.choices(all_posts, weights=weights, k=1)[0]

    # Build interaction plan from archetype rates
    interaction_plan = []
    for bot in all_bots:
        arch = (BOT_ARCHETYPES if bot.role == "sissy" else BOT_MASTER_ARCHETYPES).get(bot._sim_archetype, {})
        for _ in range(random.randint(*arch.get("like_rate", (1, 3)))):
            interaction_plan.append((bot, "like"))
        for _ in range(random.randint(*arch.get("comment_rate", (0, 2)))):
            interaction_plan.append((bot, "comment"))
        for _ in range(random.randint(*arch.get("follow_rate", (1, 3)))):
            interaction_plan.append((bot, "follow"))
        if bot.role == "sissy":
            for _ in range(random.randint(*arch.get("task_rate", (0, 1)))):
                interaction_plan.append((bot, "accept_task"))
        if bot.role == "master" or bot._sim_archetype == "veteran":
            for _ in range(random.randint(*arch.get("invest_rate", (0, 1)))):
                interaction_plan.append((bot, "invest"))
        for _ in range(random.randint(*arch.get("gift_rate", (0, 1)))):
            interaction_plan.append((bot, "gift"))

    random.shuffle(interaction_plan)
    _timed_sim["total"] = len(interaction_plan)
    _timed_sim["progress"] = 0
    interact_delay = interact_secs / max(len(interaction_plan), 1)

    for idx, (bot, action) in enumerate(interaction_plan):
        if action == "like":
            post = _pick_popular_post()
            if post:
                pair = (bot.id, post.id)
                if pair not in liked_pairs:
                    liked_pairs.add(pair)
                    db.session.execute(likes_table.insert().values(user_id=bot.id, post_id=post.id))
                    post_pop[post.id] = post_pop.get(post.id, 1) + 0.3
                    if post.author_id != bot.id:
                        db.session.add(Notification(
                            user_id=post.author_id, type="like",
                            message=f"{bot.username} liked your post \"{post.title[:30]}\"",
                            link=f"/post/{post.id}",
                        ))
                    summary["likes"] += 1

        elif action == "comment":
            post = _pick_popular_post()
            if post:
                comment_text = _generate_comment(bot._sim_archetype)
                db.session.add(Comment(post_id=post.id, author_id=bot.id, body=comment_text))
                post_pop[post.id] = post_pop.get(post.id, 1) + 0.5
                if post.author_id != bot.id:
                    db.session.add(Notification(
                        user_id=post.author_id, type="comment",
                        message=f"{bot.username} commented on \"{post.title[:30]}\"",
                        link=f"/post/{post.id}",
                    ))
                if bot.role == "sissy":
                    fast_add_points(bot, 3, reason=f"Bot comment on post #{post.id}")
                    snapshot_needed.add(bot.id)
                summary["comments"] += 1

        elif action == "follow":
            target_id = _pick_target(social_graph, bot.id)
            if target_id:
                pair = (bot.id, target_id)
                if pair not in follow_pairs:
                    follow_pairs.add(pair)
                    target = user_by_id.get(target_id)
                    if target:
                        db.session.execute(followers_table.insert().values(follower_id=bot.id, followed_id=target_id))
                        db.session.add(Notification(
                            user_id=target_id, type="follow",
                            message=f"{bot.username} started following you!",
                            link=f"/profile/{bot.username}",
                        ))
                        if target_id in sissy_by_id:
                            snapshot_needed.add(target_id)
                        summary["follows"] += 1

        elif action == "accept_task" and open_tasks:
            candidates = [t for t in open_tasks if t.creator_id != bot.id and t.assignee_id is None]
            if candidates:
                task = random.choice(candidates)
                task.assignee_id = bot.id
                task.status = "completed"
                task.completed_at = datetime.utcnow()
                fast_add_points(bot, task.reward_xp, reason=f"Bot completed: {task.title[:30]}")
                bot.coins += task.reward_coins
                db.session.add(Transaction(user_id=bot.id, amount=task.reward_coins, description=f"Task reward: {task.title[:30]}"))
                db.session.add(Notification(
                    user_id=task.creator_id, type="task",
                    message=f"{bot.username} completed your task \"{task.title[:30]}\"",
                    link="/tasks",
                ))
                snapshot_needed.add(bot.id)
                open_tasks.remove(task)
                summary["tasks_completed"] += 1

        elif action == "invest" and market_sissies:
            target_id = _pick_target(social_graph, bot.id)
            sissy = sissy_by_id.get(target_id) if (target_id and sissy_by_id.get(target_id, None) and sissy_by_id[target_id].listed_on_market) else random.choice(market_sissies)
            if sissy and sissy.id != bot.id:
                amount = random.choice([5, 10, 15, 20, 25, 30, 50])
                if bot.coins >= amount:
                    inv = Investment(investor_id=bot.id, sissy_id=sissy.id, amount=amount, bought_value=sissy.xp)
                    bot.coins -= amount
                    db.session.add(inv)
                    db.session.add(Transaction(user_id=bot.id, amount=-amount, description=f"Invested in {sissy.username}"))
                    db.session.add(Notification(
                        user_id=sissy.id, type="invest",
                        message=f"{bot.username} invested {amount} coins in you!",
                        link="/market",
                    ))
                    snapshot_needed.add(sissy.id)
                    summary["investments"] += 1

        elif action == "gift" and all_sissies:
            target_id = _pick_target(social_graph, bot.id)
            sissy = sissy_by_id.get(target_id) if target_id else random.choice(all_sissies)
            if sissy and sissy.id != bot.id:
                amount = random.choice([1, 2, 3, 5, 10])
                if bot.coins >= amount:
                    bot.coins -= amount
                    db.session.add(Transaction(user_id=bot.id, amount=-amount, description=f"Gift to {sissy.username}"))
                    db.session.add(PendingGift(sender_id=bot.id, recipient_id=sissy.id, amount=amount))
                    db.session.add(Notification(
                        user_id=sissy.id, type="gift",
                        message=f"{bot.username} sent you {amount} coins!",
                        link="/gifts",
                    ))
                    summary["gifts"] += 1

        db.session.commit()
        _timed_sim["progress"] = idx + 1
        if interact_delay > 0:
            _time.sleep(interact_delay)

    # ---- PHASE 3: Finalize ----
    _timed_sim["phase"] = "Finalizing"
    db.session.flush()
    for sid in snapshot_needed:
        sissy = sissy_by_id.get(sid) or db.session.get(User, sid)
        if sissy and sissy.role == "sissy":
            record_market_snapshot(sissy)

    for bot in bot_sissies:
        if bot.level >= 5:
            bot.listed_on_market = True

    db.session.commit()
    _timed_sim["phase"] = "Complete"
    _timed_sim["summary"] = summary



@app.route("/admin/simulate", methods=["POST"])
@login_required
@admin_required
def admin_simulate():
    num_sissies = request.form.get("num_sissies", 5, type=int)
    num_masters = request.form.get("num_masters", 2, type=int)
    actions_min = request.form.get("actions_min", 3, type=int)
    actions_max = request.form.get("actions_max", 10, type=int)
    sim_minutes = request.form.get("sim_minutes", 0, type=int)

    # Clamp values
    num_sissies = max(0, min(num_sissies, 100))
    num_masters = max(0, min(num_masters, 50))
    actions_min = max(1, min(actions_min, 100))
    actions_max = max(actions_min, min(actions_max, 200))
    sim_minutes = max(0, min(sim_minutes, 1440))

    if sim_minutes > 0:
        started = start_timed_simulation(num_sissies, num_masters, actions_min, actions_max, sim_minutes)
        if started:
            flash(f"Timed simulation started! Running over {sim_minutes} minute{'s' if sim_minutes != 1 else ''}.", "info")
        else:
            flash("A timed simulation is already running.", "warning")
        return redirect(url_for("admin_panel"))

    summary = run_simulation(num_sissies, num_masters, actions_min, actions_max)

    flash(
        f"Simulation complete! Created {summary['sissies_created']} sissies + "
        f"{summary['masters_created']} masters. "
        f"Generated: {summary['posts']} posts, {summary.get('forum_threads', 0)} forum threads, "
        f"{summary.get('forum_replies', 0)} forum replies, {summary['likes']} likes, "
        f"{summary['comments']} comments, {summary['tasks_created']} tasks, "
        f"{summary['tasks_completed']} completed, {summary['investments']} investments, "
        f"{summary['follows']} follows, {summary['gifts']} gifts.",
        "success",
    )
    return redirect(url_for("admin_panel"))


@app.route("/admin/sim_status")
@login_required
@admin_required
def admin_sim_status():
    """JSON endpoint for polling timed simulation progress."""
    return jsonify(_timed_sim)


@app.route("/admin/clear_bots", methods=["POST"])
@login_required
@admin_required
def admin_clear_bots():
    """Remove all bot accounts and their associated data, undoing effects on real accounts."""
    bots = User.query.filter_by(is_bot=True).all()
    bot_ids = [b.id for b in bots]
    bot_usernames = [b.username for b in bots]
    if not bot_ids:
        flash("No bot accounts to remove.", "info")
        return redirect(url_for("admin_panel"))

    # --- Reverse gift coin transfers to real users ---
    # With pending gift system, unclaimed bot gifts never reached recipients.
    # Delete unclaimed PendingGifts from bots (no coin reversal needed).
    PendingGift.query.filter(PendingGift.sender_id.in_(bot_ids), PendingGift.is_claimed == False).delete(synchronize_session=False)
    # For claimed bot gifts, reverse the coins recipients already collected.
    claimed_bot_gifts = PendingGift.query.filter(PendingGift.sender_id.in_(bot_ids), PendingGift.is_claimed == True).all()
    for g in claimed_bot_gifts:
        recip = db.session.get(User, g.recipient_id)
        if recip:
            recip.coins = max(0, recip.coins - g.amount)
    PendingGift.query.filter(PendingGift.sender_id.in_(bot_ids)).delete(synchronize_session=False)
    # Also remove PendingGifts sent TO bots
    PendingGift.query.filter(PendingGift.recipient_id.in_(bot_ids)).delete(synchronize_session=False)
    # Legacy: reverse old-style direct gift transactions to real users
    gift_txns = Transaction.query.filter(
        ~Transaction.user_id.in_(bot_ids),
        Transaction.description.like("Bot gift from bot_%"),
    ).all()
    for txn in gift_txns:
        recipient = db.session.get(User, txn.user_id)
        if recipient:
            recipient.coins = max(0, recipient.coins - txn.amount)
    Transaction.query.filter(
        ~Transaction.user_id.in_(bot_ids),
        Transaction.description.like("Bot gift from bot_%"),
    ).delete(synchronize_session=False)

    # --- Reset real-user tasks that bots completed (don't delete them) ---
    real_tasks_botted = Task.query.filter(
        ~Task.creator_id.in_(bot_ids),
        Task.assignee_id.in_(bot_ids),
    ).all()
    for task in real_tasks_botted:
        task.assignee_id = None
        task.status = "open"
        task.completed_at = None
    # Delete tasks created by bots entirely
    Task.query.filter(Task.creator_id.in_(bot_ids)).delete(synchronize_session=False)

    # --- Remove bot-generated notifications on real users ---
    # Bot usernames all start with bot_s_ or bot_m_, match on message content
    Notification.query.filter(
        db.or_(
            Notification.user_id.in_(bot_ids),
            Notification.message.like("%bot\\_s\\_%"),
            Notification.message.like("%bot\\_m\\_%"),
        )
    ).delete(synchronize_session=False)

    # --- Delete bot-owned data ---
    Comment.query.filter(Comment.author_id.in_(bot_ids)).delete(synchronize_session=False)
    Investment.query.filter(
        db.or_(Investment.investor_id.in_(bot_ids), Investment.sissy_id.in_(bot_ids))
    ).delete(synchronize_session=False)
    Transaction.query.filter(Transaction.user_id.in_(bot_ids)).delete(synchronize_session=False)
    Message.query.filter(
        db.or_(Message.sender_id.in_(bot_ids), Message.recipient_id.in_(bot_ids))
    ).delete(synchronize_session=False)
    XPAudit.query.filter(XPAudit.user_id.in_(bot_ids)).delete(synchronize_session=False)
    MarketSnapshot.query.filter(MarketSnapshot.sissy_id.in_(bot_ids)).delete(synchronize_session=False)
    Streak.query.filter(Streak.user_id.in_(bot_ids)).delete(synchronize_session=False)
    StreakHistory.query.filter(StreakHistory.user_id.in_(bot_ids)).delete(synchronize_session=False)
    DailyReward.query.filter(DailyReward.user_id.in_(bot_ids)).delete(synchronize_session=False)
    UserAchievement.query.filter(UserAchievement.user_id.in_(bot_ids)).delete(synchronize_session=False)
    Activity.query.filter(Activity.user_id.in_(bot_ids)).delete(synchronize_session=False)

    # Delete likes by bots
    db.session.execute(likes_table.delete().where(likes_table.c.user_id.in_(bot_ids)))
    # Delete follows by/to bots
    db.session.execute(followers_table.delete().where(
        db.or_(followers_table.c.follower_id.in_(bot_ids), followers_table.c.followed_id.in_(bot_ids))
    ))
    # Delete posts by bots (need to clear likes/tags first)
    bot_posts = Post.query.filter(Post.author_id.in_(bot_ids)).all()
    for p in bot_posts:
        p.liked_by = []
        p.tags = []
    db.session.flush()
    Post.query.filter(Post.author_id.in_(bot_ids)).delete(synchronize_session=False)

    # Delete forum replies and threads by bots (clear tags first)
    ForumReply.query.filter(ForumReply.author_id.in_(bot_ids)).delete(synchronize_session=False)
    bot_threads = ForumThread.query.filter(ForumThread.author_id.in_(bot_ids)).all()
    for t in bot_threads:
        t.tags = []
        # Also delete replies by non-bots in bot-created threads
        ForumReply.query.filter_by(thread_id=t.id).delete(synchronize_session=False)
    db.session.flush()
    ForumThread.query.filter(ForumThread.author_id.in_(bot_ids)).delete(synchronize_session=False)

    # Delete bot users
    User.query.filter(User.id.in_(bot_ids)).delete(synchronize_session=False)
    db.session.commit()

    flash(f"Removed {len(bot_ids)} bot accounts and reversed all effects on real accounts.", "warning")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Seed data (for demo)
# ---------------------------------------------------------------------------
def seed_data():
    """Create demo accounts and default achievements."""
    if User.query.first():
        return

    # Demo admin
    admin = User(username="AdminGG", email="admin@demo.gg", role="admin", coins=100,
                 bio="Platform administrator", avatar_url=generate_avatar("AdminGG"))
    admin.set_password("demo1234")
    db.session.add(admin)

    # Demo master
    master = User(username="DomQueen", email="dom@demo.gg", role="master", coins=100,
                  bio="", avatar_url=generate_avatar("DomQueen"))
    master.set_password("demo1234")
    db.session.add(master)

    # Demo sissy
    sissy = User(username="LaceyDream", email="laceydream@demo.gg", role="sissy",
                 bio="", avatar_url=generate_avatar("LaceyDream"))
    sissy.set_password("demo1234")
    db.session.add(sissy)

    # Default achievements
    default_achievements = [
        ("First Post", "Create your first post", "bi-pencil-square", False, 5),
        ("5 Posts", "Create 5 posts", "bi-collection", False, 15),
        ("25 Posts", "Create 25 posts", "bi-stack", False, 50),
        ("First Task", "Complete your first task", "bi-clipboard-check", False, 10),
        ("Task Master", "Complete 10 tasks", "bi-clipboard2-check", False, 50),
        ("Level 5", "Reach level 5", "bi-arrow-up-circle", False, 20),
        ("Level 10", "Reach level 10", "bi-arrow-up-circle-fill", False, 50),
        ("Level 25", "Reach level 25", "bi-rocket", False, 100),
        ("Level 50", "Reach level 50", "bi-rocket-takeoff", False, 250),
        ("3-Day Streak", "Maintain a 3-day login streak", "bi-fire", False, 10),
        ("7-Day Streak", "Maintain a 7-day login streak", "bi-fire", False, 30),
        ("30-Day Streak", "Maintain a 30-day login streak", "bi-fire", False, 100),
        ("Popular", "Get 10 likes on your posts", "bi-heart-fill", False, 25),
        ("Hidden Gem", "A secret achievement...", "bi-gem", True, 50),
    ]
    for name, desc, icon, secret, xp in default_achievements:
        db.session.add(Achievement(name=name, description=desc, icon=icon, is_secret=secret, xp_reward=xp))

    # Default forum categories
    forum_categories = [
        ("General Discussion", "Chat about anything and everything", "bi-chat-dots", 1),
        ("Introductions", "Say hi and tell us about yourself!", "bi-person-raised-hand", 2),
        ("Tips & Guides", "Share knowledge, tutorials, and advice", "bi-lightbulb", 3),
        ("Challenges", "Post and accept challenges from the community", "bi-trophy", 4),
        ("Style & Fashion", "Outfits, looks, and inspiration", "bi-stars", 5),
        ("Off-Topic", "Memes, random thoughts, and fun stuff", "bi-emoji-laughing", 6),
    ]
    for name, desc, icon, order in forum_categories:
        db.session.add(ForumCategory(name=name, description=desc, icon=icon, sort_order=order))

    db.session.commit()
    print("[seed] Demo accounts and achievements created.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    # Lightweight migration: add columns that may be missing from older DBs
    with db.engine.connect() as conn:
        for col, default in [("profile_frame", "''"), ("profile_title", "''"), ("market_value_boost", "0")]:
            try:
                conn.execute(db.text(f"ALTER TABLE user ADD COLUMN {col} VARCHAR(60) DEFAULT {default}"))
                conn.commit()
            except Exception:
                conn.rollback()
        try:
            conn.execute(db.text("UPDATE forum_category SET icon='bi-person-raised-hand' WHERE icon='bi-hand-wave'"))
            conn.commit()
        except Exception:
            conn.rollback()
    seed_data()
    seed_shop_items()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1", port=5000)
