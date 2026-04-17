# ---------------------------------------------------------------------------
# Admin: Give coins to user

import io
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

from markupsafe import Markup, escape
from flask import (
    Flask, render_template, redirect, url_for, flash, request,
    jsonify, abort, send_from_directory, Response,
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# --- Project modules ---
from extensions import db, login_manager, csrf, limiter
from constants import (
    utcnow, STARTING_LEVEL, LEVEL_CAP, LEVEL_XP_TABLE, TIERS,
    MULTIPLIER_STACK_STRATEGY,
    TASK_REWARD_XP, TASK_REWARD_COINS, POST_REWARD_XP,
    STREAK_BONUS_TABLE, DAILY_LOGIN_COINS, DAILY_LOGIN_XP, DAILY_REWARD_ROADMAP,
    FLAIR_COLORS, FLAIR_COST, BOOST_COST,
    ALLOWED_EXTENSIONS,
)
from models import (
    User, Post, Tag, Task, Investment, Transaction, Activity, PendingGift,
    Multiplier, Achievement, UserAchievement, Streak, StreakHistory,
    XPAudit, MarketSnapshot, Notification, Comment, Message, DailyReward,
    ForumCategory, ForumThread, ForumReply, Report,
    DailyChallenge, UserDailyChallenge, ShopItem, UserPurchase, UserPowerup,
    Lesson, LessonProgress, JournalEntry, JournalPhoto, PostMedia,
    likes_table, post_tags, followers_table, blocked_users,
)
from helpers import (
    generate_avatar, extract_hashtags, hashtagify_filter,
    notify, log_activity, record_market_snapshot,
    distribute_dividends, get_sponsor, check_achievements,
    generate_daily_challenges, bump_daily_challenge,
    validate_password, generate_verification_token, verify_email_token,
    generate_reset_token, verify_reset_token,
)
from simulation import run_simulation, start_timed_simulation, get_timed_sim_status

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY")
if not _secret and not os.environ.get("FLASK_DEBUG"):
    import warnings
    warnings.warn(
        "SECRET_KEY not set! Using insecure default. "
        "Set the SECRET_KEY environment variable for production.",
        stacklevel=1,
    )
    _secret = "goodgurl-dev-key-change-in-production"
app.config["SECRET_KEY"] = _secret
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///goodgurl.db"
# Expose daily reward roadmap to templates
from constants import DAILY_REWARD_ROADMAP
app.config["DAILY_REWARD_ROADMAP"] = DAILY_REWARD_ROADMAP
app.config["LEVEL_XP_TABLE"] = LEVEL_XP_TABLE
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# Initialize extensions with app
db.init_app(app)
csrf.init_app(app)
limiter.init_app(app)
login_manager.init_app(app)


@app.before_request
def _disable_limiter_in_tests():
    """Ensure the rate limiter is off when TESTING is True."""
    limiter.enabled = not app.config.get("TESTING", False)

# Ensure avatar directory exists
AVATAR_DIR = os.path.join(app.static_folder, "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)

# Register template filter
app.template_filter("hashtagify")(hashtagify_filter)


# ---------------------------------------------------------------------------
# HTTPS enforcement
# ---------------------------------------------------------------------------
@app.before_request
def enforce_https():
    if app.testing or app.debug:
        return
    if not request.is_secure and request.headers.get("X-Forwarded-Proto", "http") != "https":
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not app.debug:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403


@app.errorhandler(500)
def server_error(e):
    return render_template("errors/500.html"), 500


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


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapped


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

        # FIX: prevent self-registration as admin
        if role not in ("sissy", "master"):
            flash("Invalid role.", "danger")
            return redirect(url_for("register"))

        # FIX: password strength validation
        pw_error = validate_password(password)
        if pw_error:
            flash(pw_error, "danger")
            return redirect(url_for("register"))

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already taken.", "danger")
            return redirect(url_for("register"))

        user = User(username=username, email=email, role=role,
                    avatar_url=generate_avatar(username))
        user.set_password(password)
        if role == "master":
            user.coins = 100
        db.session.add(user)
        db.session.flush()
        if role == "sissy":
            user.record_streak("login")

        # FIX: email verification token
        token = generate_verification_token(email)
        flash(f"Welcome! Please verify your email. Your verification link: /verify/{token}", "info")

        db.session.commit()
        login_user(user)
        flash("Welcome to goodgurl.gg!", "success")
        return redirect(url_for("feed"))
    return render_template("register.html")


@app.route("/verify/<token>")
def verify_email(token):
    """Verify a user's email address via a signed token."""
    email = verify_email_token(token)
    if email is None:
        flash("Verification link is invalid or has expired.", "danger")
        return redirect(url_for("login"))
    user = User.query.filter_by(email=email).first()
    if user is None:
        flash("Account not found.", "danger")
        return redirect(url_for("login"))
    if user.is_verified:
        flash("Email already verified.", "info")
    else:
        user.is_verified = True
        db.session.commit()
        flash("Email verified successfully!", "success")
    return redirect(url_for("feed"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.is_banned:
            flash("This account has been banned.", "danger")
            return render_template("login.html")
        if user and user.check_password(password):
            login_user(user)
            if user.role == "sissy":
                streak, increased = user.record_streak("login")
                if increased and streak.count > 1:
                    flash(f"Login streak: {streak.count} days!", "info")


                today = utcnow().date()
                already_claimed = DailyReward.query.filter_by(user_id=user.id, date=today).first()
                if not already_claimed:
                    streak_day = min(streak.count, len(DAILY_REWARD_ROADMAP))
                    reward = DAILY_REWARD_ROADMAP[streak_day-1] if streak_day > 0 else DAILY_REWARD_ROADMAP[0]
                    coins_reward = reward["coins"]
                    xp_reward = reward["xp"]

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


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        user = User.query.filter_by(email=email).first()
        if user:
            token = generate_reset_token(email)
            flash(f"Password reset link (valid 1 hour): /reset-password/{token}", "info")
        else:
            flash("If that email exists, a reset link has been generated.", "info")
        return redirect(url_for("forgot_password"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    email = verify_reset_token(token)
    if email is None:
        flash("Reset link is invalid or has expired.", "danger")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        pw_error = validate_password(password)
        if pw_error:
            flash(pw_error, "danger")
            return redirect(url_for("reset_password", token=token))
        user = User.query.filter_by(email=email).first()
        if not user:
            flash("Account not found.", "danger")
            return redirect(url_for("login"))
        user.set_password(password)
        db.session.commit()
        flash("Password reset successfully! Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("reset_password.html", token=token)


# ---------------------------------------------------------------------------
# Reels
# ---------------------------------------------------------------------------
@app.route("/reels")
def reels():
    return render_template("reels.html")


@app.route("/api/reels")
@limiter.limit("30 per minute")
def api_reels():
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
# Feed
# ---------------------------------------------------------------------------
FEED_PER_PAGE = 24


@app.route("/")
def feed():
    page = request.args.get("page", 1, type=int)
    tab = request.args.get("tab", "all")

    if current_user.is_authenticated and tab == "following":
        followed_ids = [u.id for u in current_user.followed.all()]
        if followed_ids:
            q = Post.query.filter(Post.author_id.in_(followed_ids))
        else:
            q = Post.query.filter(db.false())
    elif current_user.is_authenticated:
        liked_post_ids = db.session.query(likes_table.c.post_id).filter(
            likes_table.c.user_id == current_user.id
        ).subquery()
        liked_tag_ids = db.session.query(post_tags.c.tag_id).filter(
            post_tags.c.post_id.in_(db.session.query(liked_post_ids))
        ).subquery()

        curated_ids_q = Post.query.filter(
            Post.tags.any(Tag.id.in_(db.session.query(liked_tag_ids)))
        ).with_entities(Post.id).subquery()

        q = Post.query
    else:
        q = Post.query

    q = q.order_by(Post.is_boosted.desc(), Post.created_at.desc())
    pagination = q.paginate(page=page, per_page=FEED_PER_PAGE, error_out=False)

    # Top users (by XP)
    top_users = User.query.order_by(User.xp.desc()).limit(3).all() if current_user.is_authenticated else []

    # Daily challenge (placeholder logic)
    daily_challenge = None
    if current_user.is_authenticated:
        daily_challenge = {
            'id': 1,
            'title': 'Like 5 posts',
            'description': 'Like 5 different posts today.',
            'progress_pct': 60,
            'completed': False
        }

    # Site-wide goal (placeholder logic)
    site_goal = {
        'description': 'Reach 100 posts site-wide today!',
        'progress': 42,
        'target': 100,
        'unit': 'posts',
        'progress_pct': 42
    }

    # Quick quest (placeholder logic)
    quick_quest = {'id': 1, 'title': 'Comment on a post', 'description': 'Leave a comment on any post.'}

    # Market ticker data
    ticker_sissies = User.query.filter_by(role="sissy", listed_on_market=True).order_by(
        User.xp.desc()
    ).limit(20).all()
    ticker_data = []
    for s in ticker_sissies:
        mv = s.market_value
        first_snap = MarketSnapshot.query.filter_by(sissy_id=s.id).order_by(
            MarketSnapshot.timestamp.asc()
        ).first()
        if first_snap and first_snap.market_value > 0:
            change_pct = round((mv - first_snap.market_value) / first_snap.market_value * 100, 1)
        else:
            change_pct = 0.0
        ticker_data.append({
            "username": s.username,
            "value": mv,
            "change_pct": change_pct,
        })

    return render_template(
        "feed.html",
        posts=pagination.items,
        pagination=pagination,
        tab=tab,
        top_users=top_users,
        daily_challenge=daily_challenge,
        site_goal=site_goal,
        quick_quest=quick_quest,
        ticker_data=ticker_data,
    )


# ---------------------------------------------------------------------------
# GG TV
# ---------------------------------------------------------------------------
@app.route("/ggtv")
def ggtv():
    return render_template("ggtv.html")


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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
        tag_names = list(dict.fromkeys(tag_names + extract_hashtags(body)))

        if not title or not body:
            flash("Title and body are required.", "danger")
            return redirect(url_for("new_post"))

        media_url = ""
        media_urls = []
        files = request.files.getlist("media_file")
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                ext = file.filename.rsplit(".", 1)[1].lower()
                safe_name = secrets.token_hex(16) + "." + ext
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], safe_name))
                media_urls.append(url_for("uploaded_file", filename=safe_name))
            elif file and file.filename:
                flash("File type not allowed. Accepted: png, jpg, jpeg, gif, webp, mp4, webm.", "warning")
                return redirect(url_for("new_post"))

        if media_urls:
            media_url = media_urls[0]

        if not media_url:
            media_url = request.form.get("media_url", "").strip()
            if media_url:
                parsed = urlparse(media_url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    flash("Invalid media URL. Only http/https URLs are allowed.", "danger")
                    return redirect(url_for("new_post"))
                media_urls = [media_url]

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

        # Powerup logic
        powerup_id = request.form.get("powerup_id")
        applied_powerup = None
        if powerup_id:
            from models import UserPowerup
            pu = UserPowerup.query.filter_by(id=powerup_id, user_id=current_user.id, used=False).first()
            if pu:
                applied_powerup = pu
                pu.used = True
                from datetime import datetime
                pu.used_at = datetime.utcnow()
                # Apply effect
                if pu.item.css_class == "boost":
                    post.is_boosted = True
                elif pu.item.css_class == "double-xp":
                    # We'll double the XP after add_points
                    pass
                elif pu.item.css_class == "golden-post":
                    post.body = f"<span class='golden-post'>{post.body}</span>"

        db.session.add(post)
        db.session.flush()

        # Create PostMedia entries for album
        for idx, murl in enumerate(media_urls):
            ext = murl.rsplit(".", 1)[-1].lower() if "." in murl else ""
            if ext in ("mp4", "webm"):
                mtype = "video"
            elif ext == "gif":
                mtype = "gif"
            else:
                mtype = "image"
            db.session.add(PostMedia(post_id=post.id, media_url=murl, media_type=mtype, position=idx))

        # XP logic
        final_xp, levelled_up = current_user.add_points(POST_REWARD_XP, reason=f"Posted: {title}")
        # Double XP if powerup applied
        if applied_powerup and applied_powerup.item.css_class == "double-xp":
            current_user.add_points(POST_REWARD_XP, reason=f"Double XP Powerup: {title}")
            final_xp *= 2
        current_user.record_streak("post")

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
        if applied_powerup:
            msg += f" (Powerup applied: {applied_powerup.item.name})"
        if levelled_up:
            msg += f" Level up! You are now level {current_user.level}!"
        flash(msg, "success")
        return redirect(url_for("feed"))
    # Pass available (unused) powerups to the template
    available_powerups = []
    if hasattr(current_user, 'powerups'):
        available_powerups = [pu for pu in current_user.powerups if not pu.used]
    return render_template("new_post.html", available_powerups=available_powerups)


@app.route("/post/<int:post_id>")
def view_post(post_id):
    post = Post.query.get_or_404(post_id)
    return render_template("view_post.html", post=post)


@app.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user in post.liked_by.all():
        post.liked_by.remove(current_user)
    else:
        post.liked_by.append(current_user)
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
            title=title, description=description,
            reward_coins=reward_coins, reward_xp=reward_xp,
            status="accepted" if assignee else "open",
        )
        current_user.coins -= reward_coins
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
    task.completed_at = utcnow()
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


@app.route("/task/<int:task_id>/reset", methods=["POST"])
@login_required
def reset_task(task_id):
    task = Task.query.get_or_404(task_id)
    if task.status != "accepted":
        abort(403)
    # Sissies can reset their own accepted tasks; masters can reset tasks they created
    if current_user.role == "sissy" and task.assignee_id != current_user.id:
        abort(403)
    if current_user.role == "master" and task.creator_id != current_user.id:
        abort(403)
    old_assignee_id = task.assignee_id
    task.assignee_id = None
    task.status = "open"
    log_activity(current_user.id, "task_reset",
                 f'Reset progress on task "{escape(task.title)}"')
    if current_user.role == "sissy":
        notify(task.creator_id, "task",
               f"{current_user.username} dropped your task \"{task.title}\"",
               url_for("tasks"))
    elif old_assignee_id:
        notify(old_assignee_id, "task",
               f"{current_user.username} reset your task \"{task.title}\"",
               url_for("tasks"))
    db.session.commit()
    flash("Task progress reset. The task is open again.", "info")
    return redirect(url_for("tasks"))


# ---------------------------------------------------------------------------
# Daily Challenges
# ---------------------------------------------------------------------------
@app.route("/challenges")
@login_required
def challenges():
    today = utcnow().date()
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
    if ShopItem.query.count() > 0:
        return
    items = [
        # Frames
        ShopItem(name="Pink Glow Frame", description="A glowing pink border around your avatar", category="frame", css_class="frame-pink-glow", price=50, level_required=3, icon="bi-circle"),
        ShopItem(name="Gold Frame", description="A prestigious golden avatar frame", category="frame", css_class="frame-gold", price=150, level_required=10, icon="bi-circle"),
        ShopItem(name="Rainbow Frame", description="An animated rainbow avatar frame", category="frame", css_class="frame-rainbow", price=300, level_required=20, icon="bi-circle"),
        ShopItem(name="Neon Frame", description="Neon-lit avatar frame", category="frame", css_class="frame-neon", price=200, level_required=15, icon="bi-circle"),
        # Titles
        ShopItem(name="Princess", description="Display 'Princess' title on your profile", category="title", css_class="Princess", price=75, level_required=5, icon="bi-stars"),
        ShopItem(name="Queen", description="Display 'Queen' title on your profile", category="title", css_class="Queen", price=200, level_required=15, icon="bi-stars"),
        ShopItem(name="Goddess", description="Display 'Goddess' title on your profile", category="title", css_class="Goddess", price=500, level_required=25, icon="bi-stars"),
        # Flair
        ShopItem(name="Pink Flair", description="Your username glows pink", category="flair", css_class="#ff69b4", price=30, level_required=2, icon="bi-palette"),
        ShopItem(name="Purple Flair", description="Your username glows purple", category="flair", css_class="#9b59b6", price=30, level_required=2, icon="bi-palette"),
        ShopItem(name="Gold Flair", description="Your username glows gold", category="flair", css_class="#ffd700", price=100, level_required=10, icon="bi-palette"),
        # Powerups
        ShopItem(name="Boost Post", description="Boost your post to the top of the feed for 24h.", category="powerup", css_class="boost", price=50, level_required=1, icon="bi-lightning"),
        ShopItem(name="Double XP", description="Double XP for this post.", category="powerup", css_class="double-xp", price=75, level_required=1, icon="bi-stars"),
        ShopItem(name="Golden Post", description="Make your post stand out with a golden border.", category="powerup", css_class="golden-post", price=100, level_required=5, icon="bi-award"),
    ]
    db.session.add_all(items)
    db.session.commit()


def seed_academy_lessons():
    """No longer seeds static lessons — daily generation handles it."""
    pass


# ---------------------------------------------------------------------------
# Daily Lesson Template Pools
# ---------------------------------------------------------------------------
FEMINIZATION_LESSON_POOL = [
    {"audio_title": "Voice Feminization Practice", "audio_desc": "Gentle exercises to soften your vocal tone and develop a feminine speaking voice.",
     "task_title": "Voice Recording Challenge", "task_desc": "Record yourself reading a passage using your feminized voice. Listen back and note improvements.",
     "objective": "Record a 1-minute voice clip practicing your feminine voice."},
    {"audio_title": "Feminine Posture & Movement", "audio_desc": "Guided session on graceful posture, sitting, and walking with feminine elegance.",
     "task_title": "Posture Practice Session", "task_desc": "Practice feminine posture for 20 minutes — sit, stand, and walk with intention.",
     "objective": "Spend 20 minutes practicing graceful posture and movement."},
    {"audio_title": "Skincare & Self-Care Ritual", "audio_desc": "Learn the fundamentals of a nurturing skincare routine to feel pampered and feminine.",
     "task_title": "Create Your Skincare Routine", "task_desc": "Design and follow your personal morning or evening skincare ritual.",
     "objective": "Complete a full skincare routine (cleanse, tone, moisturize) and document it."},
    {"audio_title": "Makeup Basics: Foundation & Eyes", "audio_desc": "Step-by-step audio guide for foundation application and eye makeup essentials.",
     "task_title": "Everyday Makeup Look", "task_desc": "Apply a natural everyday makeup look following the techniques from the audio session.",
     "objective": "Create a subtle everyday makeup look and share a selfie or describe the result."},
    {"audio_title": "Wardrobe & Style Exploration", "audio_desc": "Discover how to build a feminine wardrobe that expresses your true self.",
     "task_title": "Outfit of the Day", "task_desc": "Put together a feminine outfit — focus on colors, fit, and how it makes you feel.",
     "objective": "Assemble and wear a complete feminine outfit today."},
    {"audio_title": "Feminine Mannerisms & Gestures", "audio_desc": "Learn subtle hand gestures, expressions, and social mannerisms that radiate femininity.",
     "task_title": "Mannerism Mirror Practice", "task_desc": "Practice feminine gestures and expressions in front of a mirror for 15 minutes.",
     "objective": "Spend 15 minutes rehearsing feminine mannerisms in front of a mirror."},
    {"audio_title": "Confidence & Self-Love Meditation", "audio_desc": "A calming meditation to embrace your feminine identity with confidence and self-love.",
     "task_title": "Affirmation Journal Entry", "task_desc": "Write 5 positive affirmations about your femininity and read them aloud.",
     "objective": "Write and recite 5 positive feminization affirmations."},
    {"audio_title": "Hair Styling Essentials", "audio_desc": "Tips and techniques for feminine hair care, styling, and wig maintenance.",
     "task_title": "Style Session", "task_desc": "Style your hair or wig in a new feminine look and document the result.",
     "objective": "Try a new feminine hairstyle or wig style today."},
    {"audio_title": "Walking in Heels Training", "audio_desc": "Master the art of walking confidently and gracefully in heels.",
     "task_title": "Heel Walk Practice", "task_desc": "Practice walking in heels for at least 15 minutes. Focus on balance and posture.",
     "objective": "Walk in heels for 15 minutes, practicing turns and stopping."},
    {"audio_title": "Social Feminization Skills", "audio_desc": "Navigate social situations with feminine grace — conversation, body language, and presence.",
     "task_title": "Social Interaction Practice", "task_desc": "Use feminine social cues in a real or practice conversation today.",
     "objective": "Have a conversation (in person or online) using feminine social cues."},
    {"audio_title": "Feminine Writing & Expression", "audio_desc": "Develop a softer, more expressive writing and communication style.",
     "task_title": "Feminine Letter Writing", "task_desc": "Write a short letter or message using a warm, feminine tone.",
     "objective": "Write a heartfelt message or journal entry in a feminine voice."},
    {"audio_title": "Body Language Mastery", "audio_desc": "Advanced body language techniques that communicate elegance and femininity.",
     "task_title": "Body Language Challenge", "task_desc": "Consciously use feminine body language throughout your day.",
     "objective": "Practice feminine body language for at least 1 hour during daily activities."},
]

BIMBOFICATION_LESSON_POOL = [
    {"audio_title": "Bimbo Mindset Activation", "audio_desc": "Embrace the bubbly, carefree, and joyful bimbo state of mind.",
     "task_title": "Positivity Burst", "task_desc": "Write down 10 things that make you happy and bubbly — embrace the energy!",
     "objective": "List 10 things that fill you with bubbly joy and read them aloud."},
    {"audio_title": "Bimbo Affirmation Session", "audio_desc": "Powerful affirmations to reinforce your bimbo transformation and self-love.",
     "task_title": "Affirmation Repetition", "task_desc": "Repeat your bimbo affirmations 10 times each with enthusiasm and belief.",
     "objective": "Recite each affirmation from the audio at least 10 times."},
    {"audio_title": "Glam Makeup Masterclass", "audio_desc": "Bold lips, dramatic lashes, and full glam — the bimbo beauty standard.",
     "task_title": "Full Glam Look", "task_desc": "Create a full glam bimbo makeup look — go bold with lips and lashes!",
     "objective": "Apply full glam makeup with bold lip color and dramatic lashes."},
    {"audio_title": "Body Confidence Meditation", "audio_desc": "Love every inch of yourself — a guided meditation for total body confidence.",
     "task_title": "Body Positivity Selfie", "task_desc": "Take a confidence selfie and write 3 things you love about your body.",
     "objective": "Take a selfie and list 3 things you love about your appearance."},
    {"audio_title": "Bimbo Fashion Guide", "audio_desc": "Pink, platform heels, mini skirts, and crop tops — dress like the bimbo you are.",
     "task_title": "Bimbo Outfit Assembly", "task_desc": "Put together the most bimbo outfit you can — think pink, tight, and fun!",
     "objective": "Assemble and wear (or plan) a full bimbo outfit."},
    {"audio_title": "Bubbly Personality Training", "audio_desc": "Learn to be the life of the party — giggly, charming, and irresistibly fun.",
     "task_title": "Social Butterfly Challenge", "task_desc": "Be extra bubbly and cheerful in all your interactions today.",
     "objective": "Practice bubbly, enthusiastic energy in at least 3 interactions."},
    {"audio_title": "Bimbo Fitness & Curves", "audio_desc": "Exercises and routines to enhance your curves and bimbo physique.",
     "task_title": "Bimbo Workout", "task_desc": "Complete a 20-minute workout focused on glutes, waist, and overall tone.",
     "objective": "Do a 20-minute bimbo fitness routine (squats, hip thrusts, stretches)."},
    {"audio_title": "Pink Aesthetic Immersion", "audio_desc": "Surround yourself with the bimbo aesthetic — pink everything, glitter, and glamour.",
     "task_title": "Aesthetic Mood Board", "task_desc": "Create a bimbo aesthetic mood board with your dream looks, decor, and vibes.",
     "objective": "Create a mood board (digital or physical) of your ideal bimbo aesthetic."},
    {"audio_title": "Bimbo Speech Patterns", "audio_desc": "Learn the playful, valley-girl-inspired speech patterns that define bimbo culture.",
     "task_title": "Bimbo Talk Practice", "task_desc": "Practice bimbo speech patterns for 15 minutes — breathy, playful, and fun!",
     "objective": "Record or practice bimbo speech patterns for 15 minutes."},
    {"audio_title": "Self-Care & Pampering", "audio_desc": "Treat yourself like the princess you are — nails, bath, face masks, and more.",
     "task_title": "Pamper Session", "task_desc": "Have a full pamper session — bath, face mask, nails, the works!",
     "objective": "Complete a full pampering session (bath/shower, face mask, nail care)."},
    {"audio_title": "Bimbo Dance & Movement", "audio_desc": "Move your body with confidence — bimbo dance moves and sultry poses.",
     "task_title": "Dance It Out", "task_desc": "Put on your favorite song and dance freely for 10 minutes — let loose!",
     "objective": "Dance freely and confidently to music for at least 10 minutes."},
    {"audio_title": "Total Bimbo Energy", "audio_desc": "Channel maximum bimbo energy — combine everything for your ultimate transformation.",
     "task_title": "Bimbo Day Challenge", "task_desc": "Live your entire day in full bimbo mode — outfit, attitude, energy, everything!",
     "objective": "Commit to full bimbo energy for the rest of the day."},
]


# Fixed Session 1 for Feminization track (every day)
FEMINIZATION_SESSION_ONE = {
    "audio_title": "Assuming Feminine Identity",
    "audio_desc": "Before any training can begin, you must first assume your feminine identity. This guided audio walks you through getting properly dressed, mentally preparing, and fully submitting to your role as a sissy.",
    "task_title": "Feminine Identity Preparation",
    "task_desc": "Get properly dressed in feminine attire, do your makeup if available, and mentally assume your position as a sissy before proceeding with today's training.",
    "objective": "Dress in your feminine attire, prepare yourself mentally, and affirm your sissy identity. You must be in role before continuing with today's sessions.",
}


def generate_daily_lessons_for_date(date_str):
    """Generate 6 lessons per track for a given date (3 audio + 3 paired tasks)."""
    import hashlib
    existing = Lesson.query.filter_by(date_key=date_str).first()
    if existing:
        return

    for track, pool, fp_reward, bp_reward in [
        ("feminization", FEMINIZATION_LESSON_POOL, True, False),
        ("bimbofication", BIMBOFICATION_LESSON_POOL, False, True),
    ]:
        # Deterministic selection based on date + track for consistency
        seed_val = int(hashlib.md5(f"{date_str}-{track}".encode()).hexdigest(), 16)
        import random
        rng = random.Random(seed_val)

        order = 1

        if track == "feminization":
            # Session 1 is always "Assuming Feminine Identity"
            item = FEMINIZATION_SESSION_ONE
            audio = Lesson(
                track=track, order=order, title=item["audio_title"],
                description=item["audio_desc"], lesson_type="audio",
                reward_xp=15, reward_coins=10, reward_fp=10,
                is_active=True, date_key=date_str,
            )
            db.session.add(audio)
            db.session.flush()

            task = Lesson(
                track=track, order=order + 1, title=item["task_title"],
                description=item["task_desc"], lesson_type="objective",
                objective_text=item["objective"],
                reward_xp=20, reward_coins=15, reward_fp=15,
                is_active=True, date_key=date_str, paired_with=audio.id,
            )
            db.session.add(task)
            order += 2

            # Sessions 2 & 3 from the random pool
            selected = rng.sample(pool, min(2, len(pool)))
        else:
            # Bimbofication: 3 random sessions
            selected = rng.sample(pool, min(3, len(pool)))

        for item in selected:
            # Create audio lesson
            audio = Lesson(
                track=track, order=order, title=item["audio_title"],
                description=item["audio_desc"], lesson_type="audio",
                reward_xp=15, reward_coins=10,
                reward_fp=10 if fp_reward else 0,
                reward_bp=10 if bp_reward else 0,
                is_active=True, date_key=date_str,
            )
            db.session.add(audio)
            db.session.flush()

            task = Lesson(
                track=track, order=order + 1, title=item["task_title"],
                description=item["task_desc"], lesson_type="objective",
                objective_text=item["objective"],
                reward_xp=20, reward_coins=15,
                reward_fp=15 if fp_reward else 0,
                reward_bp=15 if bp_reward else 0,
                is_active=True, date_key=date_str, paired_with=audio.id,
            )
            db.session.add(task)
            order += 2

    db.session.commit()


@app.route("/shop")
@login_required
def shop():
    items = ShopItem.query.order_by(ShopItem.category, ShopItem.price).all()
    owned_ids = {p.item_id for p in UserPurchase.query.filter_by(user_id=current_user.id).all()}
    # Powerups: pass all UserPowerup objects for current user
    powerups = []
    if hasattr(current_user, 'powerups'):
        powerups = current_user.powerups
    return render_template("shop.html", items=items, owned_ids=owned_ids, powerups=powerups)



@app.route("/shop/buy/<int:item_id>", methods=["POST"])
@login_required
def buy_shop_item(item_id):
    item = ShopItem.query.get_or_404(item_id)
    if item.category == "powerup":
        # Powerups are single-use, allow buying multiples
        if current_user.level < item.level_required:
            flash(f"You need level {item.level_required} to buy this.", "warning")
            return redirect(url_for("shop"))
        if current_user.coins < item.price:
            flash("Not enough coins!", "danger")
            return redirect(url_for("shop"))
        current_user.coins -= item.price
        db.session.add(UserPowerup(user_id=current_user.id, item_id=item.id))
        db.session.add(Transaction(user_id=current_user.id, amount=-item.price,
                                   description=f"Bought powerup: {item.name}"))
        db.session.commit()
        flash(f"Purchased {item.name}!", "success")
        return redirect(url_for("shop"))
    else:
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
# Forum
# ---------------------------------------------------------------------------
@app.route("/forum")
@login_required
def forum_index():
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
    category = ForumCategory.query.get_or_404(category_id)
    page = request.args.get("page", 1, type=int)
    threads = category.threads.paginate(page=page, per_page=20, error_out=False)
    return render_template("forum/category.html", category=category, threads=threads)


@app.route("/forum/<int:category_id>/new", methods=["GET", "POST"])
@login_required
def forum_new_thread(category_id):
    category = ForumCategory.query.get_or_404(category_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        if not title or not body:
            flash("Title and body are required.", "danger")
            return redirect(url_for("forum_new_thread", category_id=category_id))
        thread = ForumThread(
            category_id=category.id, author_id=current_user.id,
            title=title, body=body,
        )
        db.session.add(thread)
        db.session.flush()
        for tag_name in extract_hashtags(body):
            tag = Tag.query.filter_by(name=tag_name).first()
            if not tag:
                tag = Tag(name=tag_name)
                db.session.add(tag)
            thread.tags.append(tag)
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
            thread_id=thread.id, author_id=current_user.id, body=body,
        )
        thread.last_reply_at = utcnow()
        db.session.add(reply)
        existing_tag_names = {t.name for t in thread.tags}
        for tag_name in extract_hashtags(body):
            if tag_name not in existing_tag_names:
                tag = Tag.query.filter_by(name=tag_name).first()
                if not tag:
                    tag = Tag(name=tag_name)
                    db.session.add(tag)
                thread.tags.append(tag)
                existing_tag_names.add(tag_name)
        if thread.author_id != current_user.id:
            notify(thread.author_id, "forum_reply",
                   f"{current_user.username} replied to \"{thread.title}\"",
                   url_for("forum_thread", thread_id=thread.id))
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
    db.session.commit()
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
    for s in sissies:
        total_invested = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=s.id).scalar() or 0
        investors = Investment.query.filter_by(sissy_id=s.id).count()
        current_mv = s.market_value

        first_snap = MarketSnapshot.query.filter_by(sissy_id=s.id).order_by(MarketSnapshot.timestamp.asc()).first()
        if first_snap and first_snap.market_value > 0:
            actual_growth = round((current_mv - first_snap.market_value) / first_snap.market_value * 100, 1)
        else:
            actual_growth = 0.0

        recent_snaps = MarketSnapshot.query.filter_by(sissy_id=s.id).order_by(
            MarketSnapshot.timestamp.desc()
        ).limit(5).all()
        if len(recent_snaps) >= 2:
            oldest = recent_snaps[-1]
            newest = recent_snaps[0]
            delta_val = newest.market_value - oldest.market_value
            hours = max((newest.timestamp - oldest.timestamp).total_seconds() / 3600, 0.1)
            rate_per_hour = delta_val / hours
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
        investor_id=current_user.id, sissy_id=sissy.id,
        amount=amount, bought_value=sissy.market_value,
    )
    current_user.coins -= amount
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
# Wallet
# ---------------------------------------------------------------------------
@app.route("/wallet")
@login_required
@role_required("sissy")
def wallet():
    investments_in_me = Investment.query.filter_by(sissy_id=current_user.id).all()
    total_funded = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=current_user.id).scalar() or 0
    sponsor = get_sponsor(current_user)
    # Angel Shop: get all powerups
    powerups = ShopItem.query.filter_by(category="powerup").order_by(ShopItem.price).all()
    user_powerups = []
    if hasattr(current_user, 'powerups'):
        user_powerups = current_user.powerups
    return render_template("wallet.html",
        investments=investments_in_me, total_funded=total_funded,
        sponsor=sponsor, flair_colors=FLAIR_COLORS, flair_cost=FLAIR_COST,
        powerups=powerups, user_powerups=user_powerups)
# Angel Shop: buy powerup with funded coins
@app.route("/wallet/buy_powerup/<int:item_id>", methods=["POST"])
@login_required
@role_required("sissy")
def wallet_buy_powerup(item_id):
    item = ShopItem.query.get_or_404(item_id)
    if item.category != "powerup":
        flash("This item is not a powerup.", "danger")
        return redirect(url_for("wallet"))
    if current_user.level < item.level_required:
        flash(f"You need level {item.level_required} to buy this.", "warning")
        return redirect(url_for("wallet"))
    if current_user.funded_coins < item.price:
        flash("Not enough funded coins!", "danger")
        return redirect(url_for("wallet"))
    current_user.funded_coins -= item.price
    db.session.add(UserPowerup(user_id=current_user.id, item_id=item.id))
    db.session.add(Transaction(user_id=current_user.id, amount=-item.price,
                               description=f"Bought powerup (angel shop): {item.name}"))
    db.session.commit()
    flash(f"Purchased {item.name} with investor funds!", "success")
    return redirect(url_for("wallet"))


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
    flash("Post boosted! It will appear at the top of the feed.", "success")
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
# Hashtag Pages
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
    seed = f"{current_user.username}-{secrets.token_hex(4)}"
    current_user.avatar_url = generate_avatar(seed)
    db.session.commit()
    flash("New avatar generated!", "success")
    return redirect(url_for("account"))


@app.route("/account/buy_coins", methods=["POST"])
@login_required
@role_required("master")
def buy_coins():
    amount = request.form.get("amount", 0, type=int)
    if amount <= 0 or amount > 10000:
        flash("Invalid amount (1-10000).", "danger")
        return redirect(url_for("account"))
    current_user.coins += amount
    db.session.add(Transaction(user_id=current_user.id, amount=amount, description=f"Purchased {amount} coins"))
    log_activity(current_user.id, "buy_coins", f'Purchased {amount} coins')
    db.session.commit()
    flash(f"Added {amount} coins to your account!", "success")
    return redirect(url_for("account"))


@app.route("/account/change-password", methods=["POST"])
@login_required
def change_password():
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    if not current_user.check_password(current_pw):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("account"))
    pw_error = validate_password(new_pw)
    if pw_error:
        flash(pw_error, "danger")
        return redirect(url_for("account"))
    current_user.set_password(new_pw)
    db.session.commit()
    flash("Password changed successfully!", "success")
    return redirect(url_for("account"))


@app.route("/account/upload-avatar", methods=["POST"])
@login_required
def upload_avatar():
    file = request.files.get("avatar")
    if not file or not file.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("account"))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
        flash("Only PNG, JPG, WEBP, or GIF images are allowed.", "danger")
        return redirect(url_for("account"))
    safe_name = f"{current_user.username}-{secrets.token_hex(4)}.{ext}"
    avatar_dir = os.path.join(app.static_folder, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    file.save(os.path.join(avatar_dir, safe_name))
    current_user.avatar_url = f"/static/avatars/{safe_name}"
    db.session.commit()
    flash("Profile picture updated!", "success")
    return redirect(url_for("account"))


@app.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    password = request.form.get("password", "")
    if not current_user.check_password(password):
        flash("Incorrect password. Account not deleted.", "danger")
        return redirect(url_for("account"))
    user = current_user._get_current_object()
    logout_user()
    db.session.delete(user)
    db.session.commit()
    flash("Your account has been permanently deleted.", "info")
    return redirect(url_for("feed"))


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
    journal_entries = JournalEntry.query.filter_by(user_id=user.id).order_by(JournalEntry.created_at.desc()).all() if user.role == "sissy" else []
    return render_template("profile.html", user=user, posts=posts, threads=threads,
                           completed_tasks=completed_tasks,
                           user_achievements=user_achievements, login_streak=login_streak,
                           sponsor=sponsor, journal_entries=journal_entries)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
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
@limiter.limit("30 per minute")
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
@limiter.limit("30 per minute")
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
# Reports
# ---------------------------------------------------------------------------
@app.route("/report/post/<int:post_id>", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
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
@limiter.limit("10 per minute")
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
# Block / Unblock
# ---------------------------------------------------------------------------
@app.route("/block/<string:username>", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
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
@limiter.limit("20 per minute")
def unblock_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    if current_user.blocked.filter(blocked_users.c.blocked_id == user.id).count() > 0:
        current_user.blocked.remove(user)
        db.session.commit()
        flash(f"{user.username} has been unblocked.", "info")
    return redirect(url_for("profile", username=username))


# ---------------------------------------------------------------------------
# Gifting
# ---------------------------------------------------------------------------
@app.route("/gift/<string:username>", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
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
# Gift Chest
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
@limiter.limit("20 per minute")
def claim_gift(gift_id):
    gift = PendingGift.query.get_or_404(gift_id)
    if gift.recipient_id != current_user.id:
        abort(403)
    if gift.is_claimed:
        flash("Already claimed.", "info")
        return redirect(url_for("gifts"))
    gift.is_claimed = True
    gift.claimed_at = utcnow()
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
@limiter.limit("5 per minute")
def claim_all_gifts():
    pending = PendingGift.query.filter_by(recipient_id=current_user.id, is_claimed=False).all()
    if not pending:
        flash("No gifts to claim.", "info")
        return redirect(url_for("gifts"))
    total = 0
    for gift in pending:
        gift.is_claimed = True
        gift.claimed_at = utcnow()
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
# Admin Panel
# ---------------------------------------------------------------------------
@app.route("/admin/give_coins", methods=["POST"])
@login_required
@admin_required
def admin_give_coins():
    username = request.form.get("username", "").strip()
    amount = request.form.get("amount", 0, type=int)
    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f"User '{username}' not found.", "danger")
        return redirect(url_for("admin_panel"))
    if amount <= 0:
        flash("Amount must be positive.", "warning")
        return redirect(url_for("admin_panel"))
    user.coins += amount
    db.session.commit()
    flash(f"Gave {amount} coins to {user.username}.", "success")
    return redirect(url_for("admin_panel"))
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
        user.is_banned = True
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
        starts_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=hours),
    )
    db.session.add(m)
    db.session.commit()
    flash(f"Multiplier '{name}' ({mult_value}x) active for {hours}h.", "success")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Context processor
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
# Market Metrics & Charts
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
        "market_metrics.html", sissy=sissy, snapshots=snapshots,
        completed_tasks=completed_tasks, total_invested=total_invested,
        investor_count=investor_count,
    )


@app.route("/admin/chart/growth.png")
@login_required
@admin_required
def admin_growth_chart():
    tf = request.args.get("tf", "30d")
    tf_map = {
        "1h": timedelta(hours=1), "6h": timedelta(hours=6),
        "1d": timedelta(days=1), "7d": timedelta(days=7),
        "30d": timedelta(days=30), "all": None,
    }
    window = tf_map.get(tf, timedelta(days=30))
    now = utcnow()
    cutoff = (now - window) if window else datetime(2000, 1, 1)

    if tf in ("1h", "6h"):
        bucket_fmt = "%Y-%m-%d %H:00"
    elif tf == "1d":
        bucket_fmt = "%Y-%m-%d %H:00"
    else:
        bucket_fmt = "%Y-%m-%d"

    def _dates(model, date_col="created_at"):
        col = getattr(model, date_col)
        return [r[0] for r in db.session.query(col).filter(col >= cutoff).all() if r[0]]

    user_dates = _dates(User)
    post_dates = _dates(Post)
    comment_dates = _dates(Comment)
    invest_dates = _dates(Investment)
    thread_dates = _dates(ForumThread)

    all_dates = sorted(set(
        [d.strftime(bucket_fmt) for d in user_dates] +
        [d.strftime(bucket_fmt) for d in post_dates] +
        [d.strftime(bucket_fmt) for d in comment_dates] +
        [d.strftime(bucket_fmt) for d in invest_dates] +
        [d.strftime(bucket_fmt) for d in thread_dates]
    ))

    if not all_dates:
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

    ax1.plot(date_index, users_cum, color="#38bdf8", linewidth=2.5, marker="o", markersize=3, label="Users")
    ax1.fill_between(date_index, users_cum, alpha=0.10, color="#38bdf8")
    ax1.plot(date_index, posts_cum, color="#ff4da6", linewidth=2, marker=".", markersize=3, label="Posts")
    ax1.fill_between(date_index, posts_cum, alpha=0.10, color="#ff4da6")
    ax1.set_ylabel("Cumulative Count", color="#aaa")
    ax1.legend(facecolor="#1a1033", edgecolor="#333", labelcolor="#ccc", loc="upper left")
    ax1.set_title(f"Site Growth — {tf_labels.get(tf, tf)}", color="#e0e0e0", fontsize=14, pad=10)

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

    tf = request.args.get("tf", "30d")
    tf_map = {
        "5m": timedelta(minutes=5), "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1), "6h": timedelta(hours=6),
        "1d": timedelta(days=1), "7d": timedelta(days=7),
        "30d": timedelta(days=30), "all": None,
    }
    window = tf_map.get(tf, timedelta(days=30))
    query = MarketSnapshot.query.filter_by(sissy_id=sissy.id)
    if window is not None:
        cutoff = utcnow() - window
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

    ax1.plot(df["date"], df["market_value"], color="#ff4da6", linewidth=2, marker="o", markersize=3, label="Market Value")
    ax1.fill_between(df["date"], df["market_value"], alpha=0.15, color="#ff4da6")
    ax1.plot(df["date"], df["xp"], color="#38bdf8", linewidth=1.5, linestyle="--", marker=".", markersize=3, label="XP")
    ax1.set_ylabel("Value", color="#aaa")
    ax1.legend(facecolor="#1a1033", edgecolor="#333", labelcolor="#ccc")
    ax1.set_title(f"{sissy.username} — {tf_labels.get(tf, tf)}", color="#e0e0e0", fontsize=14, pad=10)

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
# Admin Simulation Routes
# ---------------------------------------------------------------------------
@app.route("/admin/simulate", methods=["POST"])
@login_required
@admin_required
def admin_simulate():
    num_sissies = request.form.get("num_sissies", 5, type=int)
    num_masters = request.form.get("num_masters", 2, type=int)
    actions_min = request.form.get("actions_min", 3, type=int)
    actions_max = request.form.get("actions_max", 10, type=int)
    sim_minutes = request.form.get("sim_minutes", 0, type=int)

    num_sissies = max(0, min(num_sissies, 100))
    num_masters = max(0, min(num_masters, 50))
    actions_min = max(1, min(actions_min, 100))
    actions_max = max(actions_min, min(actions_max, 200))
    sim_minutes = max(0, min(sim_minutes, 1440))

    if sim_minutes > 0:
        started = start_timed_simulation(app, num_sissies, num_masters, actions_min, actions_max, sim_minutes)
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
    return jsonify(get_timed_sim_status())


@app.route("/admin/clear_bots", methods=["POST"])
@login_required
@admin_required
def admin_clear_bots():
    bots = User.query.filter_by(is_bot=True).all()
    bot_ids = [b.id for b in bots]
    if not bot_ids:
        flash("No bot accounts to remove.", "info")
        return redirect(url_for("admin_panel"))

    PendingGift.query.filter(PendingGift.sender_id.in_(bot_ids), PendingGift.is_claimed == False).delete(synchronize_session=False)
    claimed_bot_gifts = PendingGift.query.filter(PendingGift.sender_id.in_(bot_ids), PendingGift.is_claimed == True).all()
    for g in claimed_bot_gifts:
        recip = db.session.get(User, g.recipient_id)
        if recip:
            recip.coins = max(0, recip.coins - g.amount)
    PendingGift.query.filter(PendingGift.sender_id.in_(bot_ids)).delete(synchronize_session=False)
    PendingGift.query.filter(PendingGift.recipient_id.in_(bot_ids)).delete(synchronize_session=False)
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

    real_tasks_botted = Task.query.filter(
        ~Task.creator_id.in_(bot_ids), Task.assignee_id.in_(bot_ids),
    ).all()
    for task in real_tasks_botted:
        task.assignee_id = None
        task.status = "open"
        task.completed_at = None
    Task.query.filter(Task.creator_id.in_(bot_ids)).delete(synchronize_session=False)

    Notification.query.filter(
        db.or_(
            Notification.user_id.in_(bot_ids),
            Notification.message.like("%bot\\_s\\_%"),
            Notification.message.like("%bot\\_m\\_%"),
        )
    ).delete(synchronize_session=False)

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

    db.session.execute(likes_table.delete().where(likes_table.c.user_id.in_(bot_ids)))
    db.session.execute(followers_table.delete().where(
        db.or_(followers_table.c.follower_id.in_(bot_ids), followers_table.c.followed_id.in_(bot_ids))
    ))
    bot_posts = Post.query.filter(Post.author_id.in_(bot_ids)).all()
    for p in bot_posts:
        p.liked_by = []
        p.tags = []
    db.session.flush()
    Post.query.filter(Post.author_id.in_(bot_ids)).delete(synchronize_session=False)

    ForumReply.query.filter(ForumReply.author_id.in_(bot_ids)).delete(synchronize_session=False)
    bot_threads = ForumThread.query.filter(ForumThread.author_id.in_(bot_ids)).all()
    for t in bot_threads:
        t.tags = []
        ForumReply.query.filter_by(thread_id=t.id).delete(synchronize_session=False)
    db.session.flush()
    ForumThread.query.filter(ForumThread.author_id.in_(bot_ids)).delete(synchronize_session=False)

    User.query.filter(User.id.in_(bot_ids)).delete(synchronize_session=False)
    db.session.commit()

    flash(f"Removed {len(bot_ids)} bot accounts and reversed all effects on real accounts.", "warning")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
def seed_data():
    if User.query.first():
        return

    admin = User(username="AdminGG", email="admin@demo.gg", role="admin", coins=100,
                 bio="Platform administrator", avatar_url=generate_avatar("AdminGG"))
    admin.set_password("demo1234")
    db.session.add(admin)

    master = User(username="DomQueen", email="dom@demo.gg", role="master", coins=100,
                  bio="", avatar_url=generate_avatar("DomQueen"))
    master.set_password("demo1234")
    db.session.add(master)

    sissy = User(username="LaceyDream", email="laceydream@demo.gg", role="sissy",
                 bio="", avatar_url=generate_avatar("LaceyDream"))
    sissy.set_password("demo1234")
    db.session.add(sissy)

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
# New: Daily Reward Claim Route
# ---------------------------------------------------------------------------
from flask import session

@app.route("/claim_daily_reward", methods=["POST"])
@login_required
def claim_daily_reward():
    today = utcnow().date()
    already_claimed = DailyReward.query.filter_by(user_id=current_user.id, date=today).first()
    if already_claimed:
        flash("Already claimed today's reward!", "info")
        return redirect(url_for("feed"))
    streak, increased = current_user.record_streak("login")
    streak_day = min(streak.count, len(DAILY_REWARD_ROADMAP))
    reward = DAILY_REWARD_ROADMAP[streak_day-1] if streak_day > 0 else DAILY_REWARD_ROADMAP[0]
    coins_reward = reward["coins"]
    xp_reward = reward["xp"]
    if increased and streak.count in STREAK_BONUS_TABLE:
        bonus = STREAK_BONUS_TABLE[streak.count]
        coins_reward += bonus["coins"]
        xp_reward += bonus["xp"]
        notify(current_user.id, "streak_reward",
               f"Streak milestone! {streak.count}-day streak bonus: +{bonus['coins']} coins, +{bonus['xp']} XP!")
        flash(f"Streak milestone bonus! +{bonus['coins']} coins, +{bonus['xp']} XP!", "warning")
    current_user.coins += coins_reward
    current_user.add_points(xp_reward, reason="Daily login reward")
    db.session.add(DailyReward(user_id=current_user.id, date=today, coins_awarded=coins_reward, xp_awarded=xp_reward))
    db.session.add(Transaction(user_id=current_user.id, amount=coins_reward, description="Daily login reward"))
    db.session.commit()
    flash(f"Daily reward claimed: +{coins_reward} coins, +{xp_reward} XP!", "success")
    session["show_achievement_popup"] = f"Daily reward: +{coins_reward} coins, +{xp_reward} XP!"
    return redirect(url_for("feed"))

# ---------------------------------------------------------------------------
# New: Quick Quest Claim Route (placeholder logic)
# ---------------------------------------------------------------------------
@app.route("/claim_quest/<int:quest_id>", methods=["POST"])
@login_required
def claim_quest(quest_id):
    # Placeholder: always allow claim for demo
    coins = 10
    xp = 10
    current_user.coins += coins
    current_user.add_points(xp, reason="Quick quest reward")
    db.session.add(Transaction(user_id=current_user.id, amount=coins, description="Quick quest reward"))
    db.session.commit()
    flash(f"Quick quest completed! +{coins} coins, +{xp} XP!", "success")
    session["show_achievement_popup"] = f"Quick quest: +{coins} coins, +{xp} XP!"
    return redirect(url_for("feed"))

# ---------------------------------------------------------------------------
# GoodGurl Labs — Academy
# ---------------------------------------------------------------------------
@app.route("/academy")
@login_required
def academy():
    from constants import utcnow
    today = utcnow().strftime("%Y-%m-%d")

    # Generate today's lessons if not yet created
    generate_daily_lessons_for_date(today)

    show_welcome = False
    show_acceptance = False
    labs_accepted = True

    if current_user.is_authenticated:
        labs_accepted = current_user.labs_accepted
        if not session.get("labs_welcome_seen"):
            show_welcome = True
        if not labs_accepted:
            show_acceptance = True

    fem_lessons = Lesson.query.filter_by(track="feminization", is_active=True, date_key=today).order_by(Lesson.order).all()
    bimbo_lessons = Lesson.query.filter_by(track="bimbofication", is_active=True, date_key=today).order_by(Lesson.order).all()

    completed_ids = set()
    fem_journaled = False
    bimbo_journaled = False
    if current_user.is_authenticated:
        completed_ids = {
            lp.lesson_id for lp in LessonProgress.query.filter_by(
                user_id=current_user.id, completed=True
            ).all()
        }
        fem_journaled = JournalEntry.query.filter_by(user_id=current_user.id, date_key=today, track="feminization").first() is not None
        bimbo_journaled = JournalEntry.query.filter_by(user_id=current_user.id, date_key=today, track="bimbofication").first() is not None

    return render_template(
        "academy.html",
        fem_lessons=fem_lessons,
        bimbo_lessons=bimbo_lessons,
        completed_ids=completed_ids,
        show_welcome=show_welcome,
        show_acceptance=show_acceptance,
        labs_accepted=labs_accepted,
        today=today,
        fem_journaled=fem_journaled,
        bimbo_journaled=bimbo_journaled,
    )


@app.route("/academy/accept", methods=["POST"])
@login_required
def academy_accept():
    current_user.labs_accepted = True
    session["labs_welcome_seen"] = True
    db.session.commit()
    flash("Welcome to GoodGurl Labs! Your journey begins now.", "success")
    return redirect(url_for("academy"))


@app.route("/academy/dismiss-welcome", methods=["POST"])
@login_required
def academy_dismiss_welcome():
    session["labs_welcome_seen"] = True
    return "", 204


@app.route("/academy/complete/<int:lesson_id>", methods=["POST"])
@login_required
def academy_complete_lesson(lesson_id):
    from constants import utcnow
    lesson = Lesson.query.get_or_404(lesson_id)

    if not current_user.labs_accepted:
        flash("You must accept your calling before starting lessons.", "warning")
        return redirect(url_for("academy"))

    existing = LessonProgress.query.filter_by(user_id=current_user.id, lesson_id=lesson.id).first()
    if existing and existing.completed:
        flash("You already completed this lesson.", "info")
        return redirect(url_for("academy"))

    # Check prerequisite: previous lesson in same track + date must be completed
    if lesson.order > 1:
        prev = Lesson.query.filter_by(
            track=lesson.track, order=lesson.order - 1,
            is_active=True, date_key=lesson.date_key
        ).first()
        if prev:
            prev_prog = LessonProgress.query.filter_by(user_id=current_user.id, lesson_id=prev.id, completed=True).first()
            if not prev_prog:
                flash("Complete the previous lesson first!", "warning")
                return redirect(url_for("academy"))

    if not existing:
        existing = LessonProgress(user_id=current_user.id, lesson_id=lesson.id)
        db.session.add(existing)

    # Task (objective) lessons require photo proof
    proof_url = ""
    if lesson.lesson_type == "objective":
        file = request.files.get("proof_photo")
        if not file or not file.filename:
            flash("📸 You must upload a photo as proof of completing this task.", "warning")
            return redirect(url_for("academy"))
        if not allowed_file(file.filename):
            flash("File type not allowed. Accepted: png, jpg, jpeg, gif, webp.", "warning")
            return redirect(url_for("academy"))
        ext = file.filename.rsplit(".", 1)[1].lower()
        if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
            flash("Only image files are accepted as proof.", "warning")
            return redirect(url_for("academy"))
        safe_name = secrets.token_hex(16) + "." + ext
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], safe_name))
        proof_url = url_for("uploaded_file", filename=safe_name)

    existing.completed = True
    existing.completed_at = utcnow()
    if proof_url:
        existing.proof_photo = proof_url

    # Award rewards
    current_user.xp += lesson.reward_xp
    current_user.coins += lesson.reward_coins
    if lesson.reward_fp:
        current_user.fp += lesson.reward_fp
    if lesson.reward_bp:
        current_user.bp += lesson.reward_bp

    db.session.commit()
    flash(f"✅ Lesson complete! +{lesson.reward_xp} XP, +{lesson.reward_coins} coins"
          + (f", +{lesson.reward_fp} FP" if lesson.reward_fp else "")
          + (f", +{lesson.reward_bp} BP" if lesson.reward_bp else ""),
          "success")
    return redirect(url_for("academy"))


@app.route("/academy/reset/<int:lesson_id>", methods=["POST"])
@login_required
def academy_reset_lesson(lesson_id):
    from constants import utcnow
    lesson = Lesson.query.get_or_404(lesson_id)

    progress = LessonProgress.query.filter_by(
        user_id=current_user.id, lesson_id=lesson.id, completed=True
    ).first()
    if not progress:
        flash("This lesson hasn't been completed yet.", "warning")
        return redirect(url_for("academy"))

    # Don't allow reset if a later lesson in the same track/day depends on this one
    next_lesson = Lesson.query.filter_by(
        track=lesson.track, order=lesson.order + 1,
        is_active=True, date_key=lesson.date_key
    ).first()
    if next_lesson:
        next_prog = LessonProgress.query.filter_by(
            user_id=current_user.id, lesson_id=next_lesson.id, completed=True
        ).first()
        if next_prog:
            flash("Reset the later lessons first before resetting this one.", "warning")
            return redirect(url_for("academy"))

    # Don't allow reset if the track is already journaled for this day
    today = lesson.date_key
    journal = JournalEntry.query.filter_by(user_id=current_user.id, date_key=today, track=lesson.track).first()
    if journal:
        flash("Can't reset — today's journal has already been saved.", "warning")
        return redirect(url_for("academy"))

    # Reverse rewards
    current_user.xp = max(0, current_user.xp - lesson.reward_xp)
    current_user.coins = max(0, current_user.coins - lesson.reward_coins)
    if lesson.reward_fp:
        current_user.fp = max(0, current_user.fp - lesson.reward_fp)
    if lesson.reward_bp:
        current_user.bp = max(0, current_user.bp - lesson.reward_bp)

    db.session.delete(progress)
    db.session.commit()
    flash(f"🔄 Reset \"{lesson.title}\" — rewards reversed.", "info")
    return redirect(url_for("academy"))


@app.route("/academy/complete-day", methods=["POST"])
@login_required
def academy_complete_day():
    """Archive a track's completed lessons into the Lab Journal."""
    from constants import utcnow
    today = utcnow().strftime("%Y-%m-%d")
    track = request.form.get("track", "").strip()

    if track not in ("feminization", "bimbofication"):
        flash("Invalid track.", "danger")
        return redirect(url_for("academy"))

    # Ensure not already archived for this track
    existing = JournalEntry.query.filter_by(user_id=current_user.id, date_key=today, track=track).first()
    if existing:
        flash(f"{track.title()} track already saved for today!", "info")
        return redirect(url_for("academy"))

    lessons = Lesson.query.filter_by(track=track, is_active=True, date_key=today).all()
    if not lessons:
        flash("No lessons found for this track today.", "warning")
        return redirect(url_for("academy"))

    completed_progress = LessonProgress.query.filter(
        LessonProgress.user_id == current_user.id,
        LessonProgress.lesson_id.in_([l.id for l in lessons]),
        LessonProgress.completed == True,
    ).all()
    completed_ids = {lp.lesson_id for lp in completed_progress}

    # Verify all lessons in this track are completed
    if len(completed_ids) != len(lessons):
        flash(f"Complete all {track} lessons first.", "warning")
        return redirect(url_for("academy"))

    # Tally rewards
    total_xp = sum(l.reward_xp for l in lessons)
    total_coins = sum(l.reward_coins for l in lessons)
    total_fp = sum(l.reward_fp for l in lessons)
    total_bp = sum(l.reward_bp for l in lessons)

    # Create journal entry
    entry = JournalEntry(
        user_id=current_user.id,
        date_key=today,
        track=track,
        lessons_completed=len(lessons),
        fem_completed=len(lessons) if track == "feminization" else 0,
        bimbo_completed=len(lessons) if track == "bimbofication" else 0,
        total_xp_earned=total_xp,
        total_coins_earned=total_coins,
        total_fp_earned=total_fp,
        total_bp_earned=total_bp,
    )
    db.session.add(entry)
    db.session.flush()

    # Attach proof photos
    proof_map = {lp.lesson_id: lp for lp in completed_progress}
    for lesson in lessons:
        lp = proof_map.get(lesson.id)
        if lp and lp.proof_photo:
            photo = JournalPhoto(
                entry_id=entry.id,
                photo_url=lp.proof_photo,
                caption=lesson.objective_text or lesson.description,
                lesson_title=lesson.title,
            )
            db.session.add(photo)
            entry.photos.append(photo)

    # Optionally create a public photo album post
    share_public = request.form.get("share_album") == "1"
    if share_public and entry.photos:
        photos_with_captions = []
        for p in entry.photos:
            photos_with_captions.append(f"**{p.lesson_title}**\n{p.caption}")

        track_label = "Feminization" if track == "feminization" else "Bimbofication"
        album_body = (
            f"📓 **GoodGurl Lab Journal — {track_label} — {today}**\n\n"
            f"Completed {len(lessons)} {track} lessons today!\n\n"
            + "\n\n---\n\n".join(photos_with_captions)
        )
        first_photo_url = entry.photos[0].photo_url
        album_post = Post(
            author_id=current_user.id,
            title=f"Lab Journal — {track_label} — {today}",
            body=album_body,
            media_url=first_photo_url,
        )
        tag = Tag.query.filter_by(name="labjournal").first()
        if not tag:
            tag = Tag(name="labjournal")
            db.session.add(tag)
        album_post.tags.append(tag)
        db.session.add(album_post)
        db.session.flush()

        # Create PostMedia entries for each proof photo
        for idx, photo in enumerate(entry.photos):
            ext = photo.photo_url.rsplit(".", 1)[-1].lower() if "." in photo.photo_url else ""
            mtype = "gif" if ext == "gif" else "image"
            db.session.add(PostMedia(post_id=album_post.id, media_url=photo.photo_url, media_type=mtype, position=idx))

        entry.post_id = album_post.id

    # Award completion bonus — equal to the total earned from all lessons in the track
    # Double the bonus if sharing as a public album post
    bonus_mult = 2 if (share_public and entry.photos) else 1
    bonus_xp = total_xp * bonus_mult
    bonus_coins = total_coins * bonus_mult
    bonus_fp = total_fp * bonus_mult
    bonus_bp = total_bp * bonus_mult
    current_user.xp += bonus_xp
    current_user.coins += bonus_coins
    if bonus_fp:
        current_user.fp += bonus_fp
    if bonus_bp:
        current_user.bp += bonus_bp

    db.session.commit()
    track_label = "Feminization" if track == "feminization" else "Bimbofication"
    bonus_msg = f"🎉 Bonus: +{bonus_xp} XP, +{bonus_coins} coins"
    if bonus_fp:
        bonus_msg += f", +{bonus_fp} FP"
    if bonus_bp:
        bonus_msg += f", +{bonus_bp} BP"
    if share_public and entry.photos:
        flash(f"📓 {track_label} journal saved & photo album posted! {bonus_msg} (2x for sharing!)", "success")
    else:
        flash(f"📓 {track_label} track archived to your Lab Journal! {bonus_msg}", "success")
    return redirect(url_for("academy"))


# ---------------------------------------------------------------------------
# New: Help/FAQ and Legal Pages
# ---------------------------------------------------------------------------
@app.route("/help")
def help_page():
    return render_template("help.html")

@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html")

@app.route("/legal")
def legal_page():
    return render_template("legal.html")

# ---------------------------------------------------------------------------
# New: Onboarding Modal Logic (set session flag)
# ---------------------------------------------------------------------------
@app.before_request
def onboarding_check():
    if current_user.is_authenticated and not session.get("onboarded"):
        session["show_onboarding"] = True
        session["onboarded"] = True

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    with db.engine.connect() as conn:
        for col, default in [("profile_frame", "''"), ("profile_title", "''"), ("market_value_boost", "0")]:
            try:
                conn.execute(db.text(f"ALTER TABLE user ADD COLUMN {col} VARCHAR(60) DEFAULT {default}"))
                conn.commit()
            except Exception:
                conn.rollback()
        # Add is_verified column if missing
        try:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN is_verified BOOLEAN DEFAULT 0"))
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN is_banned BOOLEAN DEFAULT 0"))
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            conn.execute(db.text("UPDATE forum_category SET icon='bi-person-raised-hand' WHERE icon='bi-hand-wave'"))
            conn.commit()
        except Exception:
            conn.rollback()
        # Add fp/bp columns if missing
        for col in ["fp", "bp"]:
            try:
                conn.execute(db.text(f"ALTER TABLE user ADD COLUMN {col} INTEGER DEFAULT 0"))
                conn.commit()
            except Exception:
                conn.rollback()
        # Add labs_accepted column if missing
        try:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN labs_accepted BOOLEAN DEFAULT 0"))
            conn.commit()
        except Exception:
            conn.rollback()
        # Add daily lesson columns if missing
        for col, coltype, default in [("date_key", "VARCHAR(10)", "NULL"), ("paired_with", "INTEGER", "NULL")]:
            try:
                conn.execute(db.text(f"ALTER TABLE lessons ADD COLUMN {col} {coltype} DEFAULT {default}"))
                conn.commit()
            except Exception:
                conn.rollback()
        # Add proof_photo to lesson_progress if missing
        try:
            conn.execute(db.text("ALTER TABLE lesson_progress ADD COLUMN proof_photo VARCHAR(256) DEFAULT ''"))
            conn.commit()
        except Exception:
            conn.rollback()
    seed_data()
    seed_shop_items()
    seed_academy_lessons()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1", port=5000)
