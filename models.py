import math
from datetime import timedelta

from extensions import db
from flask_login import UserMixin
from constants import (
    STARTING_LEVEL, LEVEL_CAP, LEVEL_CAP_POINTS_CONTINUE, LEVEL_XP_TABLE,
    TIERS, MULTIPLIER_STACK_STRATEGY, utcnow,
)


# ---------------------------------------------------------------------------
# Single-use powerup inventory for users
# ---------------------------------------------------------------------------

class UserPowerup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("shop_item.id"), nullable=False)
    used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime, nullable=True)
    acquired_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship("User", backref="powerups")
    item = db.relationship("ShopItem")


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

likes_table = db.Table(
    "likes",
    db.Column("user_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("post_id", db.Integer, db.ForeignKey("post.id"), primary_key=True),
)

post_tags = db.Table(
    "post_tags",
    db.Column("post_id", db.Integer, db.ForeignKey("post.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)

thread_tags = db.Table(
    "thread_tags",
    db.Column("thread_id", db.Integer, db.ForeignKey("forum_thread.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)

followers_table = db.Table(
    "followers",
    db.Column("follower_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("followed_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)

blocked_users = db.Table(
    "blocked_users",
    db.Column("blocker_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("blocked_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False)  # 'sissy' or 'master'
    is_bot = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    bio = db.Column(db.Text, default="")
    avatar_url = db.Column(db.String(256), default="")
    coins = db.Column(db.Integer, default=0)
    funded_coins = db.Column(db.Integer, default=0)
    flair_color = db.Column(db.String(7), default="")
    xp = db.Column(db.Integer, default=0)
    level = db.Column(db.Integer, default=STARTING_LEVEL)
    show_wealth = db.Column(db.Boolean, default=False)
    listed_on_market = db.Column(db.Boolean, default=False)
    market_value_boost = db.Column(db.Integer, default=0)
    fp = db.Column(db.Integer, default=0)   # Feminization Power
    bp = db.Column(db.Integer, default=0)   # Bimbofication Power
    labs_accepted = db.Column(db.Boolean, default=False)
    profile_frame = db.Column(db.String(60), default="")
    profile_title = db.Column(db.String(60), default="")
    created_at = db.Column(db.DateTime, default=utcnow)

    # Relationships
    posts = db.relationship("Post", backref="author", lazy="dynamic", cascade="all, delete-orphan")
    tasks_created = db.relationship("Task", backref="creator", lazy="dynamic", foreign_keys="Task.creator_id", cascade="all, delete-orphan")
    tasks_assigned = db.relationship("Task", backref="assignee", lazy="dynamic", foreign_keys="Task.assignee_id")
    investments = db.relationship("Investment", backref="investor", lazy="dynamic", foreign_keys="Investment.investor_id", cascade="all, delete-orphan")
    liked_posts = db.relationship("Post", secondary=likes_table, backref=db.backref("liked_by", lazy="dynamic"), lazy="dynamic")
    achievements = db.relationship("UserAchievement", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    streaks = db.relationship("Streak", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    xp_audit = db.relationship("XPAudit", backref="user", lazy="dynamic", cascade="all, delete-orphan")

    from werkzeug.security import generate_password_hash, check_password_hash

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

    # ---- XP & Levelling ----

    def _get_active_multiplier(self):
        now = utcnow()
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
        if self.role != "sissy":
            return 0, False
        db_mult = self._get_active_multiplier()
        inline_mult = multiplier or 1.0
        if MULTIPLIER_STACK_STRATEGY == "compound":
            total_mult = db_mult * inline_mult
        elif MULTIPLIER_STACK_STRATEGY == "additive":
            total_mult = db_mult + inline_mult - 1.0
        else:
            total_mult = max(db_mult, inline_mult)
        final_amount = int(amount * total_mult)
        at_cap = self.level >= LEVEL_CAP
        if at_cap and not LEVEL_CAP_POINTS_CONTINUE:
            final_amount = 0
        old_level = self.level
        self.xp += final_amount
        db.session.add(XPAudit(
            user_id=self.id, amount=final_amount, original_amount=amount,
            multiplier_applied=round(total_mult, 2), reason=reason, audit_type="add",
        ))
        levelled_up = False
        if not at_cap:
            new_level = self._calculate_level()
            if new_level > old_level:
                self.level = min(new_level, LEVEL_CAP)
                levelled_up = True
        return final_amount, levelled_up

    def deduct_points(self, amount, reason=""):
        self.xp = max(0, self.xp - amount)
        db.session.add(XPAudit(
            user_id=self.id, amount=-amount, original_amount=amount,
            multiplier_applied=1.0, reason=reason, audit_type="deduct",
        ))
        self.level = max(STARTING_LEVEL, self._calculate_level())

    def _calculate_level(self):
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
        if self.level >= LEVEL_CAP:
            return 0
        return max(0, LEVEL_XP_TABLE[self.level + 1] - self.xp)

    @property
    def level_progress_pct(self):
        if self.level >= LEVEL_CAP:
            return 100
        current_threshold = LEVEL_XP_TABLE.get(self.level, 0)
        next_threshold = LEVEL_XP_TABLE.get(self.level + 1, current_threshold + 1)
        span = next_threshold - current_threshold
        if span <= 0:
            return 100
        progress = self.xp - current_threshold
        return min(100, int((progress / span) * 100))

    # ---- Tiers ----

    @property
    def tier(self):
        current = TIERS[0]
        for t in TIERS:
            if self.level >= t["level"]:
                current = t
        return current

    @property
    def rank(self):
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
        nt = self.get_next_tier()
        if nt is None:
            return 0
        return max(0, nt["level"] - self.level)

    def tier_progress(self):
        nt = self.get_next_tier()
        if nt is None:
            return 100
        current_tier_lvl = self.tier["level"]
        span = nt["level"] - current_tier_lvl
        if span <= 0:
            return 100
        return min(100, int(((self.level - current_tier_lvl) / span) * 100))

    # ---- Achievements ----

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

    # ---- Streaks ----

    def record_streak(self, activity_name):
        today = utcnow().date()
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        if streak:
            if streak.last_activity_date == today:
                return streak, False
            yesterday = today - timedelta(days=1)
            if streak.frozen_until and streak.frozen_until >= today:
                streak.last_activity_date = today
                return streak, False
            if streak.last_activity_date == yesterday:
                streak.count += 1
                streak.last_activity_date = today
                return streak, True
            else:
                if streak.count > 1:
                    db.session.add(StreakHistory(
                        user_id=self.id, activity_name=activity_name,
                        count=streak.count, started_at=streak.started_at,
                        ended_at=utcnow(),
                    ))
                streak.count = 1
                streak.started_at = utcnow()
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
        today = utcnow().date()
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        return streak is not None and streak.last_activity_date == today

    def freeze_streak(self, activity_name, days=1):
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        if streak:
            streak.frozen_until = (utcnow() + timedelta(days=days)).date()

    def unfreeze_streak(self, activity_name):
        streak = Streak.query.filter_by(user_id=self.id, activity_name=activity_name).first()
        if streak:
            streak.frozen_until = None

    # ---- Market value ----

    @property
    def market_value(self):
        post_count = self.posts.count()
        completed = Task.query.filter_by(assignee_id=self.id, status="completed").count()
        total_likes = 0
        total_comments = 0
        for p in self.posts.all():
            total_likes += p.liked_by.count()
            total_comments += p.comments.count()
        engagement_rate = min((total_likes / post_count) if post_count > 0 else 0, 20)
        investor_count = Investment.query.filter_by(sissy_id=self.id).count()
        total_invested = db.session.query(db.func.sum(Investment.amount)).filter_by(sissy_id=self.id).scalar() or 0
        achievement_count = UserAchievement.query.filter(
            UserAchievement.user_id == self.id,
            UserAchievement.progress >= 100
        ).count()
        login_streak = self.get_current_streak_count("login")
        post_streak = self.get_current_streak_count("post")
        streak_bonus = min(login_streak, 30) * 2 + min(post_streak, 14) * 3
        latest_post = self.posts.order_by(Post.created_at.desc()).first()
        if latest_post:
            days_since = (utcnow() - latest_post.created_at).days
            recency = max(0.7, 1.0 - (days_since * 0.01))
        else:
            recency = 0.7
        base = self.xp + (self.level - 1) * 10
        content = post_count * 5 + total_likes * 2 + total_comments * 3
        engagement = int(engagement_rate * 20)
        work = completed * 15
        social = self.followers.count() * 8 + investor_count * 12
        consistency = streak_bonus + achievement_count * 5
        demand = int(math.log2(1 + total_invested) * 3)
        raw = base + content + engagement + work + social + consistency + demand
        return max(0, int(raw * recency) + int(self.market_value_boost or 0))


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    media_url = db.Column(db.String(512), default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    is_boosted = db.Column(db.Boolean, default=False)
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
    reward_coins = db.Column(db.Integer, default=10)
    reward_xp = db.Column(db.Integer, default=25)
    status = db.Column(db.String(16), default="open")
    created_at = db.Column(db.DateTime, default=utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class Investment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    investor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sissy_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    bought_value = db.Column(db.Integer, nullable=False)
    total_dividends = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)
    sissy = db.relationship("User", foreign_keys=[sissy_id])


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=utcnow)


class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    action = db.Column(db.String(32), nullable=False)
    icon = db.Column(db.String(48), default="bi-activity")
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    user = db.relationship("User", backref=db.backref("activities", lazy="dynamic",
                           order_by="Activity.created_at.desc()"))


class PendingGift(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    is_claimed = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    claimed_at = db.Column(db.DateTime, nullable=True)
    sender = db.relationship("User", foreign_keys=[sender_id], backref="sent_gifts")
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref="received_gifts")


# ---------------------------------------------------------------------------
# Level-Up Models
# ---------------------------------------------------------------------------

class Multiplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    multiplier = db.Column(db.Float, nullable=False, default=2.0)
    is_active = db.Column(db.Boolean, default=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Achievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.Text, default="")
    icon = db.Column(db.String(64), default="bi-award")
    is_secret = db.Column(db.Boolean, default=False)
    xp_reward = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)


class UserAchievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    achievement_id = db.Column(db.Integer, db.ForeignKey("achievement.id"), nullable=False)
    progress = db.Column(db.Integer, default=0)
    earned_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    achievement = db.relationship("Achievement")

    @staticmethod
    def on_progress_update(mapper, connection, target):
        if target.progress >= 100 and target.earned_at is None:
            target.earned_at = utcnow()

db.event.listen(UserAchievement, "before_update", UserAchievement.on_progress_update)
db.event.listen(UserAchievement, "before_insert", UserAchievement.on_progress_update)


class Streak(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    activity_name = db.Column(db.String(64), nullable=False)
    count = db.Column(db.Integer, default=0)
    last_activity_date = db.Column(db.Date, nullable=True)
    frozen_until = db.Column(db.Date, nullable=True)
    started_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "activity_name"),)


class StreakHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    activity_name = db.Column(db.String(64), nullable=False)
    count = db.Column(db.Integer, nullable=False)
    started_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)


class XPAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    original_amount = db.Column(db.Integer, nullable=False)
    multiplier_applied = db.Column(db.Float, default=1.0)
    reason = db.Column(db.String(256), default="")
    audit_type = db.Column(db.String(16), default="add")
    created_at = db.Column(db.DateTime, default=utcnow)


class MarketSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sissy_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    market_value = db.Column(db.Integer, nullable=False)
    xp = db.Column(db.Integer, nullable=False)
    post_count = db.Column(db.Integer, nullable=False)
    task_count = db.Column(db.Integer, nullable=False)
    investor_count = db.Column(db.Integer, nullable=False)
    follower_count = db.Column(db.Integer, nullable=False, default=0)
    sissy = db.relationship("User", foreign_keys=[sissy_id])


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    type = db.Column(db.String(32), nullable=False)
    message = db.Column(db.String(512), nullable=False)
    link = db.Column(db.String(256), default="")
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    user = db.relationship("User", backref=db.backref("notifications", lazy="dynamic"))


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    author = db.relationship("User", backref=db.backref("comments", lazy="dynamic"))
    post = db.relationship("Post", backref=db.backref("comments", lazy="dynamic"))


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    sender = db.relationship("User", foreign_keys=[sender_id], backref=db.backref("messages_sent", lazy="dynamic"))
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref=db.backref("messages_received", lazy="dynamic"))


class DailyReward(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    coins_awarded = db.Column(db.Integer, default=0)
    xp_awarded = db.Column(db.Integer, default=0)
    __table_args__ = (db.UniqueConstraint("user_id", "date"),)


# ---------------------------------------------------------------------------
# Forum Models
# ---------------------------------------------------------------------------

class ForumCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    description = db.Column(db.Text, default="")
    icon = db.Column(db.String(64), default="bi-chat-dots")
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)
    threads = db.relationship("ForumThread", backref="category", lazy="dynamic",
                              order_by="ForumThread.is_pinned.desc(), ForumThread.last_reply_at.desc()")


class ForumThread(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("forum_category.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(256), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False)
    is_locked = db.Column(db.Boolean, default=False)
    view_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    last_reply_at = db.Column(db.DateTime, default=utcnow, index=True)
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
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("forum_thread.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    author = db.relationship("User", backref=db.backref("forum_replies", lazy="dynamic"))


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("forum_thread.id"), nullable=True)
    reason = db.Column(db.String(256), nullable=False)
    status = db.Column(db.String(16), default="open")
    created_at = db.Column(db.DateTime, default=utcnow)
    reporter = db.relationship("User", backref="reports_filed")
    post = db.relationship("Post", backref="reports")
    thread = db.relationship("ForumThread", backref="reports")


class DailyChallenge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    goal_type = db.Column(db.String(40), nullable=False)
    goal_count = db.Column(db.Integer, nullable=False, default=1)
    reward_coins = db.Column(db.Integer, nullable=False, default=5)
    reward_xp = db.Column(db.Integer, nullable=False, default=10)


class UserDailyChallenge(db.Model):
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
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(300), default="")
    category = db.Column(db.String(40), nullable=False)
    css_class = db.Column(db.String(60), default="")
    price = db.Column(db.Integer, nullable=False, default=50)
    level_required = db.Column(db.Integer, default=1)
    icon = db.Column(db.String(60), default="bi-bag")


class UserPurchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("shop_item.id"), nullable=False)
    purchased_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship("User", backref="purchases")
    item = db.relationship("ShopItem", backref="buyers")
    __table_args__ = (db.UniqueConstraint("user_id", "item_id"),)


# ---------------------------------------------------------------------------
# Late-bound relationships
# ---------------------------------------------------------------------------

User.followed = db.relationship(
    "User", secondary=followers_table,
    primaryjoin=(followers_table.c.follower_id == User.id),
    secondaryjoin=(followers_table.c.followed_id == User.id),
    backref=db.backref("followers", lazy="dynamic"), lazy="dynamic"
)

User.blocked = db.relationship(
    "User", secondary=blocked_users,
    primaryjoin=(blocked_users.c.blocker_id == User.id),
    secondaryjoin=(blocked_users.c.blocked_id == User.id),
    backref=db.backref("blocked_by", lazy="dynamic"), lazy="dynamic"
)


# ---------------------------------------------------------------------------
# Academy — Lesson Tracks
# ---------------------------------------------------------------------------

class Lesson(db.Model):
    __tablename__ = "lessons"
    id = db.Column(db.Integer, primary_key=True)
    track = db.Column(db.String(20), nullable=False, index=True)  # 'feminization' or 'bimbofication'
    order = db.Column(db.Integer, nullable=False, default=0)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, default="")
    lesson_type = db.Column(db.String(16), nullable=False, default="objective")  # 'audio' or 'objective'
    audio_url = db.Column(db.String(256), default="")
    objective_text = db.Column(db.Text, default="")
    reward_xp = db.Column(db.Integer, default=10)
    reward_coins = db.Column(db.Integer, default=5)
    reward_fp = db.Column(db.Integer, default=0)
    reward_bp = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    date_key = db.Column(db.String(10), nullable=True, index=True)  # 'YYYY-MM-DD' for daily lessons
    paired_with = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=True)  # task linked to audio
    created_at = db.Column(db.DateTime, default=utcnow)

    progress = db.relationship("LessonProgress", backref="lesson", lazy="dynamic", cascade="all, delete-orphan")


class LessonProgress(db.Model):
    __tablename__ = "lesson_progress"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False, index=True)
    completed = db.Column(db.Boolean, default=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    proof_photo = db.Column(db.String(256), default="")

    __table_args__ = (db.UniqueConstraint("user_id", "lesson_id", name="uq_user_lesson"),)

    user = db.relationship("User", backref=db.backref("lesson_progress", lazy="dynamic"))


# ---------------------------------------------------------------------------
# GoodGurl Lab Journal – archives daily completed sessions
# ---------------------------------------------------------------------------

journal_photos = db.Table(
    "journal_photos",
    db.Column("entry_id", db.Integer, db.ForeignKey("journal_entry.id"), primary_key=True),
    db.Column("photo_id", db.Integer, db.ForeignKey("journal_photo.id"), primary_key=True),
)


class JournalEntry(db.Model):
    __tablename__ = "journal_entry"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    date_key = db.Column(db.String(10), nullable=False)  # 'YYYY-MM-DD'
    fem_completed = db.Column(db.Integer, default=0)
    bimbo_completed = db.Column(db.Integer, default=0)
    total_xp_earned = db.Column(db.Integer, default=0)
    total_coins_earned = db.Column(db.Integer, default=0)
    total_fp_earned = db.Column(db.Integer, default=0)
    total_bp_earned = db.Column(db.Integer, default=0)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True)  # optional album post
    created_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "date_key", name="uq_user_journal_date"),)

    user = db.relationship("User", backref=db.backref("journal_entries", lazy="dynamic"))
    post = db.relationship("Post", backref="journal_entry")
    photos = db.relationship("JournalPhoto", secondary=journal_photos, backref="entries", lazy="select")


class JournalPhoto(db.Model):
    __tablename__ = "journal_photo"
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entry.id"), nullable=False)
    photo_url = db.Column(db.String(256), nullable=False)
    caption = db.Column(db.Text, default="")
    lesson_title = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=utcnow)
