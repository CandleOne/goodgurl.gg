import os
import re
import random
import secrets
import threading
import time as _time

from faker import Faker as _Faker
from werkzeug.security import generate_password_hash

from extensions import db
from constants import (
    utcnow, STARTING_LEVEL, LEVEL_CAP, LEVEL_CAP_POINTS_CONTINUE,
    MULTIPLIER_STACK_STRATEGY, TASK_REWARD_XP, POST_REWARD_XP,
    BOT_ARCHETYPES, BOT_MASTER_ARCHETYPES,
    POST_TEMPLATES, COMMENT_TEMPLATES, TASK_TEMPLATES,
    FORUM_TEMPLATES, FORUM_REPLY_TEMPLATES, FILL_WORDS,
)
from models import (
    User, Post, Tag, Task, Investment, Transaction, PendingGift,
    Notification, Comment, Multiplier, XPAudit, MarketSnapshot,
    ForumCategory, ForumThread, ForumReply, PostMedia,
    likes_table, post_tags, followers_table,
)
from helpers import generate_avatar, extract_hashtags, record_market_snapshot

_fake = _Faker()

# ---------------------------------------------------------------------------
# Bot Media Scanner
# ---------------------------------------------------------------------------
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".webm"}


def _scan_bot_media():
    base = os.path.dirname(__file__)
    photo_dir = os.path.join(base, "static", "bot_post_photo_bank")
    video_dir = os.path.join(base, "static", "assets", "bot_post_video_bank")
    gif_dir = os.path.join(base, "static", "assets", "bot_post_gif_bank")
    media = []
    if os.path.isdir(photo_dir):
        for f in os.listdir(photo_dir):
            if os.path.splitext(f)[1].lower() in _IMAGE_EXTS:
                media.append(f"/static/bot_post_photo_bank/{f}")
    if os.path.isdir(video_dir):
        for f in os.listdir(video_dir):
            if os.path.splitext(f)[1].lower() in _VIDEO_EXTS:
                media.append(f"/static/assets/bot_post_video_bank/{f}")
    if os.path.isdir(gif_dir):
        for f in os.listdir(gif_dir):
            if os.path.splitext(f)[1].lower() == ".gif":
                media.append(f"/static/assets/bot_post_gif_bank/{f}")
    return media


# ---------------------------------------------------------------------------
# Content Generation Helpers
# ---------------------------------------------------------------------------

def _fill_template(template, extra_vars=None):
    context = {}
    for key, values in FILL_WORDS.items():
        context[key] = random.choice(values)
    context["number"] = random.randint(3, 50)
    context["day_count"] = random.randint(1, 120)
    context["level"] = random.randint(5, 30)
    context["first_name"] = _fake.first_name_female()
    context["city"] = _fake.city()
    if extra_vars:
        context.update(extra_vars)
    if "{mention}" in template:
        context["mention"] = context.get("mention", "someone amazing")
    try:
        return template.format_map(context)
    except (KeyError, IndexError):
        return template


def _pick_archetype(archetypes_dict):
    names = list(archetypes_dict.keys())
    weights = [archetypes_dict[n]["weight"] for n in names]
    return random.choices(names, weights=weights, k=1)[0]


def _generate_username(role):
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
    tag = secrets.token_hex(3)
    prefix = "bot_s_" if role == "sissy" else "bot_m_"
    return f"{prefix}{tag}"


def _generate_bio(archetype_dict, archetype_name):
    templates = archetype_dict[archetype_name]["bio_templates"]
    return _fill_template(random.choice(templates))


def _generate_post(post_styles):
    style = random.choice(post_styles)
    templates = POST_TEMPLATES.get(style, POST_TEMPLATES["casual"])
    title_tpl, body_tpl = random.choice(templates)
    return _fill_template(title_tpl), _fill_template(body_tpl)


def _generate_comment(archetype_name):
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
    templates = COMMENT_TEMPLATES.get(style, COMMENT_TEMPLATES["casual"])
    return _fill_template(random.choice(templates))


def _generate_task(master_archetype):
    arch = BOT_MASTER_ARCHETYPES[master_archetype]
    style = random.choice(arch["task_styles"])
    templates = TASK_TEMPLATES.get(style, TASK_TEMPLATES["creative"])
    title, desc = random.choice(templates)
    return title, desc


def _generate_forum(style=None):
    if not style:
        style = random.choice(list(FORUM_TEMPLATES.keys()))
    templates = FORUM_TEMPLATES.get(style, FORUM_TEMPLATES["discussion"])
    title_tpl, body_tpl = random.choice(templates)
    return _fill_template(title_tpl), _fill_template(body_tpl)


def _generate_forum_reply():
    return _fill_template(random.choice(FORUM_REPLY_TEMPLATES))


# ---------------------------------------------------------------------------
# Social Graph Engine
# ---------------------------------------------------------------------------

def _build_social_graph(all_bots, sissy_bots, master_bots):
    graph = {}
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
            w = pop_score.get(other.id, 1)
            if getattr(bot, '_sim_archetype', '') == getattr(other, '_sim_archetype', ''):
                w *= 1.5
            if bot.role != other.role:
                w *= 1.3
            w *= random.uniform(0.5, 1.5)
            targets.append((other.id, w))
        total = sum(t[1] for t in targets) or 1
        targets = [(tid, tw / total) for tid, tw in targets]
        targets.sort(key=lambda x: -x[1])
        graph[bot.id] = targets
    return graph


def _pick_target(graph, bot_id, exclude_ids=None):
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
    summary = {
        "sissies_created": 0, "masters_created": 0,
        "posts": 0, "likes": 0, "comments": 0,
        "tasks_created": 0, "tasks_completed": 0,
        "investments": 0, "follows": 0, "gifts": 0,
        "forum_threads": 0, "forum_replies": 0,
    }

    now = utcnow()
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

    # --- Phase 0: Create bot accounts ---
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
    social_graph = _build_social_graph(all_bots, bot_sissies, bot_masters)

    all_sissies = User.query.filter_by(role="sissy").all()
    all_users = User.query.all()
    sissy_by_id = {u.id: u for u in all_sissies}
    user_by_id = {u.id: u for u in all_users}

    # --- Phase 1: Content creation ---
    bot_media_pool = _scan_bot_media()
    all_categories = ForumCategory.query.all()
    created_threads = []

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
            ext = media.rsplit('.', 1)[-1].lower() if '.' in media else ''
            mtype = 'video' if ext in ('mp4', 'webm') else ('gif' if ext == 'gif' else 'image')
            db.session.add(PostMedia(post_id=post.id, media_url=media, media_type=mtype, position=0))
            for tag_name in extract_hashtags(body + " " + title):
                post.tags.append(get_or_create_tag(tag_name))
            fast_add_points(bot, POST_REWARD_XP, reason=f"Bot post: {title[:40]}")
            summary["posts"] += 1
        snapshot_needed.add(bot.id)

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

        for bot in all_bots:
            if not created_threads:
                break
            for _ in range(random.randint(0, 3)):
                thread = random.choice(created_threads)
                reply_body = _generate_forum_reply()
                reply = ForumReply(thread_id=thread.id, author_id=bot.id, body=reply_body)
                thread.last_reply_at = utcnow()
                db.session.add(reply)
                for tag_name in extract_hashtags(reply_body):
                    existing = {t.name for t in thread.tags}
                    if tag_name not in existing:
                        thread.tags.append(get_or_create_tag(tag_name))
                if bot.role == "sissy":
                    fast_add_points(bot, 2, reason=f"Bot reply in: {thread.title[:30]}")
                    snapshot_needed.add(bot.id)
                summary["forum_replies"] += 1

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
    for bot in bot_sissies:
        if bot.level >= 5:
            bot.listed_on_market = True
    db.session.flush()

    # --- Phase 2: Interactions ---
    all_posts = Post.query.all()
    open_tasks = list(Task.query.filter_by(status="open").all())
    market_sissies = [s for s in all_sissies if s.listed_on_market and s.level >= 5]
    post_pop = {p.id: random.uniform(0.5, 2.0) for p in all_posts}

    def _pick_popular_post(posts_list):
        if not posts_list:
            return None
        weights = [post_pop.get(p.id, 1.0) for p in posts_list]
        return random.choices(posts_list, weights=weights, k=1)[0]

    for bot in all_bots:
        arch = (BOT_ARCHETYPES if bot.role == "sissy" else BOT_MASTER_ARCHETYPES).get(
            getattr(bot, '_sim_archetype', ''), {})

        # Likes
        for _ in range(random.randint(*arch.get("like_rate", (1, 5)))):
            post = _pick_popular_post(all_posts)
            if post:
                pair = (bot.id, post.id)
                if pair not in liked_pairs:
                    liked_pairs.add(pair)
                    pending_likes.append({"user_id": bot.id, "post_id": post.id})
                    post_pop[post.id] = post_pop.get(post.id, 1) + 0.3
                    if post.author_id != bot.id:
                        pending_notifications.append(dict(
                            user_id=post.author_id, type="like",
                            message=f"{bot.username} liked your post \"{post.title[:30]}\"",
                            link=f"/post/{post.id}",
                        ))
                    summary["likes"] += 1

        # Comments
        for _ in range(random.randint(*arch.get("comment_rate", (0, 2)))):
            post = _pick_popular_post(all_posts)
            if post:
                comment_text = _generate_comment(getattr(bot, '_sim_archetype', 'casual'))
                db.session.add(Comment(post_id=post.id, author_id=bot.id, body=comment_text))
                post_pop[post.id] = post_pop.get(post.id, 1) + 0.5
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

        # Follows
        already_followed = set()
        for _ in range(random.randint(*arch.get("follow_rate", (1, 3)))):
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
            for _ in range(random.randint(*arch.get("task_rate", (0, 1)))):
                candidates = [t for t in open_tasks if t.creator_id != bot.id and t.assignee_id is None]
                if candidates:
                    task = random.choice(candidates)
                    task.assignee_id = bot.id
                    task.status = "completed"
                    task.completed_at = utcnow()
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

        # Investments
        if (bot.role == "master" or getattr(bot, '_sim_archetype', '') == "veteran") and market_sissies:
            for _ in range(random.randint(*arch.get("invest_rate", (0, 2)))):
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

        # Gifts
        for _ in range(random.randint(*arch.get("gift_rate", (0, 1)))):
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
# Timed Simulation (background thread)
# ---------------------------------------------------------------------------
_timed_sim = {"running": False, "phase": "", "progress": 0, "total": 0, "summary": None, "error": None}


def get_timed_sim_status():
    return _timed_sim


def start_timed_simulation(flask_app, num_sissies, num_masters, actions_min, actions_max, sim_minutes):
    global _timed_sim
    if _timed_sim["running"]:
        return False
    _timed_sim.update(running=True, phase="Starting", progress=0, total=0, summary=None, error=None)

    def worker():
        global _timed_sim
        try:
            with flask_app.app_context():
                _run_timed_sim_inner(num_sissies, num_masters, actions_min, actions_max, sim_minutes)
        except Exception as exc:
            _timed_sim["error"] = str(exc)
        finally:
            _timed_sim["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return True


def _run_timed_sim_inner(num_sissies, num_masters, actions_min, actions_max, sim_minutes):
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

    now = utcnow()
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
            ext = media.rsplit('.', 1)[-1].lower() if '.' in media else ''
            mtype = 'video' if ext in ('mp4', 'webm') else ('gif' if ext == 'gif' else 'image')
            db.session.add(PostMedia(post_id=post.id, media_url=media, media_type=mtype, position=0))
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

    if created_threads:
        for bot in all_bots:
            for _ in range(random.randint(0, 3)):
                thread = random.choice(created_threads)
                reply_body = _generate_forum_reply()
                reply = ForumReply(thread_id=thread.id, author_id=bot.id, body=reply_body)
                thread.last_reply_at = utcnow()
                db.session.add(reply)
                for tag_name in extract_hashtags(reply_body):
                    existing = {t.name for t in thread.tags}
                    if tag_name not in existing:
                        thread.tags.append(get_or_create_tag(tag_name))
                if bot.role == "sissy":
                    fast_add_points(bot, 2, reason=f"Bot reply in: {thread.title[:30]}")
                summary["forum_replies"] += 1
        db.session.commit()

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
                task.completed_at = utcnow()
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
