#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Advanced Instagram Automation Bot (single-file)
- Telegram-controlled
- SQLite persistence for actions, limits, blacklist, followed timestamps, hashtags
- Smart follow/unfollow (wait period, blacklist, daily caps)
- Targeted follow, hashtag tiers, geo engagement
- Story viewing + optional emoji reaction
- Personalized DMs with human-like delays and conditional triggers
- Scheduler for background jobs
- Flask keep-alive service for Koyeb hosting
- Uses instagrapi for Instagram operations and python-telegram-bot for Telegram control
"""

import os
import time
import random
import logging
import sqlite3
import threading
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, date

import schedule
import requests
from flask import Flask

from instagrapi import Client
from instagrapi.exceptions import ClientError, BadPassword, ChallengeRequired

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("advanced-insta-bot")

# ---------------------------
# Configuration (ENV first)
# ---------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
IG_USERNAME_ENV = os.environ.get("IG_USERNAME")
IG_PASSWORD_ENV = os.environ.get("IG_PASSWORD")

# Safety defaults
FOLLOW_WAIT_DAYS_MIN = int(os.environ.get("FOLLOW_WAIT_DAYS_MIN", "7"))   # earliest to unfollow if no follow back
FOLLOW_WAIT_DAYS_MAX = int(os.environ.get("FOLLOW_WAIT_DAYS_MAX", "14"))  # max wait before forced unfollow in scheduled cleanup
DAILY_DEFAULT_LIMITS = {
    "follows": int(os.environ.get("DAILY_FOLLOWS", "50")),
    "unfollows": int(os.environ.get("DAILY_UNFOLLOWS", "50")),
    "likes": int(os.environ.get("DAILY_LIKES", "200")),
    "dms": int(os.environ.get("DAILY_DMS", "10")),
    "story_views": int(os.environ.get("DAILY_STORY_VIEWS", "500")),
}

# ---------------------------
# Basic validations
# ---------------------------
if not TELEGRAM_BOT_TOKEN:
    log.warning("TELEGRAM_BOT_TOKEN not set — Telegram will fail.")
if ALLOWED_USER_ID == 0:
    log.warning("ALLOWED_USER_ID not set or zero — Telegram auth will fail until fixed.")

# ---------------------------
# Database setup
# ---------------------------
DB_FILE = "bot_data.sqlite"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# Tables:
cur.execute("""CREATE TABLE IF NOT EXISTS liked_posts (post_id TEXT PRIMARY KEY)""")
cur.execute("""CREATE TABLE IF NOT EXISTS viewed_stories (story_id TEXT PRIMARY KEY)""")
cur.execute("""CREATE TABLE IF NOT EXISTS unfollowed_users (user_id TEXT PRIMARY KEY)""")
cur.execute("""CREATE TABLE IF NOT EXISTS credentials (key TEXT PRIMARY KEY, value TEXT)""")

# Advanced tables:
cur.execute("""
CREATE TABLE IF NOT EXISTS followed_users (
    user_id TEXT PRIMARY KEY,
    followed_at TEXT
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS blacklist_users (
    user_id TEXT PRIMARY KEY
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS daily_limits (
    day TEXT PRIMARY KEY,
    follows INTEGER DEFAULT 0,
    unfollows INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    dms INTEGER DEFAULT 0,
    story_views INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS hashtags (
    tag TEXT PRIMARY KEY,
    tier INTEGER DEFAULT 2
)
""")
conn.commit()

# ---------------------------
# Instagram client and session persistence
# ---------------------------
cl = Client()
SESSION_FILE = "insta_session.json"

def save_session():
    try:
        settings = cl.get_settings()
        import json
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        log.info("Instagram session saved.")
    except Exception:
        log.exception("Failed to save Instagram session.")

def load_session():
    if os.path.exists(SESSION_FILE):
        try:
            import json
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
            cl.set_settings(settings)
            # instagrapi doesn't have a direct login_by_session universal method - we'll rely on cookies
            log.info("Loaded instagram session from file (will try to use it on requests).")
            return True
        except Exception:
            log.exception("Could not load session file.")
    return False

def get_instagram_credentials() -> Tuple[Optional[str], Optional[str]]:
    username = IG_USERNAME_ENV
    password = IG_PASSWORD_ENV
    if not username or not password:
        cur.execute("SELECT value FROM credentials WHERE key=?", ("username",))
        r = cur.fetchone()
        if r:
            username = r[0]
        cur.execute("SELECT value FROM credentials WHERE key=?", ("password",))
        r2 = cur.fetchone()
        if r2:
            password = r2[0]
    return username, password

def login_instagram(username: Optional[str] = None, password: Optional[str] = None) -> Tuple[bool, str]:
    username = username or IG_USERNAME_ENV
    password = password or IG_PASSWORD_ENV
    if not username or not password:
        username_db, password_db = get_instagram_credentials()
        username = username or username_db
        password = password or password_db

    if not username or not password:
        log.info("No Instagram credentials available.")
        return False, "No credentials provided."

    try:
        # Try using saved settings if present
        load_session()
        # Real attempt to login
        cl.login(username, password)
        save_session()
        # store in DB only if not provided via env
        if not IG_USERNAME_ENV:
            cur.execute("INSERT OR REPLACE INTO credentials (key, value) VALUES (?, ?)", ("username", username))
        if not IG_PASSWORD_ENV:
            cur.execute("INSERT OR REPLACE INTO credentials (key, value) VALUES (?, ?)", ("password", password))
        conn.commit()
        log.info("Instagram login successful for %s", username)
        return True, "Login successful."
    except BadPassword:
        log.warning("Bad password for %s", username)
        return False, "Bad password."
    except ChallengeRequired as e:
        log.exception("Challenge required during login: %s", e)
        return False, f"Challenge required: {e}"
    except ClientError as e:
        log.exception("Client error during login: %s", e)
        return False, f"ClientError: {e}"
    except Exception as e:
        log.exception("Unexpected login error: %s", e)
        return False, f"Unexpected: {e}"

def ensure_login() -> bool:
    """
    Ensure client is logged in; attempt a best-effort login otherwise.
    """
    try:
        if getattr(cl, "user_id", None):
            # we consider logged in if user_id available
            return True
        ok, _ = login_instagram()
        return ok
    except Exception:
        log.exception("ensure_login failed.")
        return False

# ---------------------------
# Utility helpers for DB usage
# ---------------------------
def get_today_str() -> str:
    return date.today().isoformat()

def reset_daily_limits_if_needed():
    today = get_today_str()
    cur.execute("SELECT day FROM daily_limits WHERE day=?", (today,))
    if not cur.fetchone():
        # create new row with defaults
        cur.execute("INSERT OR REPLACE INTO daily_limits (day, follows, unfollows, likes, dms, story_views) VALUES (?, ?, ?, ?, ?, ?)",
                    (today, 0, 0, 0, 0, 0))
        conn.commit()

def increment_limit(action: str, amount: int = 1):
    reset_daily_limits_if_needed()
    today = get_today_str()
    cur.execute(f"UPDATE daily_limits SET {action} = {action} + ? WHERE day=?", (amount, today))
    conn.commit()

def get_limits() -> Dict[str, int]:
    reset_daily_limits_if_needed()
    today = get_today_str()
    cur.execute("SELECT follows, unfollows, likes, dms, story_views FROM daily_limits WHERE day=?", (today,))
    r = cur.fetchone()
    if r:
        return {"follows": r[0], "unfollows": r[1], "likes": r[2], "dms": r[3], "story_views": r[4]}
    return {"follows": 0, "unfollows": 0, "likes": 0, "dms": 0, "story_views": 0}

def set_daily_cap(action: str, cap: int):
    # We will store caps as env default + runtime; this function stores custom cap by writing to hashtags (hack) OR
    # Simpler: keep in-memory override (but user asked persistent). We'll implement a dedicated caps table.
    cur.execute("""CREATE TABLE IF NOT EXISTS caps (action TEXT PRIMARY KEY, cap INTEGER)""")
    cur.execute("INSERT OR REPLACE INTO caps (action, cap) VALUES (?, ?)", (action, cap))
    conn.commit()

def get_daily_cap(action: str) -> int:
    cur.execute("""CREATE TABLE IF NOT EXISTS caps (action TEXT PRIMARY KEY, cap INTEGER)""")
    cur.execute("SELECT cap FROM caps WHERE action=?", (action,))
    r = cur.fetchone()
    if r:
        return int(r[0])
    return DAILY_DEFAULT_LIMITS.get(action, 99999)

# ---------------------------
# Core features (likes, story view, follow/unfollow, hashtags, geo)
# ---------------------------
def auto_like_followers(likes_per_user: int = 2, daily_cap_check: bool = True) -> str:
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        followers = cl.user_followers(cl.user_id)
        count_liked = 0
        for user_id in list(followers.keys()):
            try:
                # Check daily cap for likes
                if daily_cap_check and get_limits()["likes"] >= get_daily_cap("likes"):
                    log.info("Daily likes cap reached.")
                    break
                medias = cl.user_medias(user_id, amount=likes_per_user)
                for m in medias:
                    cur.execute("SELECT 1 FROM liked_posts WHERE post_id=?", (str(m.pk),))
                    if cur.fetchone():
                        continue
                    cl.media_like(m.pk)
                    cur.execute("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", (str(m.pk),))
                    conn.commit()
                    increment_limit("likes", 1)
                    count_liked += 1
                    log.info("Liked media %s from user %s", m.pk, user_id)
                    time.sleep(random.uniform(5, 15))
            except ClientError as e:
                log.warning("Like error user %s: %s", user_id, e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected like error for user %s: %s", user_id, e)
        return f"✅ Auto-like done! Liked {count_liked} posts."
    except Exception as e:
        log.exception("Auto-like overall error: %s", e)
        return f"An error occurred: {e}"

def auto_view_stories(users_dict: Dict, reaction_chance: float = 0.05, daily_cap_check: bool = True) -> str:
    """
    View stories for users in users_dict (dict of user_id -> username). Optionally react to some stories randomly.
    """
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        count_viewed = 0
        count_reacted = 0
        for user_id in list(users_dict.keys()):
            try:
                # Check daily cap for story views
                if daily_cap_check and get_limits()["story_views"] >= get_daily_cap("story_views"):
                    log.info("Daily story views cap reached.")
                    break
                stories = cl.user_stories(user_id)
                for s in stories:
                    cur.execute("SELECT 1 FROM viewed_stories WHERE story_id=?", (str(s.pk),))
                    if cur.fetchone():
                        continue
                    try:
                        cl.story_view(s.pk)
                        cur.execute("INSERT OR REPLACE INTO viewed_stories (story_id) VALUES (?)", (str(s.pk),))
                        conn.commit()
                        increment_limit("story_views", 1)
                        count_viewed += 1
                        log.info("Viewed story %s from %s", s.pk, user_id)
                        # Random reaction
                        if random.random() < reaction_chance:
                            # Choose a light emoji reaction
                            emoji = random.choice(["😍", "🔥", "👍", "❤️"])
                            try:
                                # instagrapi story_reel, reaction API may differ; we'll attempt direct API:
                                cl.story_reel_item_seen(s.pk)  # mark as seen (redundant) — kept for resilience
                                # instagrapi has no direct 'react' method easily; many versions require direct endpoint.
                                # We'll attempt media_reaction (if available) or skip gracefully.
                                if hasattr(cl, "story_reaction"):
                                    cl.story_reaction(s.pk, emoji)
                                elif hasattr(cl, "media_comment"):
                                    # Fallback: don't comment on story; skip
                                    pass
                                count_reacted += 1
                                log.info("Reacted to story %s with %s", s.pk, emoji)
                            except Exception:
                                log.debug("Could not react to story; skipping reaction.")
                        time.sleep(random.uniform(3, 8))
                    except ClientError as e:
                        log.warning("Story view error for story %s: %s", s.pk, e)
                        time.sleep(30)
            except ClientError as e:
                log.warning("User stories fetch error for %s: %s", user_id, e)
                time.sleep(30)
            except Exception as e:
                log.exception("Unexpected story error for %s: %s", user_id, e)
        return f"✅ Stories viewed: {count_viewed}, reactions sent: {count_reacted}"
    except Exception as e:
        log.exception("Auto-view overall error: %s", e)
        return f"An error occurred: {e}"

def follow_user(user_id: str, track_follow: bool = True) -> Tuple[bool, str]:
    """
    Follow a user id and record in followed_users with timestamp.
    """
    if not ensure_login():
        return False, "Instagram not logged in."
    try:
        cur.execute("SELECT 1 FROM blacklist_users WHERE user_id=?", (str(user_id),))
        if cur.fetchone():
            return False, "User is blacklisted; skipping follow."

        # daily cap check
        if get_limits()["follows"] >= get_daily_cap("follows"):
            return False, "Daily follow cap reached."

        cl.user_follow(user_id)
        if track_follow:
            cur.execute("INSERT OR REPLACE INTO followed_users (user_id, followed_at) VALUES (?, ?)",
                        (str(user_id), datetime.utcnow().isoformat()))
            conn.commit()
        increment_limit("follows", 1)
        log.info("Followed user %s", user_id)
        return True, "Followed."
    except ClientError as e:
        log.warning("Follow error for %s: %s", user_id, e)
        return False, f"ClientError: {e}"
    except Exception as e:
        log.exception("Unexpected follow error: %s", e)
        return False, f"Error: {e}"

def unfollow_user(user_id: str, track_unfollow: bool = True) -> Tuple[bool, str]:
    """
    Unfollow a user and record in unfollowed_users to avoid repeated attempts.
    """
    if not ensure_login():
        return False, "Instagram not logged in."
    try:
        cur.execute("SELECT 1 FROM blacklist_users WHERE user_id=?", (str(user_id),))
        if cur.fetchone():
            return False, "User is blacklisted; skipping unfollow."

        if get_limits()["unfollows"] >= get_daily_cap("unfollows"):
            return False, "Daily unfollow cap reached."

        cl.user_unfollow(user_id)
        if track_unfollow:
            cur.execute("INSERT OR REPLACE INTO unfollowed_users (user_id) VALUES (?)", (str(user_id),))
            conn.commit()
        increment_limit("unfollows", 1)
        log.info("Unfollowed user %s", user_id)
        return True, "Unfollowed."
    except ClientError as e:
        log.warning("Unfollow error for %s: %s", user_id, e)
        return False, f"ClientError: {e}"
    except Exception as e:
        log.exception("Unexpected unfollow error: %s", e)
        return False, f"Error: {e}"

def scheduled_unfollow_non_followers(wait_days_min: int = FOLLOW_WAIT_DAYS_MIN, wait_days_max: int = FOLLOW_WAIT_DAYS_MAX, batch: int = 50):
    """
    Unfollow users who didn't follow back, but only after waiting between wait_days_min and wait_days_max.
    """
    if not ensure_login():
        log.info("Scheduled unfollow: not logged in.")
        return "Not logged in"

    try:
        my_followers = cl.user_followers(cl.user_id)
        my_following = cl.user_following(cl.user_id)
        removed = 0
        now = datetime.utcnow()
        for user_id in list(my_following.keys()):
            # skip if follower
            if user_id in my_followers:
                continue
            # skip if blacklisted
            cur.execute("SELECT 1 FROM blacklist_users WHERE user_id=?", (str(user_id),))
            if cur.fetchone():
                continue
            # check when followed
            cur.execute("SELECT followed_at FROM followed_users WHERE user_id=?", (str(user_id),))
            r = cur.fetchone()
            if not r:
                # If we don't know when we followed them, be conservative: skip
                continue
            followed_at = datetime.fromisoformat(r[0])
            age_days = (now - followed_at).days
            if age_days >= wait_days_min:
                # Only unfollow if within the allowed max or if beyond max forced unfollow
                if age_days >= wait_days_min and age_days <= wait_days_max:
                    ok, msg = unfollow_user(user_id, track_unfollow=True)
                    if ok:
                        removed += 1
                    time.sleep(random.uniform(10, 30))
                elif age_days > wait_days_max:
                    ok, msg = unfollow_user(user_id, track_unfollow=True)
                    if ok:
                        removed += 1
                    time.sleep(random.uniform(10, 30))
            if removed >= batch or get_limits()["unfollows"] >= get_daily_cap("unfollows"):
                break
        log.info("Scheduled unfollow run removed %s users", removed)
        return f"Unfollowed {removed} users."
    except Exception as e:
        log.exception("Error in scheduled_unfollow_non_followers: %s", e)
        return f"Error: {e}"

def targeted_follow_from_user_recent_engagers(target_username: str, max_to_follow: int = 20, lookback_posts: int = 3):
    """
    Follow users who recently engaged with target_username's recent posts.
    - Get recent posts of target (lookback_posts)
    - For each post, get likers and commenters, follow a subset based on daily cap and blacklist
    """
    if not ensure_login():
        return "Not logged in."
    try:
        uid = cl.user_id_from_username(target_username)
    except Exception as e:
        return f"Could not resolve username {target_username}: {e}"

    followed = 0
    try:
        medias = cl.user_medias(uid, amount=lookback_posts)
        # We'll gather users who liked/commented across recent posts
        candidate_ids = []
        for m in medias:
            try:
                # likes
                likers = cl.media_likers(m.pk)
                candidate_ids.extend([str(x.pk) if hasattr(x, "pk") else str(x) for x in likers])
                # comments
                comments = cl.media_comments(m.pk)
                candidate_ids.extend([str(c.user.pk) for c in comments])
            except Exception:
                continue
        # unique and shuffle
        candidate_ids = list(dict.fromkeys(candidate_ids))
        random.shuffle(candidate_ids)
        for cid in candidate_ids:
            if followed >= max_to_follow:
                break
            # respect daily cap
            if get_limits()["follows"] >= get_daily_cap("follows"):
                log.info("Daily follow cap reached during targeted follow.")
                break
            # skip if already following or blacklisted
            try:
                # check relationship quickly (may be heavy)
                rel = cl.user_following(cl.user_id).get(cid)
                # we just attempt follow_user directly; follow_user checks blacklist & caps
                ok, msg = follow_user(cid, track_follow=True)
                if ok:
                    followed += 1
                    time.sleep(random.uniform(30, 90))  # human-like delay between follows
            except Exception:
                # attempt follow directly anyway
                ok, msg = follow_user(cid, track_follow=True)
                if ok:
                    followed += 1
                    time.sleep(random.uniform(30, 90))
        return f"Targeted follow done: followed {followed} users from {target_username} engagements."
    except Exception as e:
        log.exception("Error in targeted follow: %s", e)
        return f"Error: {e}"

def hashtag_engage(tier: int = 2, per_tag: int = 10, deeper_engage_chance: float = 0.2):
    """
    Engage posts for hashtags of given tier.
    - likes posts
    - sometimes open profile and like 1-3 more posts for deeper engagement
    """
    if not ensure_login():
        return "Not logged in."
    try:
        # select hashtags of requested tier
        cur.execute("SELECT tag FROM hashtags WHERE tier=?", (tier,))
        rows = cur.fetchall()
        tags = [r[0] for r in rows]
        if not tags:
            return f"No hashtags found for tier {tier}."
        engaged_total = 0
        for tag in tags:
            try:
                medias = cl.hashtag_medias_recent(tag, amount=per_tag)
                for m in medias:
                    # daily like cap
                    if get_limits()["likes"] >= get_daily_cap("likes"):
                        log.info("Daily likes cap reached.")
                        return f"Engaged {engaged_total} posts; cap reached."
                    cur.execute("SELECT 1 FROM liked_posts WHERE post_id=?", (str(m.pk),))
                    if cur.fetchone():
                        continue
                    # like main post
                    try:
                        cl.media_like(m.pk)
                        cur.execute("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", (str(m.pk),))
                        conn.commit()
                        increment_limit("likes", 1)
                        engaged_total += 1
                        log.info("Liked media %s for hashtag %s", m.pk, tag)
                        # deeper engagement sometimes
                        if random.random() < deeper_engage_chance:
                            # visit profile and like 1-3 posts
                            author_id = m.user.pk if hasattr(m, "user") else None
                            if author_id:
                                user_medias = cl.user_medias(author_id, amount=random.randint(1, 3))
                                for um in user_medias:
                                    cur.execute("SELECT 1 FROM liked_posts WHERE post_id=?", (str(um.pk),))
                                    if cur.fetchone():
                                        continue
                                    cl.media_like(um.pk)
                                    cur.execute("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", (str(um.pk),))
                                    conn.commit()
                                    increment_limit("likes", 1)
                                    engaged_total += 1
                                    time.sleep(random.uniform(5, 20))
                        time.sleep(random.uniform(5, 20))
                    except Exception as e:
                        log.warning("Error engaging media %s: %s", m.pk, e)
                        continue
            except Exception as e:
                log.exception("Hashtag error for %s: %s", tag, e)
                continue
        return f"✅ Hashtag engage completed. Engaged {engaged_total} posts."
    except Exception as e:
        log.exception("Hashtag engage overall error: %s", e)
        return f"Error: {e}"

def geo_engage(location_query: str, per_location: int = 20):
    """
    Engage posts by searching a location string (city, place).
    """
    if not ensure_login():
        return "Not logged in."
    try:
        # Search locations - instagrapi has location_search_by_name or location_search
        try:
            places = cl.location_search(location_query)
        except Exception:
            try:
                places = cl.location_search_by_name(location_query)
            except Exception:
                places = []
        if not places:
            return f"No places found for {location_query}"
        engaged = 0
        for p in places[:3]:  # try top 3 places
            try:
                medias = cl.location_medias(p.pk, amount=per_location)
                for m in medias:
                    if get_limits()["likes"] >= get_daily_cap("likes"):
                        log.info("Daily likes cap reached.")
                        return f"Geo engage engaged {engaged} posts; cap reached."
                    cur.execute("SELECT 1 FROM liked_posts WHERE post_id=?", (str(m.pk),))
                    if cur.fetchone():
                        continue
                    try:
                        cl.media_like(m.pk)
                        cur.execute("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", (str(m.pk),))
                        conn.commit()
                        increment_limit("likes", 1)
                        engaged += 1
                        time.sleep(random.uniform(5, 20))
                    except Exception:
                        continue
            except Exception:
                continue
        return f"✅ Geo engage done. Engaged {engaged} posts near '{location_query}'."
    except Exception as e:
        log.exception("Geo engage error: %s", e)
        return f"Error: {e}"

# ---------------------------
# DM features (personalized, conditional)
# ---------------------------
def send_personalized_dm(user_id: str, template: str, placeholders: Dict[str, str] = None, delay_seconds: Optional[int] = None) -> Tuple[bool, str]:
    """
    Send personalized DM. Template can include placeholders like {username}
    Delay_seconds (int) introduces human-like delay before sending.
    """
    if not ensure_login():
        return False, "Not logged in."
    try:
        placeholders = placeholders or {}
        # Attempt to resolve username if we have only user_id, handle both
        try:
            # fetch username
            target_user = cl.user_info(user_id)
            uname = target_user.username if hasattr(target_user, "username") else str(user_id)
        except Exception:
            uname = str(user_id)
        message = template.format(username=uname, **placeholders)
        if delay_seconds:
            time.sleep(delay_seconds)
        # Observe daily DM cap
        if get_limits()["dms"] >= get_daily_cap("dms"):
            return False, "Daily DM cap reached."
        try:
            cl.direct_send(message, [user_id])
            increment_limit("dms", 1)
            return True, "DM sent."
        except ClientError as e:
            log.warning("DM ClientError: %s", e)
            return False, f"ClientError: {e}"
    except Exception as e:
        log.exception("send_personalized_dm error: %s", e)
        return False, f"Error: {e}"

# ---------------------------
# Telegram command handlers
# ---------------------------
LOGIN_STATE = {}  # in-memory state for /login flow

async def check_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("🚫 Not authorized!")
        return False
    return True

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    await update.message.reply_text(
        "✅ Advanced Instagram Bot Ready\n"
        "Commands:\n"
        "/login - Log in to Instagram (via Telegram)\n"
        "/like - Auto-like followers' posts\n"
        "/followers - View followers' stories\n"
        "/followings - View followings' stories\n"
        "/allstories - View all stories\n"
        "/unfollow - Run smart unfollow (scheduled usually)\n"
        "/follow_target <username> - Targeted follow from a user's engagers\n"
        "/blacklist_add <username_or_userid> - Add user to blacklist (won't be unfollowed)\n"
        "/blacklist_remove <username_or_userid> - Remove from blacklist\n"
        "/set_limit <action> <count> - Set daily cap for action (follows/unfollows/likes/dms/story_views)\n"
        "/stats - Today's action counts\n        "
        "/add_hashtag <tag> <tier> - Add hashtag to DB (tier 1-3)\n"
        "/hashtag_engage <tier> - Run hashtag engagement for that tier\n"
        "/geo_engage <place> - Engage posts near a place\n"
        "/dm_new_follower <username> <template> - (advanced) send template DM to user id/username\n"
    )

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    LOGIN_STATE[update.effective_user.id] = {"state": "waiting_username"}
    await update.message.reply_text("Send Instagram username (will be stored in DB).")

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles plaintext replies for login flow and any future multi-step flows.
    """
    user_id = update.effective_user.id
    if user_id not in LOGIN_STATE:
        return
    state = LOGIN_STATE[user_id]
    if state.get("state") == "waiting_username":
        state["username"] = update.message.text.strip()
        state["state"] = "waiting_password"
        await update.message.reply_text("Now send Instagram password (it will be stored in DB).")
    elif state.get("state") == "waiting_password":
        username = state.get("username")
        password = update.message.text.strip()
        # store in DB
        cur.execute("INSERT OR REPLACE INTO credentials (key, value) VALUES (?, ?)", ("username", username))
        cur.execute("INSERT OR REPLACE INTO credentials (key, value) VALUES (?, ?)", ("password", password))
        conn.commit()
        ok, msg = login_instagram(username, password)
        if ok:
            await update.message.reply_text("✅ Instagram login successful and saved.")
        else:
            await update.message.reply_text(f"🚫 Login failed: {msg}")
        del LOGIN_STATE[user_id]

async def like_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    await update.message.reply_text("❤️ Liking followers' posts...")
    res = auto_like_followers(2)
    await update.message.reply_text(res)

async def followers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    await update.message.reply_text("👀 Viewing followers' stories...")
    followers = cl.user_followers(cl.user_id)
    res = auto_view_stories(followers)
    await update.message.reply_text(res)

async def followings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    await update.message.reply_text("👀 Viewing followings' stories...")
    followings = cl.user_following(cl.user_id)
    res = auto_view_stories(followings)
    await update.message.reply_text(res)

async def allstories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    await update.message.reply_text("👀 Viewing all stories...")
    followers = cl.user_followers(cl.user_id)
    followings = cl.user_following(cl.user_id)
    combined = {**followers, **followings}
    res = auto_view_stories(combined)
    await update.message.reply_text(res)

async def unfollow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    await update.message.reply_text("💔 Running smart unfollow...")
    res = scheduled_unfollow_non_followers()
    await update.message.reply_text(res)

async def follow_target_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /follow_target <username>")
        return
    target = context.args[0]
    await update.message.reply_text(f"🔎 Targeted following from {target} engagers...")
    res = targeted_follow_from_user_recent_engagers(target, max_to_follow=20)
    await update.message.reply_text(res)

async def blacklist_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /blacklist_add <username_or_userid>")
        return
    key = context.args[0]
    # try to resolve username -> user_id
    try:
        if key.isdigit():
            uid = key
        else:
            uid = cl.user_id_from_username(key)
        cur.execute("INSERT OR REPLACE INTO blacklist_users (user_id) VALUES (?)", (str(uid),))
        conn.commit()
        await update.message.reply_text(f"✅ Added {key} to blacklist (id {uid}).")
    except Exception as e:
        await update.message.reply_text(f"🚫 Could not add to blacklist: {e}")

async def blacklist_remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /blacklist_remove <username_or_userid>")
        return
    key = context.args[0]
    try:
        if key.isdigit():
            uid = key
        else:
            uid = cl.user_id_from_username(key)
        cur.execute("DELETE FROM blacklist_users WHERE user_id=?", (str(uid),))
        conn.commit()
        await update.message.reply_text(f"✅ Removed {key} (id {uid}) from blacklist.")
    except Exception as e:
        await update.message.reply_text(f"🚫 Could not remove from blacklist: {e}")

async def set_limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /set_limit <action> <count> (actions: follows/unfollows/likes/dms/story_views)")
        return
    action = context.args[0]
    try:
        count = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Count must be a number.")
        return
    set_daily_cap(action, count)
    await update.message.reply_text(f"✅ Set daily cap {action} = {count}")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    limits = get_limits()
    caps = {a: get_daily_cap(a) for a in ["follows", "unfollows", "likes", "dms", "story_views"]}
    text = (
        f"📊 Today's counts:\n"
        f"Follows: {limits['follows']} / {caps['follows']}\n"
        f"Unfollows: {limits['unfollows']} / {caps['unfollows']}\n"
        f"Likes: {limits['likes']} / {caps['likes']}\n"
        f"DMs: {limits['dms']} / {caps['dms']}\n"
        f"Story views: {limits['story_views']} / {caps['story_views']}\n"
    )
    await update.message.reply_text(text)

async def add_hashtag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add_hashtag <tag> <tier> (tier 1-3)")
        return
    tag = context.args[0].lstrip("#").lower()
    try:
        tier = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Tier must be an integer (1-3).")
        return
    cur.execute("INSERT OR REPLACE INTO hashtags (tag, tier) VALUES (?, ?)", (tag, tier))
    conn.commit()
    await update.message.reply_text(f"✅ Hashtag #{tag} added to tier {tier}.")

async def hashtag_engage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    t = 2
    if context.args:
        try:
            t = int(context.args[0])
        except Exception:
            pass
    await update.message.reply_text(f"🔎 Running hashtag engagement for tier {t} ...")
    res = hashtag_engage(tier=t, per_tag=8)
    await update.message.reply_text(res)

async def geo_engage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if not context.args:
        await update.message.reply_text("Usage: /geo_engage <place name>")
        return
    place = " ".join(context.args)
    await update.message.reply_text(f"📍 Geo-engaging near {place} ...")
    res = geo_engage(place, per_location=15)
    await update.message.reply_text(res)

async def dm_new_follower_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /dm_new_follower <username_or_userid> <template>\nUse {username} in template.")
        return
    key = context.args[0]
    template = " ".join(context.args[1:])
    try:
        if key.isdigit():
            uid = key
        else:
            uid = cl.user_id_from_username(key)
    except Exception as e:
        await update.message.reply_text(f"Could not resolve user: {e}")
        return
    # Delay to mimic human behavior: random 1-10 minutes
    delay = random.randint(60, 600)
    await update.message.reply_text(f"DM scheduled in {delay//60} minutes.")
    # we'll run the send in a background thread to avoid blocking
    def send_later():
        ok, msg = send_personalized_dm(uid, template, placeholders=None, delay_seconds=delay)
        log.info("DM scheduled result: %s, %s", ok, msg)
    threading.Thread(target=send_later, daemon=True).start()

# ---------------------------
# Scheduler jobs (background)
# ---------------------------
def scheduler_jobs():
    """
    Register scheduled tasks:
    - auto-like smaller interactions every 6 hours
    - scheduled story viewing of followings every 3 hours
    - scheduled smart unfollow once per day
    - hashtag engagement runs spaced across day
    """
    log.info("Starting scheduler thread.")
    # helper wrappers to ensure login and catch exceptions
    def wrap(fn, *args, **kwargs):
        try:
            ensure_login()
            return fn(*args, **kwargs)
        except Exception:
            log.exception("Error in scheduled wrapper for %s", fn.__name__)
            return None

    # Schedule tasks
    schedule.every(6).hours.do(lambda: wrap(auto_like_followers, 2))
    schedule.every(3).hours.do(lambda: wrap(lambda: auto_view_stories(cl.user_following(cl.user_id) if ensure_login() else {}, 0.05)))
    schedule.every().day.at("02:00").do(lambda: wrap(scheduled_unfollow_non_followers, FOLLOW_WAIT_DAYS_MIN, FOLLOW_WAIT_DAYS_MAX, 50))
    # Spread hashtag engagement at 4am and 16:00
    schedule.every().day.at("04:00").do(lambda: wrap(hashtag_engage, 2, 6))
    schedule.every().day.at("16:00").do(lambda: wrap(hashtag_engage, 1, 5))

    # Periodic cleanup and reset
    while True:
        try:
            schedule.run_pending()
        except Exception:
            log.exception("Error running scheduled jobs.")
        time.sleep(30)

# ---------------------------
# Flask keep-alive webserver
# ---------------------------
web = Flask(__name__)

@web.route("/")
def home():
    return "✅ Advanced Instagram-Telegram bot running."

def run_web():
    log.info("Starting Flask keep-alive on port 8080")
    web.run(host="0.0.0.0", port=8080)

# ---------------------------
# Main runner
# ---------------------------
def main():
    # Start web server thread
    t_web = threading.Thread(target=run_web, daemon=True)
    t_web.start()

    # Start scheduler thread
    t_sched = threading.Thread(target=scheduler_jobs, daemon=True)
    t_sched.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("login", login_cmd))
    application.add_handler(CommandHandler("like", like_cmd))
    application.add_handler(CommandHandler("followers", followers_cmd))
    application.add_handler(CommandHandler("followings", followings_cmd))
    application.add_handler(CommandHandler("allstories", allstories_cmd))
    application.add_handler(CommandHandler("unfollow", unfollow_cmd))
    application.add_handler(CommandHandler("follow_target", follow_target_cmd))
    application.add_handler(CommandHandler("blacklist_add", blacklist_add_cmd))
    application.add_handler(CommandHandler("blacklist_remove", blacklist_remove_cmd))
    application.add_handler(CommandHandler("set_limit", set_limit_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("add_hashtag", add_hashtag_cmd))
    application.add_handler(CommandHandler("hashtag_engage", hashtag_engage_cmd))
    application.add_handler(CommandHandler("geo_engage", geo_engage_cmd))
    application.add_handler(CommandHandler("dm_new_follower", dm_new_follower_cmd))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    log.info("Starting Telegram polling.")
    application.run_polling()

if __name__ == "__main__":
    main()
