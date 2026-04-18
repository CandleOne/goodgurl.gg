import math
import os
import re
import random

from flask import current_app, url_for
from markupsafe import Markup, escape
from multiavatar.multiavatar import multiavatar as make_avatar
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from extensions import db
from constants import (
    utcnow, CHALLENGE_TEMPLATES, MULTIPLIER_STACK_STRATEGY, MIN_PASSWORD_LENGTH,
)
from models import (
    User, Post, Tag, Task, Investment, Transaction, Activity, PendingGift,
    Notification, Comment, Multiplier, Achievement, UserAchievement,
    Streak, StreakHistory, XPAudit, MarketSnapshot, DailyChallenge,
    UserDailyChallenge, ForumThread, ForumReply,
    likes_table, post_tags, followers_table,
)


# ---------------------------------------------------------------------------
# Avatar generation
# ---------------------------------------------------------------------------

def generate_avatar(seed):
    """Generate a Multiavatar SVG locally and save to static/avatars/."""
    avatar_dir = os.path.join(current_app.static_folder, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    svg_code = make_avatar(seed, None, None)
    filename = f"{seed}.svg"
    filepath = os.path.join(avatar_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(svg_code)
    return f"/static/avatars/{filename}"


# ---------------------------------------------------------------------------
# Hashtag helpers
# ---------------------------------------------------------------------------
_HASHTAG_RE = re.compile(r"#(\w{1,64})", re.UNICODE)


def extract_hashtags(text):
    return list(dict.fromkeys(m.lower() for m in _HASHTAG_RE.findall(text)))


def hashtagify_filter(text):
    """Convert #hashtag in text to clickable links. Auto-escapes HTML."""
    safe_text = str(escape(text))
    def _repl(m):
        tag_name = m.group(1).lower()
        return f'<a href="/hashtag/{tag_name}" class="text-pink text-decoration-none fw-bold">#{m.group(1)}</a>'
    return Markup(_HASHTAG_RE.sub(_repl, safe_text))


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def notify(user_id, type, message, link=""):
    n = Notification(user_id=user_id, type=type, message=message, link=link)
    db.session.add(n)
    return n


# ---------------------------------------------------------------------------
# Activity logging
# ---------------------------------------------------------------------------
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
    icon = _ACTIVITY_ICONS.get(action, "bi-activity")
    a = Activity(user_id=user_id, action=action, icon=icon, description=description)
    db.session.add(a)
    return a


# ---------------------------------------------------------------------------
# Market snapshots & dividends
# ---------------------------------------------------------------------------

def record_market_snapshot(sissy, timestamp=None):
    investor_count = Investment.query.filter_by(sissy_id=sissy.id).count()
    post_count = sissy.posts.count()
    task_count = Task.query.filter_by(assignee_id=sissy.id, status="completed").count()
    follower_count = sissy.followers.count()
    snap = MarketSnapshot(
        sissy_id=sissy.id, timestamp=timestamp or utcnow(),
        market_value=sissy.market_value, xp=sissy.xp,
        post_count=post_count, task_count=task_count,
        investor_count=investor_count, follower_count=follower_count,
    )
    db.session.add(snap)


def distribute_dividends(sissy, coins_earned):
    if coins_earned <= 0:
        return
    dividend_pool = max(1, int(coins_earned * 0.10))
    investments = Investment.query.filter_by(sissy_id=sissy.id).all()
    if not investments:
        return
    total_invested = sum(inv.amount for inv in investments)
    if total_invested <= 0:
        return
    sponsor_id = max(investments, key=lambda i: i.amount).investor_id
    # Batch-load all investors in a single query to avoid N+1
    investor_ids = [inv.investor_id for inv in investments]
    investors_by_id = {u.id: u for u in User.query.filter(User.id.in_(investor_ids)).all()}
    transactions = []
    for inv in investments:
        share = inv.amount / total_invested
        payout = max(1, int(dividend_pool * share))
        if inv.investor_id == sponsor_id and len(investments) > 1:
            payout = int(payout * 1.5)
        investor = investors_by_id.get(inv.investor_id)
        if investor:
            investor.coins += payout
            inv.total_dividends += payout
            transactions.append(Transaction(
                user_id=investor.id, amount=payout,
                description=f"Dividend from {sissy.username} ({coins_earned} earned)"
            ))
    db.session.add_all(transactions)


def get_sponsor(sissy):
    inv = Investment.query.filter_by(sissy_id=sissy.id).order_by(Investment.amount.desc()).first()
    if inv:
        return db.session.get(User, inv.investor_id)
    return None


# ---------------------------------------------------------------------------
# Achievement checker
# ---------------------------------------------------------------------------
_ACHIEVEMENT_RULES = None


def _get_achievement_rules():
    global _ACHIEVEMENT_RULES
    if _ACHIEVEMENT_RULES is not None:
        return _ACHIEVEMENT_RULES
    _ACHIEVEMENT_RULES = {}
    for ach in Achievement.query.all():
        _ACHIEVEMENT_RULES[ach.name] = {"id": ach.id, "xp_reward": ach.xp_reward}
    return _ACHIEVEMENT_RULES


def check_achievements(user):
    if user.role != "sissy":
        return
    rules = _get_achievement_rules()
    if not rules:
        return
    post_count = user.posts.count()
    task_count = Task.query.filter_by(assignee_id=user.id, status="completed").count()
    total_likes = db.session.query(db.func.count()).select_from(
        likes_table
    ).join(Post, likes_table.c.post_id == Post.id).filter(
        Post.author_id == user.id
    ).scalar() or 0
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
        ach_id = ach["id"]
        if user.has_achievement(ach_id):
            continue
        if earned:
            user.grant_achievement(ach_id, 100)
            if ach["xp_reward"]:
                user.add_points(ach["xp_reward"], reason=f"Achievement: {name}")
            notify(user.id, "achievement",
                   f"Achievement unlocked: {name}!",
                   "/account")
        else:
            user.increment_achievement_progress(ach_id, max(0, progress))


# ---------------------------------------------------------------------------
# Batch market value computation (avoids N+1 in leaderboard / market route)
# ---------------------------------------------------------------------------

def compute_market_values_batch(user_ids):
    """
    Compute market values for a list of user IDs using batch SQL queries
    instead of firing 7+ queries per user. Returns {user_id: market_value}.
    """
    if not user_ids:
        return {}
    user_ids = list(set(user_ids))

    post_counts = dict(
        db.session.query(Post.author_id, db.func.count(Post.id))
        .filter(Post.author_id.in_(user_ids))
        .group_by(Post.author_id).all()
    )
    like_counts = dict(
        db.session.query(Post.author_id, db.func.count(likes_table.c.user_id))
        .join(likes_table, likes_table.c.post_id == Post.id)
        .filter(Post.author_id.in_(user_ids))
        .group_by(Post.author_id).all()
    )
    comment_counts = dict(
        db.session.query(Post.author_id, db.func.count(Comment.id))
        .join(Comment, Comment.post_id == Post.id)
        .filter(Post.author_id.in_(user_ids))
        .group_by(Post.author_id).all()
    )
    task_counts = dict(
        db.session.query(Task.assignee_id, db.func.count(Task.id))
        .filter(Task.assignee_id.in_(user_ids), Task.status == "completed")
        .group_by(Task.assignee_id).all()
    )
    invest_rows = db.session.query(
        Investment.sissy_id,
        db.func.sum(Investment.amount),
        db.func.count(Investment.id),
    ).filter(Investment.sissy_id.in_(user_ids)).group_by(Investment.sissy_id).all()
    invest_data = {r[0]: (r[1] or 0, r[2] or 0) for r in invest_rows}

    achievement_counts = dict(
        db.session.query(UserAchievement.user_id, db.func.count(UserAchievement.id))
        .filter(UserAchievement.user_id.in_(user_ids), UserAchievement.progress >= 100)
        .group_by(UserAchievement.user_id).all()
    )
    follower_counts = dict(
        db.session.query(followers_table.c.followed_id, db.func.count(followers_table.c.follower_id))
        .filter(followers_table.c.followed_id.in_(user_ids))
        .group_by(followers_table.c.followed_id).all()
    )
    streaks_raw = Streak.query.filter(Streak.user_id.in_(user_ids)).all()
    streaks: dict = {}
    for s in streaks_raw:
        streaks.setdefault(s.user_id, {})[s.activity_name] = s.count

    latest_posts = dict(
        db.session.query(Post.author_id, db.func.max(Post.created_at))
        .filter(Post.author_id.in_(user_ids))
        .group_by(Post.author_id).all()
    )
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}

    now = utcnow()
    results = {}
    for uid in user_ids:
        u = users.get(uid)
        if not u:
            results[uid] = 0
            continue
        post_count = post_counts.get(uid, 0)
        total_likes = like_counts.get(uid, 0)
        total_comments = comment_counts.get(uid, 0)
        completed = task_counts.get(uid, 0)
        total_invested, investor_count = invest_data.get(uid, (0, 0))
        achievement_count = achievement_counts.get(uid, 0)
        follower_count = follower_counts.get(uid, 0)
        user_streaks = streaks.get(uid, {})
        login_streak = user_streaks.get("login", 0)
        post_streak = user_streaks.get("post", 0)
        streak_bonus = min(login_streak, 30) * 2 + min(post_streak, 14) * 3
        latest_dt = latest_posts.get(uid)
        recency = max(0.7, 1.0 - ((now - latest_dt).days * 0.01)) if latest_dt else 0.7
        engagement_rate = min((total_likes / post_count) if post_count > 0 else 0, 20)
        base = u.xp + (u.level - 1) * 10
        content = post_count * 5 + total_likes * 2 + total_comments * 3
        engagement = int(engagement_rate * 20)
        work = completed * 15
        social = follower_count * 8 + investor_count * 12
        consistency = streak_bonus + achievement_count * 5
        demand = int(math.log2(1 + total_invested) * 3)
        raw = base + content + engagement + work + social + consistency + demand
        results[uid] = max(0, int(raw * recency) + int(u.market_value_boost or 0))
    return results


# ---------------------------------------------------------------------------
# Daily challenge generation & progress
# ---------------------------------------------------------------------------

def generate_daily_challenges(date=None):
    date = date or utcnow().date()
    existing = DailyChallenge.query.filter_by(date=date).count()
    if existing >= 3:
        return
    rng = random.Random(str(date))
    picks = rng.sample(CHALLENGE_TEMPLATES, min(3, len(CHALLENGE_TEMPLATES)))
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
    today = utcnow().date()
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


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

def validate_password(password):
    """Return an error message if the password is weak, or None if OK."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if not re.search(r'[A-Z]', password):
        return "Password must contain at least one uppercase letter."
    if not re.search(r'[a-z]', password):
        return "Password must contain at least one lowercase letter."
    if not re.search(r'[0-9]', password):
        return "Password must contain at least one digit."
    return None


# ---------------------------------------------------------------------------
# Email verification tokens (itsdangerous)
# ---------------------------------------------------------------------------

def generate_verification_token(email):
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return s.dumps(email, salt='email-verify')


def verify_email_token(token, max_age=86400):
    """Verify a token and return the email, or None if invalid/expired."""
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        email = s.loads(token, salt='email-verify', max_age=max_age)
        return email
    except (BadSignature, SignatureExpired):
        return None


def generate_reset_token(email):
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return s.dumps(email, salt='password-reset')


def verify_reset_token(token, max_age=3600):
    """Verify a reset token and return the email, or None if invalid/expired (1h)."""
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        email = s.loads(token, salt='password-reset', max_age=max_age)
        return email
    except (BadSignature, SignatureExpired):
        return None
