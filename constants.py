import math
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Timezone-safe UTC helper (replaces deprecated datetime.utcnow)
# ---------------------------------------------------------------------------
def utcnow():
    """Return a naive UTC datetime using the non-deprecated API."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Levelling
# ---------------------------------------------------------------------------
STARTING_LEVEL = 1
LEVEL_CAP = 100
LEVEL_CAP_POINTS_CONTINUE = True


def xp_for_level(level):
    """XP needed to reach a given level. Gentle early curve, steep late game."""
    if level <= 1:
        return 0
    return int(15 * (level - 1) ** 1.7)


LEVEL_XP_TABLE = {lvl: xp_for_level(lvl) for lvl in range(1, LEVEL_CAP + 1)}

# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------
TIERS = [
    {"name": "Novice",      "level": 1,  "color": "#9ca3af", "icon": "game-icons:rose"},
    {"name": "Apprentice",  "level": 5,  "color": "#60a5fa", "icon": "game-icons:butterfly"},
    {"name": "Adept",       "level": 15, "color": "#a78bfa", "icon": "game-icons:crystal-shine"},
    {"name": "Expert",      "level": 30, "color": "#f472b6", "icon": "game-icons:fire-gem"},
    {"name": "Elite",       "level": 50, "color": "#fbbf24", "icon": "game-icons:diamond-trophy"},
    {"name": "Legend",       "level": 75, "color": "#f43f5e", "icon": "game-icons:queen-crown"},
]

# ---------------------------------------------------------------------------
# Multiplier stacking
# ---------------------------------------------------------------------------
MULTIPLIER_STACK_STRATEGY = "compound"  # compound | additive | highest

# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------
TASK_REWARD_XP = 25
TASK_REWARD_COINS = 10
POST_REWARD_XP = 10
LEADERBOARD_REWARD_INTERVAL_DAYS = 7
LEADERBOARD_TOP_REWARD_COINS = 500

# ---------------------------------------------------------------------------
# Streak & Daily Reward
# ---------------------------------------------------------------------------
STREAK_BONUS_TABLE = {
    3: {"coins": 5, "xp": 10},
    7: {"coins": 15, "xp": 30},
    14: {"coins": 30, "xp": 60},
    30: {"coins": 75, "xp": 150},
    60: {"coins": 150, "xp": 300},
    100: {"coins": 300, "xp": 600},
}

# Daily login reward roadmap: day -> {coins, xp}
DAILY_REWARD_ROADMAP = [
    {"coins": 2, "xp": 5},   # Day 1
    {"coins": 3, "xp": 7},   # Day 2
    {"coins": 4, "xp": 9},   # Day 3
    {"coins": 5, "xp": 12},  # Day 4
    {"coins": 7, "xp": 15},  # Day 5
    {"coins": 10, "xp": 20}, # Day 6
    {"coins": 15, "xp": 30}, # Day 7
]
DAILY_LOGIN_COINS = DAILY_REWARD_ROADMAP[0]["coins"]
DAILY_LOGIN_XP = DAILY_REWARD_ROADMAP[0]["xp"]

# ---------------------------------------------------------------------------
# Wallet / Shop
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

# ---------------------------------------------------------------------------
# Allowed upload extensions
# ---------------------------------------------------------------------------
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm"}

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
MIN_PASSWORD_LENGTH = 8

# ---------------------------------------------------------------------------
# Daily Challenge Templates
# ---------------------------------------------------------------------------
CHALLENGE_TEMPLATES = [
    ("Post Power", "Create {n} post(s) today", "posts", [1, 2, 3]),
    ("Task Grinder", "Complete {n} task(s) today", "tasks", [1, 2]),
    ("Social Butterfly", "Receive {n} like(s) on your posts today", "likes_received", [3, 5, 10]),
    ("Commentator", "Leave {n} comment(s) today", "comments", [2, 3, 5]),
    ("Forum Contributor", "Post {n} forum reply(ies) today", "forum_replies", [1, 2, 3]),
]


# ---------------------------------------------------------------------------
# Bot Personality & Content Generation System
# ---------------------------------------------------------------------------

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

POST_TEMPLATES = {
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

COMMENT_TEMPLATES = {
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

TASK_TEMPLATES = {
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

FORUM_TEMPLATES = {
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

FORUM_REPLY_TEMPLATES = [
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

FILL_WORDS = {
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
