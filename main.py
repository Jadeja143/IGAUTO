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
import asyncio
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, date

import schedule
import requests
from flask import Flask

from instagrapi import Client
from instagrapi.exceptions import ClientError, PleaseWaitFewMinutes, ClientUnauthorizedError, LoginRequired, BadPassword, ChallengeRequired
from pydantic import ValidationError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("advanced-insta-bot")

# SECURITY: Disable HTTP request logging to prevent token exposure
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING) 
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ---------------------------
# Validation error handler for Instagram API changes
# ---------------------------
def fix_media_validation_data(data):
    """Fix media data that causes Pydantic validation errors."""
    if isinstance(data, dict):
        # Fix image_versions2.candidates issues
        if 'image_versions2' in data and data['image_versions2']:
            if 'candidates' in data['image_versions2']:
                for candidate in data['image_versions2']['candidates']:
                    # Add missing fields that Instagram API sometimes omits
                    if 'scans_profile' not in candidate:
                        candidate['scans_profile'] = {}
                    if 'estimated_scans_sizes' not in candidate:
                        candidate['estimated_scans_sizes'] = []
        
        # Fix clips_metadata issues
        if 'clips_metadata' in data and data['clips_metadata']:
            clips = data['clips_metadata']
            if 'audio_ranking_info' not in clips or clips['audio_ranking_info'] is None:
                clips['audio_ranking_info'] = {}
            if 'original_sound_info' not in clips or clips['original_sound_info'] is None:
                clips['original_sound_info'] = {}
            if 'mashup_info' not in clips or clips['mashup_info'] is None:
                clips['mashup_info'] = {}
        
        # Fix other common missing fields
        if 'location' in data and data['location'] is None:
            data['location'] = {}
            
        # Fix carousel_media if present
        if 'carousel_media' in data and data['carousel_media']:
            for carousel_item in data['carousel_media']:
                fix_media_validation_data(carousel_item)  # Recursive fix
    
    return data

def safe_extract_media(extract_func, data):
    """Safely extract media with validation error handling."""
    try:
        return extract_func(data)
    except ValidationError as e:
        if 'scans_profile' in str(e) or 'estimated_scans_sizes' in str(e):
            # Fix the data and try again
            fixed_data = fix_media_validation_data(data)
            return extract_func(fixed_data)
        else:
            raise e

# ---------------------------
# Configuration (ENV first)
# ---------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))  # New admin user ID
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
if ADMIN_USER_ID == 0:
    log.warning("ADMIN_USER_ID not set or zero — Admin features will not work.")

# ---------------------------
# Database setup
# ---------------------------
DB_FILE = "bot_data.sqlite"
# SECURITY: Thread-safe database connection using context manager
db_lock = threading.Lock()

def get_db_connection():
    """Get a thread-safe database connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    # Enable WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    # Enable foreign key constraints
    conn.execute("PRAGMA foreign_keys=ON")
    # Optimize for faster writes
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def execute_db(query: str, params: Tuple = ()):
    """Execute database query safely with proper connection handling."""
    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            conn.commit()
            return cur.fetchall()

def fetch_db(query: str, params: Tuple = ()):
    """Fetch data from database safely."""
    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return cur.fetchall()

# Initialize tables with safe connection
with get_db_connection() as conn:
    cur = conn.cursor()
    # Existing tables:
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
    
    # New tables for admin access control
    cur.execute("""
    CREATE TABLE IF NOT EXISTS authorized_users (
        user_id TEXT PRIMARY KEY,
        username TEXT,
        authorized_at TEXT,
        authorized_by TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS access_requests (
        user_id TEXT PRIMARY KEY,
        username TEXT,
        requested_at TEXT,
        status TEXT DEFAULT 'pending'
    )
    """)
    
    # New tables for location and hashtag management
    cur.execute("""
    CREATE TABLE IF NOT EXISTS locations (
        location TEXT PRIMARY KEY,
        added_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS default_hashtags (
        hashtag TEXT PRIMARY KEY,
        added_at TEXT
    )
    """)
    
    conn.commit()

# ---------------------------
# Admin access control functions
# ---------------------------
def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    if user_id == ADMIN_USER_ID:
        return True
    result = fetch_db("SELECT 1 FROM authorized_users WHERE user_id=?", (str(user_id),))
    return bool(result)

def request_access(user_id: int, username: str) -> str:
    """Request access to the bot."""
    # Check if already authorized
    if is_authorized(user_id):
        return "You are already authorized to use this bot."
    
    # Check if request already exists
    result = fetch_db("SELECT status FROM access_requests WHERE user_id=?", (str(user_id),))
    if result:
        status = result[0][0]
        if status == 'pending':
            return "Your access request is already pending approval."
        elif status == 'denied':
            return "Your access request was denied. Contact the admin for more information."
    
    # Create new request
    execute_db("INSERT OR REPLACE INTO access_requests (user_id, username, requested_at, status) VALUES (?, ?, ?, ?)",
               (str(user_id), username, datetime.now().isoformat(), 'pending'))
    
    return "Access request submitted. Please wait for admin approval."

def approve_access(user_id: str, admin_id: int) -> str:
    """Approve user access request."""
    # Get request info
    result = fetch_db("SELECT username FROM access_requests WHERE user_id=? AND status='pending'", (user_id,))
    if not result:
        return "No pending request found for this user."
    
    username = result[0][0]
    
    # Add to authorized users
    execute_db("INSERT OR REPLACE INTO authorized_users (user_id, username, authorized_at, authorized_by) VALUES (?, ?, ?, ?)",
               (user_id, username, datetime.now().isoformat(), str(admin_id)))
    
    # Update request status
    execute_db("UPDATE access_requests SET status='approved' WHERE user_id=?", (user_id,))
    
    return f"Access approved for user @{username} (ID: {user_id})"

def deny_access(user_id: str) -> str:
    """Deny user access request."""
    execute_db("UPDATE access_requests SET status='denied' WHERE user_id=?", (user_id,))
    return f"Access denied for user ID: {user_id}"

def list_pending_requests() -> List[Tuple[str, str, str]]:
    """Get list of pending access requests."""
    return fetch_db("SELECT user_id, username, requested_at FROM access_requests WHERE status='pending' ORDER BY requested_at")

# ---------------------------
# Location management functions
# ---------------------------
def add_location(location: str) -> str:
    """Add a location to the default locations list."""
    location = location.lower().strip()
    execute_db("INSERT OR REPLACE INTO locations (location, added_at) VALUES (?, ?)",
               (location, datetime.now().isoformat()))
    return f"✅ Added location: {location}"

def remove_location(location: str) -> str:
    """Remove a location from the default locations list."""
    location = location.lower().strip()
    execute_db("DELETE FROM locations WHERE location=?", (location,))
    return f"✅ Removed location: {location}"

def list_locations() -> str:
    """List all stored locations."""
    locations = fetch_db("SELECT location FROM locations ORDER BY location")
    if not locations:
        return "📍 No locations configured."
    
    result = "📍 Configured locations:\n"
    for location in locations:
        result += f"  • {location[0]}\n"
    return result

def get_default_locations() -> List[str]:
    """Get list of default locations."""
    locations = fetch_db("SELECT location FROM locations ORDER BY location")
    return [loc[0] for loc in locations]

# ---------------------------
# Default hashtags management functions
# ---------------------------
def add_default_hashtag(hashtag: str) -> str:
    """Add a hashtag to the default hashtags list."""
    hashtag = hashtag.lower().strip().replace("#", "")
    execute_db("INSERT OR REPLACE INTO default_hashtags (hashtag, added_at) VALUES (?, ?)",
               (hashtag, datetime.now().isoformat()))
    return f"✅ Added default hashtag: #{hashtag}"

def remove_default_hashtag(hashtag: str) -> str:
    """Remove a hashtag from the default hashtags list."""
    hashtag = hashtag.lower().strip().replace("#", "")
    execute_db("DELETE FROM default_hashtags WHERE hashtag=?", (hashtag,))
    return f"✅ Removed default hashtag: #{hashtag}"

def list_default_hashtags() -> str:
    """List all stored default hashtags."""
    hashtags = fetch_db("SELECT hashtag FROM default_hashtags ORDER BY hashtag")
    if not hashtags:
        return "🏷️ No default hashtags configured."
    
    result = "🏷️ Default hashtags:\n"
    for hashtag in hashtags:
        result += f"  • #{hashtag[0]}\n"
    return result

def get_default_hashtags() -> List[str]:
    """Get list of default hashtags."""
    hashtags = fetch_db("SELECT hashtag FROM default_hashtags ORDER BY hashtag")
    return [tag[0] for tag in hashtags]

# ---------------------------
# Background task execution
# ---------------------------
async def run_in_background(func, *args, **kwargs):
    """Run a function in background thread and return result."""
    loop = asyncio.get_event_loop()
    import functools
    partial_func = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial_func)

async def run_instagram_task(update, task_name: str, func, *args, **kwargs):
    """Run an Instagram task in background and report progress."""
    try:
        await update.message.reply_text(f"🔄 Starting {task_name}...")
        result = await run_in_background(func, *args, **kwargs)
        await update.message.reply_text(result)
    except Exception as e:
        log.exception(f"Background task {task_name} failed: %s", e)
        await update.message.reply_text(f"❌ {task_name} failed: {e}")

# ---------------------------
# Instagram client and session persistence (with Pydantic fix)
# ---------------------------
cl = Client()
# Set proper delays to mimic human behavior
cl.delay_range = [2, 7]
SESSION_FILE = "insta_session.json"
# Thread-safe client lock to prevent race conditions
client_lock = threading.Lock()

# Rate limiting - token bucket system
class TokenBucket:
    def __init__(self, capacity, refill_rate, refill_interval=60):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.refill_interval = refill_interval
        self.last_refill = time.time()
        self.lock = threading.Lock()
    
    def consume(self, tokens=1):
        """Try to consume tokens. Returns True if successful."""
        with self.lock:
            now = time.time()
            # Refill tokens based on time passed
            if now - self.last_refill >= self.refill_interval:
                intervals_passed = (now - self.last_refill) / self.refill_interval
                self.tokens = min(self.capacity, self.tokens + (self.refill_rate * intervals_passed))
                self.last_refill = now
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

# Rate limiting buckets per category (requests per hour)
rate_buckets = {
    'read': TokenBucket(60, 60, 3600),    # 60 req/hour for reads (followers, following, etc.)
    'like': TokenBucket(40, 40, 3600),    # 40 req/hour for likes
    'follow': TokenBucket(15, 15, 3600),  # 15 req/hour for follows
    'story': TokenBucket(120, 120, 3600), # 120 req/hour for story views
    'search': TokenBucket(30, 30, 3600),  # 30 req/hour for hashtag/location search
}

# Cooldown tracking
cooldowns = {}

# Fix for Pydantic validation error
def safe_media_info(media_pk):
    """Safely get media info with proper error handling for missing fields."""
    try:
        media = cl.media_info(media_pk)
        # Handle missing scans_profile in image_versions2.candidates
        if hasattr(media, 'image_versions2') and media.image_versions2:
            if hasattr(media.image_versions2, 'candidates') and media.image_versions2.candidates:
                for candidate in media.image_versions2.candidates:
                    if not hasattr(candidate, 'scans_profile') or candidate.scans_profile is None:
                        candidate.scans_profile = {}  # Set default empty dict
        return media
    except Exception as e:
        log.error(f"Error getting media info: {e}")
        return None

def safe_hashtag_medias_recent(hashtag, amount=50):
    """Safely get hashtag medias with proper validation fix."""
    try:
        medias = with_client(cl.hashtag_medias_recent, hashtag, amount=amount)
        return medias
    except ValidationError as e:
        if 'scans_profile' in str(e) or 'estimated_scans_sizes' in str(e):
            log.warning(f"Validation error for hashtag {hashtag}, attempting data fix: {e}")
            try:
                # Try using hashtag_medias_top which might have different structure
                medias = with_client(cl.hashtag_medias_top, hashtag, amount=amount)
                return medias
            except ValidationError:
                log.warning(f"Both recent and top hashtag methods failed for {hashtag}, returning empty")
                return []
            except Exception as fix_error:
                log.error(f"Could not fix validation error for hashtag {hashtag}: {fix_error}")
                return []
        else:
            raise e
    except Exception as e:
        log.error(f"Error getting hashtag medias for {hashtag}: {e}")
        return []

def safe_location_medias_recent(location_pk, amount=50):
    """Safely get location medias with proper validation fix."""
    try:
        medias = with_client(cl.location_medias_recent, location_pk, amount=amount)
        return medias
    except ValidationError as e:
        if 'scans_profile' in str(e) or 'estimated_scans_sizes' in str(e):
            log.warning(f"Validation error for location {location_pk}, attempting data fix: {e}")
            try:
                # Try using location_medias_top which might have different structure
                medias = with_client(cl.location_medias_top, location_pk, amount=amount)
                return medias
            except ValidationError:
                log.warning(f"Both recent and top location methods failed for {location_pk}, returning empty")
                return []
            except Exception as fix_error:
                log.error(f"Could not fix validation error for location {location_pk}: {fix_error}")
                return []
        else:
            raise e
    except Exception as e:
        log.error(f"Error getting location medias for {location_pk}: {e}")
        return []

def safe_user_medias(user_id, amount=50):
    """Safely get user medias with proper validation fix."""
    try:
        # Try the normal method first
        medias = with_client(cl.user_medias, user_id, amount=amount)
        return medias
    except ValidationError as e:
        if 'scans_profile' in str(e) or 'estimated_scans_sizes' in str(e):
            log.warning(f"Validation error for user {user_id}, trying alternative methods: {e}")
            
            # Try to get medias using the web/public API instead
            try:
                medias = with_client(cl.user_medias_v1, user_id, amount=amount)
                return medias
            except Exception:
                pass
            
            # Try getting media using user_info and recent posts
            try:
                user_info = with_client(cl.user_info, user_id)
                if user_info and user_info.media_count > 0:
                    # For now, return empty to avoid crashes - user will see "liked 0 posts"
                    log.warning(f"Validation issues prevent getting medias for user {user_id}, skipping")
                    return []
            except Exception:
                pass
                
            log.warning(f"All methods failed for user {user_id}, returning empty list")
            return []
        else:
            raise e
    except Exception as e:
        log.error(f"Error getting user medias for {user_id}: {e}")
        return []

# Rate limiting for geocoding to be respectful to Nominatim
_last_geocode_time = 0
_geocode_cache = {}

def geocode_location(location_name):
    """Get lat/lng coordinates for a location name using Nominatim (OpenStreetMap) with rate limiting."""
    global _last_geocode_time
    
    # Check cache first
    if location_name in _geocode_cache:
        return _geocode_cache[location_name]
    
    try:
        # Rate limiting: minimum 1 second between requests (Nominatim policy)
        current_time = time.time()
        time_since_last = current_time - _last_geocode_time
        if time_since_last < 1.0:
            time.sleep(1.0 - time_since_last)
        
        # Use Nominatim API (free, no API key required)
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': location_name,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        headers = {
            'User-Agent': 'Instagram-Bot/1.0 (Educational Purpose)'
        }
        
        _last_geocode_time = time.time()
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data:
                result = float(data[0]['lat']), float(data[0]['lon'])
                # Cache successful results
                _geocode_cache[location_name] = result
                return result
        elif response.status_code == 429:
            log.warning(f"Rate limited by Nominatim for {location_name}, waiting...")
            time.sleep(5)  # Back off on rate limit
        
        # Cache negative results to avoid repeated requests
        _geocode_cache[location_name] = (None, None)
        return None, None
    except Exception as e:
        log.error(f"Geocoding error for {location_name}: {e}")
        _geocode_cache[location_name] = (None, None)
        return None, None

def safe_location_search(location_name):
    """Safely search for locations by name using proper geocoding."""
    try:
        # First try to geocode the location name to get lat/lng
        lat, lng = geocode_location(location_name)
        
        if lat and lng:
            try:
                # Use fbsearch_places if available (requires lat/lng)
                if hasattr(cl, 'fbsearch_places'):
                    locations = with_client(cl.fbsearch_places, location_name, lat, lng)
                    if locations:
                        return locations
            except Exception as e:
                log.warning(f"fbsearch_places failed for {location_name}: {e}")
            
            try:
                # Try location_search with coordinates
                locations = with_client(cl.location_search, lat, lng)
                if locations:
                    return locations
            except Exception as e:
                log.warning(f"location_search with coords failed for {location_name}: {e}")
        
        # Fallback: Try searching by hashtag and extracting locations
        try:
            hashtag_info = with_client(cl.hashtag_info, location_name.lower().replace(' ', ''))
            if hashtag_info:
                medias = safe_hashtag_medias_recent(location_name.lower().replace(' ', ''), amount=20)
                seen_locations = set()
                locations = []
                for media in medias:
                    if hasattr(media, 'location') and media.location and media.location.pk not in seen_locations:
                        locations.append(media.location)
                        seen_locations.add(media.location.pk)
                        if len(locations) >= 5:
                            break
                if locations:
                    log.info(f"Found {len(locations)} locations via hashtag fallback for {location_name}")
                    return locations
        except Exception as e:
            log.warning(f"Hashtag fallback failed for {location_name}: {e}")
        
        log.warning(f"No locations found for {location_name}")
        return []
    except Exception as e:
        log.error(f"Error searching location {location_name}: {e}")
        return []

def with_client(func, *args, **kwargs):
    """
    Execute any Instagram client operation with thread-safe locking.
    Prevents session corruption under concurrent access.
    Usage: with_client(cl.user_followers, cl.user_id)
    """
    with client_lock:
        return func(*args, **kwargs)

def ig_call(func, category='read', *args, **kwargs):
    """
    Rate-limited Instagram API call with proper error handling.
    category: 'read', 'like', 'follow', 'story', 'search'
    """
    # Check if category is in cooldown
    if category in cooldowns and time.time() < cooldowns[category]:
        remaining = int(cooldowns[category] - time.time())
        log.warning(f"Category {category} in cooldown for {remaining}s, skipping request")
        raise Exception(f"Rate limited: {category} in cooldown for {remaining}s")
    
    # Check rate bucket
    bucket = rate_buckets.get(category, rate_buckets['read'])
    if not bucket.consume():
        log.warning(f"Rate bucket {category} empty, waiting...")
        time.sleep(random.uniform(30, 90))  # Wait before retrying
        if not bucket.consume():
            raise Exception(f"Rate limited: {category} bucket exhausted")
    
    # Add base delay to mimic human behavior
    time.sleep(random.uniform(2, 7))
    
    with client_lock:
        try:
            result = func(*args, **kwargs)
            return result
        except PleaseWaitFewMinutes as e:
            # Set cooldown for this category
            cooldown_time = random.uniform(10*60, 30*60)  # 10-30 minutes
            cooldowns[category] = time.time() + cooldown_time
            log.error(f"Instagram requested wait for {category}, cooldown set for {cooldown_time/60:.1f} minutes")
            raise e
        except (ClientUnauthorizedError, LoginRequired) as e:
            # Try to re-login once
            log.warning(f"Session invalid for {category}, attempting re-login: {e}")
            try:
                if ensure_login():
                    log.info("Re-login successful, retrying request")
                    return func(*args, **kwargs)
                else:
                    cooldowns[category] = time.time() + 60*60  # 1 hour cooldown
                    raise Exception("Re-login failed, setting 1 hour cooldown")
            except Exception as retry_error:
                cooldowns[category] = time.time() + 60*60  # 1 hour cooldown
                log.error(f"Re-login and retry failed for {category}: {retry_error}")
                raise retry_error
        except Exception as e:
            # For other errors, add smaller delay to avoid hammering
            time.sleep(random.uniform(5, 15))
            raise e

def save_session():
    try:
        settings = cl.get_settings()
        import json
        # Create session file with secure permissions (readable only by owner)
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        # Secure file permissions: owner read/write only (600)
        os.chmod(SESSION_FILE, 0o600)
        log.info("Instagram session saved with secure permissions.")
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
    # SECURITY: Only use environment variables for credentials - never store in database
    username = IG_USERNAME_ENV
    password = IG_PASSWORD_ENV
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
        with client_lock:
            # Try using saved settings if present
            load_session()
            # Real attempt to login
            cl.login(username, password)
            save_session()
        # SECURITY: Never store credentials in database - only use environment variables
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
    result = fetch_db("SELECT day FROM daily_limits WHERE day=?", (today,))
    if not result:
        # create new row with defaults
        execute_db("INSERT OR REPLACE INTO daily_limits (day, follows, unfollows, likes, dms, story_views) VALUES (?, ?, ?, ?, ?, ?)",
                   (today, 0, 0, 0, 0, 0))

def increment_limit(action: str, amount: int = 1):
    reset_daily_limits_if_needed()
    today = get_today_str()
    execute_db(f"UPDATE daily_limits SET {action} = {action} + ? WHERE day=?", (amount, today))

def get_limits() -> Dict[str, int]:
    reset_daily_limits_if_needed()
    today = get_today_str()
    result = fetch_db("SELECT follows, unfollows, likes, dms, story_views FROM daily_limits WHERE day=?", (today,))
    if result:
        r = result[0]
        return {"follows": r[0], "unfollows": r[1], "likes": r[2], "dms": r[3], "story_views": r[4]}
    return {"follows": 0, "unfollows": 0, "likes": 0, "dms": 0, "story_views": 0}

def set_daily_cap(action: str, cap: int):
    # We will store caps as env default + runtime; this function stores custom cap by writing to hashtags (hack) OR
    # Simpler: keep in-memory override (but user asked persistent). We'll implement a dedicated caps table.
    execute_db("""CREATE TABLE IF NOT EXISTS caps (action TEXT PRIMARY KEY, cap INTEGER)""")
    execute_db("INSERT OR REPLACE INTO caps (action, cap) VALUES (?, ?)", (action, cap))

def get_daily_cap(action: str) -> int:
    execute_db("""CREATE TABLE IF NOT EXISTS caps (action TEXT PRIMARY KEY, cap INTEGER)""")
    result = fetch_db("SELECT cap FROM caps WHERE action=?", (action,))
    if result:
        return int(result[0][0])
    return DAILY_DEFAULT_LIMITS.get(action, 99999)

# ---------------------------
# Core features (likes, story view, follow/unfollow, hashtags, geo)
# ---------------------------
def auto_like_followers(likes_per_user: int = 2, daily_cap_check: bool = True) -> str:
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        # Use v1 API to avoid GraphQL 401 errors
        followers = ig_call(cl.user_followers_v1, 'read', cl.user_id)
        count_liked = 0
        for user_id in list(followers.keys()):
            try:
                # Check daily cap for likes
                if daily_cap_check and get_limits()["likes"] >= get_daily_cap("likes"):
                    log.info("Daily likes cap reached.")
                    break
                medias = safe_user_medias(user_id, amount=likes_per_user)
                if not medias:
                    log.warning(f"No medias found for user {user_id}, skipping to next user")
                    continue
                    
                user_liked_count = 0
                for m in medias:
                    result = fetch_db("SELECT 1 FROM liked_posts WHERE post_id=?", (str(m.pk),))
                    if result:
                        continue
                    try:
                        with_client(cl.media_like, m.pk)
                        execute_db("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", (str(m.pk),))
                        increment_limit("likes", 1)
                        count_liked += 1
                        user_liked_count += 1
                        log.info("Liked media %s from user %s", m.pk, user_id)
                        time.sleep(random.uniform(5, 15))
                    except Exception as like_error:
                        log.warning(f"Failed to like media {m.pk} from user {user_id}: {like_error}")
                        continue
                        
                log.info(f"Liked {user_liked_count} posts from user {user_id}")
            except ClientError as e:
                log.warning("Like error user %s: %s", user_id, e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected like error for user %s: %s", user_id, e)
        return f"✅ Auto-like done! Liked {count_liked} posts."
    except Exception as e:
        log.exception("Auto-like overall error: %s", e)
        return f"An error occurred: {e}"

def auto_like_following(likes_per_user: int = 2, daily_cap_check: bool = True) -> str:
    """Auto-like posts from users you're following (not followers)."""
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        following = with_client(cl.user_following, cl.user_id)
        count_liked = 0
        for user_id in list(following.keys()):
            try:
                # Check daily cap for likes
                if daily_cap_check and get_limits()["likes"] >= get_daily_cap("likes"):
                    log.info("Daily likes cap reached.")
                    break
                medias = safe_user_medias(user_id, amount=likes_per_user)
                if not medias:
                    log.warning(f"No medias found for user {user_id}, skipping to next user")
                    continue
                    
                user_liked_count = 0
                for m in medias:
                    result = fetch_db("SELECT 1 FROM liked_posts WHERE post_id=?", (str(m.pk),))
                    if result:
                        continue
                    try:
                        with_client(cl.media_like, m.pk)
                        execute_db("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", (str(m.pk),))
                        increment_limit("likes", 1)
                        count_liked += 1
                        user_liked_count += 1
                        log.info("Liked media %s from user %s", m.pk, user_id)
                        time.sleep(random.uniform(5, 15))
                    except Exception as like_error:
                        log.warning(f"Failed to like media {m.pk} from user {user_id}: {like_error}")
                        continue
                        
                log.info(f"Liked {user_liked_count} posts from user {user_id}")
            except ClientError as e:
                log.warning("Like error user %s: %s", user_id, e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected like error for user %s: %s", user_id, e)
        return f"✅ Auto-like following done! Liked {count_liked} posts from users you follow."
    except Exception as e:
        log.exception("Auto-like following overall error: %s", e)
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
                stories = with_client(cl.user_stories, user_id)
                for s in stories:
                    result = fetch_db("SELECT 1 FROM viewed_stories WHERE story_id=?", (str(s.pk),))
                    if result:
                        continue
                    try:
                        with_client(cl.story_view, s.pk)
                        execute_db("INSERT OR REPLACE INTO viewed_stories (story_id) VALUES (?)", (str(s.pk),))
                        increment_limit("story_views", 1)
                        count_viewed += 1
                        log.info("Viewed story %s from %s", s.pk, user_id)
                        # Random reaction
                        if random.random() < reaction_chance:
                            try:
                                # Try to react with heart emoji (❤️) - emoji_id varies
                                with_client(cl.story_reaction, s.pk, "❤️")
                                count_reacted += 1
                                log.info("Reacted to story %s from %s", s.pk, user_id)
                            except Exception as er:
                                log.warning("Failed to react to story %s: %s", s.pk, er)
                        time.sleep(random.uniform(3, 8))
                    except ClientError as e:
                        log.warning("Story view error for story %s: %s", s.pk, e)
                        time.sleep(30)
            except Exception as e:
                log.exception("Story view error for user %s: %s", user_id, e)
        return f"✅ Story viewing done! Viewed {count_viewed} stories, reacted to {count_reacted}."
    except Exception as e:
        log.exception("Story view overall error: %s", e)
        return f"An error occurred: {e}"

def auto_follow_targeted(hashtag: str, amount: int = 20, daily_cap_check: bool = True) -> str:
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        medias = safe_hashtag_medias_recent(hashtag, amount=amount * 3)  # get extra in case some filtered
        count_followed = 0
        for m in medias:
            try:
                if daily_cap_check and get_limits()["follows"] >= get_daily_cap("follows"):
                    log.info("Daily follows cap reached.")
                    break
                user_id = str(m.user.pk)
                # check blacklist
                result = fetch_db("SELECT 1 FROM blacklist_users WHERE user_id=?", (user_id,))
                if result:
                    continue
                # check if already followed
                result = fetch_db("SELECT 1 FROM followed_users WHERE user_id=?", (user_id,))
                if result:
                    continue
                # check if already unfollowed (we don't re-follow)
                result = fetch_db("SELECT 1 FROM unfollowed_users WHERE user_id=?", (user_id,))
                if result:
                    continue
                # perform follow
                with_client(cl.user_follow, user_id)
                execute_db("INSERT OR REPLACE INTO followed_users (user_id, followed_at) VALUES (?, ?)", 
                           (user_id, datetime.now().isoformat()))
                increment_limit("follows", 1)
                count_followed += 1
                log.info("Followed user %s from hashtag %s", user_id, hashtag)
                if count_followed >= amount:
                    break
                time.sleep(random.uniform(10, 30))
            except ClientError as e:
                log.warning("Follow error user %s: %s", getattr(locals(), 'user_id', 'unknown'), e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected follow error for user %s: %s", getattr(locals(), 'user_id', 'unknown'), e)
        return f"✅ Targeted follow done! Followed {count_followed} users from #{hashtag}."
    except Exception as e:
        log.exception("Targeted follow overall error: %s", e)
        return f"An error occurred: {e}"

def auto_follow_location(location: str, amount: int = 20, daily_cap_check: bool = True) -> str:
    """Follow users from a specific location."""
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        # Use location_medias_recent to get posts from specific location
        try:
            location_info = safe_location_search(location)
            if not location_info:
                return f"❌ Location '{location}' not found."
            
            location_pk = location_info[0].pk
            medias = safe_location_medias_recent(location_pk, amount=amount * 3)
        except Exception as e:
            log.error(f"Error searching location {location}: {e}")
            return f"❌ Error searching location: {e}"
        
        count_followed = 0
        for m in medias:
            try:
                if daily_cap_check and get_limits()["follows"] >= get_daily_cap("follows"):
                    log.info("Daily follows cap reached.")
                    break
                user_id = str(m.user.pk)
                # check blacklist
                result = fetch_db("SELECT 1 FROM blacklist_users WHERE user_id=?", (user_id,))
                if result:
                    continue
                # check if already followed
                result = fetch_db("SELECT 1 FROM followed_users WHERE user_id=?", (user_id,))
                if result:
                    continue
                # check if already unfollowed (we don't re-follow)
                result = fetch_db("SELECT 1 FROM unfollowed_users WHERE user_id=?", (user_id,))
                if result:
                    continue
                # perform follow
                with_client(cl.user_follow, user_id)
                execute_db("INSERT OR REPLACE INTO followed_users (user_id, followed_at) VALUES (?, ?)", 
                           (user_id, datetime.now().isoformat()))
                increment_limit("follows", 1)
                count_followed += 1
                log.info("Followed user %s from location %s", user_id, location)
                if count_followed >= amount:
                    break
                time.sleep(random.uniform(10, 30))
            except ClientError as e:
                log.warning("Follow error user %s: %s", getattr(locals(), 'user_id', 'unknown'), e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected follow error for user %s: %s", getattr(locals(), 'user_id', 'unknown'), e)
        return f"✅ Location follow done! Followed {count_followed} users from {location}."
    except Exception as e:
        log.exception("Location follow overall error: %s", e)
        return f"An error occurred: {e}"

def auto_unfollow_old(days_threshold: int = 7, daily_cap_check: bool = True) -> str:
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        cutoff_date = (datetime.now() - timedelta(days=days_threshold)).isoformat()
        old_follows = fetch_db("SELECT user_id, followed_at FROM followed_users WHERE followed_at < ?", (cutoff_date,))
        count_unfollowed = 0
        for user_id, followed_at in old_follows:
            try:
                if daily_cap_check and get_limits()["unfollows"] >= get_daily_cap("unfollows"):
                    log.info("Daily unfollows cap reached.")
                    break
                # Check if they follow us back
                try:
                    followers = with_client(cl.user_followers, cl.user_id)
                    if int(user_id) in followers:
                        log.info("User %s follows us back, skipping unfollow", user_id)
                        continue
                except Exception as e:
                    log.warning("Could not check if %s follows back: %s", user_id, e)
                # Unfollow
                with_client(cl.user_unfollow, user_id)
                execute_db("DELETE FROM followed_users WHERE user_id=?", (user_id,))
                execute_db("INSERT OR REPLACE INTO unfollowed_users (user_id) VALUES (?)", (user_id,))
                increment_limit("unfollows", 1)
                count_unfollowed += 1
                log.info("Unfollowed user %s (followed at %s)", user_id, followed_at)
                time.sleep(random.uniform(5, 15))
            except ClientError as e:
                log.warning("Unfollow error user %s: %s", user_id, e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected unfollow error for user %s: %s", user_id, e)
        return f"✅ Auto-unfollow done! Unfollowed {count_unfollowed} old follows."
    except Exception as e:
        log.exception("Auto-unfollow overall error: %s", e)
        return f"An error occurred: {e}"

def send_personalized_dm(user_id: str, message_template: str, daily_cap_check: bool = True) -> str:
    if not ensure_login():
        return "🚫 Instagram not logged in."
    try:
        reset_daily_limits_if_needed()
        if daily_cap_check and get_limits()["dms"] >= get_daily_cap("dms"):
            return "📝 Daily DM cap reached."
        
        # Get user info for personalization
        user_info = with_client(cl.user_info, user_id)
        username = user_info.username
        
        # Simple personalization
        message = message_template.replace("{username}", username)
        
        # Send DM
        with_client(cl.direct_send, message, [user_id])
        increment_limit("dms", 1)
        log.info("Sent DM to %s: %s", username, message[:50])
        return f"✅ DM sent to @{username}"
    except Exception as e:
        log.exception("DM error for user %s: %s", user_id, e)
        return f"❌ DM failed: {e}"

# ---------------------------
# Hashtag and geography management
# ---------------------------
def add_hashtag(tag: str, tier: int = 2) -> str:
    execute_db("INSERT OR REPLACE INTO hashtags (tag, tier) VALUES (?, ?)", (tag.lower().strip("#"), tier))
    return f"✅ Added hashtag #{tag} with tier {tier}"

def remove_hashtag(tag: str) -> str:
    execute_db("DELETE FROM hashtags WHERE tag=?", (tag.lower().strip("#"),))
    return f"✅ Removed hashtag #{tag}"

def list_hashtags() -> str:
    hashtags = fetch_db("SELECT tag, tier FROM hashtags ORDER BY tier, tag")
    if not hashtags:
        return "📝 No hashtags configured."
    result = "📝 Configured hashtags:\n"
    for tag, tier in hashtags:
        result += f"  #{tag} (Tier {tier})\n"
    return result

def add_to_blacklist(user_id: str) -> str:
    execute_db("INSERT OR REPLACE INTO blacklist_users (user_id) VALUES (?)", (user_id,))
    return f"✅ Added user {user_id} to blacklist"

def remove_from_blacklist(user_id: str) -> str:
    execute_db("DELETE FROM blacklist_users WHERE user_id=?", (user_id,))
    return f"✅ Removed user {user_id} from blacklist"

# ---------------------------
# Enhanced follow function with location and hashtag defaults
# ---------------------------
def enhanced_follow(targets: List[str], amount: int = 20) -> str:
    """Enhanced follow function that supports hashtags, locations, and defaults."""
    if not targets:
        # Use defaults
        default_hashtags = get_default_hashtags()
        default_locations = get_default_locations()
        
        if not default_hashtags and not default_locations:
            return "❌ No targets specified and no defaults configured. Use /add_default_hashtag or /add_location first."
        
        targets = []
        if default_hashtags:
            targets.extend([f"#{tag}" for tag in default_hashtags])
        if default_locations:
            targets.extend(default_locations)
    
    results = []
    total_followed = 0
    
    for target in targets:
        try:
            if target.startswith('#'):
                # Hashtag follow
                hashtag = target[1:]
                result = auto_follow_targeted(hashtag, amount, daily_cap_check=True)
                results.append(f"Hashtag #{hashtag}: {result}")
            else:
                # Location follow
                result = auto_follow_location(target, amount, daily_cap_check=True)
                results.append(f"Location {target}: {result}")
        except Exception as e:
            results.append(f"Error with {target}: {e}")
    
    return "\n".join(results)

# ---------------------------
# Statistics and reporting
# ---------------------------
def get_bot_stats() -> str:
    limits = get_limits()
    caps = {action: get_daily_cap(action) for action in limits.keys()}
    
    followed_result = fetch_db("SELECT COUNT(*) FROM followed_users")
    followed_count = followed_result[0][0] if followed_result else 0
    
    blacklist_result = fetch_db("SELECT COUNT(*) FROM blacklist_users")
    blacklist_count = blacklist_result[0][0] if blacklist_result else 0
    
    hashtag_result = fetch_db("SELECT COUNT(*) FROM hashtags")
    hashtag_count = hashtag_result[0][0] if hashtag_result else 0
    
    authorized_result = fetch_db("SELECT COUNT(*) FROM authorized_users")
    authorized_count = authorized_result[0][0] if authorized_result else 0
    
    pending_result = fetch_db("SELECT COUNT(*) FROM access_requests WHERE status='pending'")
    pending_count = pending_result[0][0] if pending_result else 0
    
    location_result = fetch_db("SELECT COUNT(*) FROM locations")
    location_count = location_result[0][0] if location_result else 0
    
    default_hashtag_result = fetch_db("SELECT COUNT(*) FROM default_hashtags")
    default_hashtag_count = default_hashtag_result[0][0] if default_hashtag_result else 0
    
    stats = f"""📊 Bot Statistics (Today: {get_today_str()})

🎯 Daily Activity:
  Follows: {limits['follows']}/{caps['follows']}
  Unfollows: {limits['unfollows']}/{caps['unfollows']}
  Likes: {limits['likes']}/{caps['likes']}
  DMs: {limits['dms']}/{caps['dms']}
  Story Views: {limits['story_views']}/{caps['story_views']}

📊 Database Stats:
  Currently Following: {followed_count}
  Blacklisted Users: {blacklist_count}
  Configured Hashtags: {hashtag_count}
  Default Hashtags: {default_hashtag_count}
  Stored Locations: {location_count}

👥 Access Control:
  Authorized Users: {authorized_count}
  Pending Requests: {pending_count}

🔐 Instagram Status: {'✅ Logged in' if ensure_login() else '❌ Not logged in'}
"""
    return stats

# ---------------------------
# Telegram bot handlers
# ---------------------------
def auth_required(func):
    """Decorator to check if user is authorized."""
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_authorized(user_id):
            keyboard = [[InlineKeyboardButton("Request Access", callback_data=f"request_access_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "🚫 You are not authorized to use this bot. Please request access from the admin.",
                reply_markup=reply_markup
            )
            return
        return await func(update, context)
    return wrapper

def admin_required(func):
    """Decorator to check if user is admin."""
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("🚫 This command is only available to admins.")
            return
        return await func(update, context)
    return wrapper

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    
    if is_authorized(user_id):
        await update.message.reply_text(
            f"Welcome back! You have access to the Instagram automation bot.\n\n"
            f"Available commands:\n"
            f"/help - Show all commands\n"
            f"/stats - Show bot statistics\n"
            f"/follow - Follow users from hashtags/locations\n"
            f"/locations - Manage default locations\n"
            f"/hashtags - Manage default hashtags"
        )
    else:
        keyboard = [[InlineKeyboardButton("Request Access", callback_data=f"request_access_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🔐 Welcome to the Instagram Automation Bot!\n\n"
            f"This bot requires admin approval to use. Please request access below.",
            reply_markup=reply_markup
        )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.username or "Unknown"
    
    if data.startswith("request_access_"):
        # Handle access request
        result = request_access(user_id, username)
        await query.edit_message_text(result)
        
        # Notify admin if there are pending requests
        if "submitted" in result.lower():
            pending_requests = list_pending_requests()
            if pending_requests and ADMIN_USER_ID > 0:
                try:
                    message = "🔔 New access request:\n\n"
                    for req_user_id, req_username, requested_at in pending_requests[-1:]:  # Show only the latest
                        keyboard = [
                            [
                                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{req_user_id}"),
                                InlineKeyboardButton("❌ Deny", callback_data=f"deny_{req_user_id}")
                            ]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        message += f"User: @{req_username} (ID: {req_user_id})\nRequested: {requested_at}"
                        
                        await context.bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text=message,
                            reply_markup=reply_markup
                        )
                except Exception as e:
                    log.error(f"Failed to notify admin: {e}")
    
    elif data.startswith("approve_"):
        # Handle approval (admin only)
        if user_id != ADMIN_USER_ID:
            await query.edit_message_text("🚫 Only admins can approve requests.")
            return
        
        target_user_id = data.split("_")[1]
        result = approve_access(target_user_id, user_id)
        await query.edit_message_text(result)
        
        # Notify the approved user
        try:
            await context.bot.send_message(
                chat_id=int(target_user_id),
                text="🎉 Your access request has been approved! You can now use the bot. Type /start to begin."
            )
        except Exception as e:
            log.error(f"Failed to notify approved user: {e}")
    
    elif data.startswith("deny_"):
        # Handle denial (admin only)
        if user_id != ADMIN_USER_ID:
            await query.edit_message_text("🚫 Only admins can deny requests.")
            return
        
        target_user_id = data.split("_")[1]
        result = deny_access(target_user_id)
        await query.edit_message_text(result)
        
        # Notify the denied user
        try:
            await context.bot.send_message(
                chat_id=int(target_user_id),
                text="❌ Your access request has been denied. Contact the admin for more information."
            )
        except Exception as e:
            log.error(f"Failed to notify denied user: {e}")

@auth_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 Instagram Automation Bot Commands

📊 **General:**
/stats - Show bot statistics and daily limits
/help - Show this help message

🎯 **Following:**
/follow [hashtag]/[location] [amount] - Follow users (uses defaults if no params)
/unfollow [days] - Unfollow old users (default: 7 days)

📍 **Location Management:**
/add_location [location] - Add default location
/remove_location [location] - Remove location
/list_locations - Show all saved locations

🏷️ **Hashtag Management:**
/add_default_hashtag [hashtag] - Add default hashtag
/remove_default_hashtag [hashtag] - Remove default hashtag
/list_default_hashtags - Show default hashtags
/add_hashtag [hashtag] [tier] - Add hashtag with tier
/remove_hashtag [hashtag] - Remove hashtag
/list_hashtags - Show all hashtags

❤️ **Engagement:**
/like_followers [amount] - Like followers' posts
/view_stories - View and react to stories
/send_dm [user_id] [message] - Send personalized DM

🚫 **Blacklist:**
/blacklist_add [user_id] - Add user to blacklist
/blacklist_remove [user_id] - Remove from blacklist

🔐 **Instagram Account:**
/login [username] [password] - Login to Instagram
/logout - Logout from Instagram

⚙️ **Settings:**
/set_cap [action] [amount] - Set daily cap for action
/reset_limits - Reset today's limits

👥 **Admin Only:**
/pending_requests - Show pending access requests
/authorized_users - Show authorized users
"""
    await update.message.reply_text(help_text)

@auth_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_bot_stats()
    await update.message.reply_text(stats)

@auth_required
async def follow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    
    if not args:
        # Use defaults
        result = enhanced_follow([], 20)
        await update.message.reply_text(result)
        return
    
    targets = []
    amount = 20
    
    # Parse arguments
    for arg in args:
        if arg.isdigit():
            amount = int(arg)
        else:
            targets.append(arg)
    
    await run_instagram_task(update, "Follow", enhanced_follow, targets, amount)

@auth_required
async def add_location_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add_location [location]\nExample: /add_location jamnagar")
        return
    
    location = " ".join(context.args)
    result = add_location(location)
    await update.message.reply_text(result)

@auth_required
async def remove_location_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_location [location]")
        return
    
    location = " ".join(context.args)
    result = remove_location(location)
    await update.message.reply_text(result)

@auth_required
async def list_locations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = list_locations()
    await update.message.reply_text(result)

@auth_required
async def add_default_hashtag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add_default_hashtag [hashtag]\nExample: /add_default_hashtag travel")
        return
    
    hashtag = context.args[0]
    result = add_default_hashtag(hashtag)
    await update.message.reply_text(result)

@auth_required
async def remove_default_hashtag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_default_hashtag [hashtag]")
        return
    
    hashtag = context.args[0]
    result = remove_default_hashtag(hashtag)
    await update.message.reply_text(result)

@auth_required
async def list_default_hashtags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = list_default_hashtags()
    await update.message.reply_text(result)

@admin_required
async def pending_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = list_pending_requests()
    if not pending:
        await update.message.reply_text("📝 No pending access requests.")
        return
    
    message = "📝 Pending Access Requests:\n\n"
    for user_id, username, requested_at in pending:
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny_{user_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        request_text = f"User: @{username} (ID: {user_id})\nRequested: {requested_at}"
        
        await update.message.reply_text(request_text, reply_markup=reply_markup)

@admin_required
async def authorized_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = fetch_db("SELECT user_id, username, authorized_at FROM authorized_users ORDER BY authorized_at")
    if not users:
        await update.message.reply_text("📝 No authorized users.")
        return
    
    message = "👥 Authorized Users:\n\n"
    for user_id, username, authorized_at in users:
        message += f"@{username} (ID: {user_id})\nAuthorized: {authorized_at}\n\n"
    
    await update.message.reply_text(message)

# Continue with rest of the handlers...
@auth_required
async def unfollow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = 7
    if context.args and context.args[0].isdigit():
        days = int(context.args[0])
    await run_instagram_task(update, "Unfollow", auto_unfollow_old, days)

@auth_required
async def like_followers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = 2
    if context.args and context.args[0].isdigit():
        amount = int(context.args[0])
    await run_instagram_task(update, "Like Followers", auto_like_followers, amount)

@auth_required
async def view_stories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_login():
        await update.message.reply_text("🚫 Instagram not logged in.")
        return
    
    try:
        followers = with_client(cl.user_followers, cl.user_id)
        await run_instagram_task(update, "View Stories", auto_view_stories, followers)
    except Exception as e:
        await update.message.reply_text(f"❌ Error getting followers: {e}")

@auth_required
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /login [username] [password]")
        return
    
    username, password = context.args
    await run_instagram_task(update, "Instagram Login", login_instagram, username, password)

@auth_required
async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with client_lock:
            cl.logout()
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        await update.message.reply_text("✅ Logged out from Instagram and cleared session.")
    except Exception as e:
        await update.message.reply_text(f"❌ Logout error: {e}")

# Additional command handlers (blacklist, hashtag management, etc.)
@auth_required
async def add_hashtag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add_hashtag [hashtag] [tier]\nExample: /add_hashtag travel 1")
        return
    
    hashtag = context.args[0]
    tier = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 2
    result = add_hashtag(hashtag, tier)
    await update.message.reply_text(result)

@auth_required
async def remove_hashtag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove_hashtag [hashtag]")
        return
    
    hashtag = context.args[0]
    result = remove_hashtag(hashtag)
    await update.message.reply_text(result)

@auth_required
async def list_hashtags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = list_hashtags()
    await update.message.reply_text(result)

@auth_required
async def blacklist_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /blacklist_add [user_id]")
        return
    
    user_id = context.args[0]
    result = add_to_blacklist(user_id)
    await update.message.reply_text(result)

@auth_required
async def blacklist_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /blacklist_remove [user_id]")
        return
    
    user_id = context.args[0]
    result = remove_from_blacklist(user_id)
    await update.message.reply_text(result)

@auth_required
async def send_dm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /send_dm [user_id] [message]")
        return
    
    user_id = context.args[0]
    message = " ".join(context.args[1:])
    await run_instagram_task(update, "Send DM", send_personalized_dm, user_id, message)

@auth_required
async def set_cap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /set_cap [action] [amount]\nActions: follows, unfollows, likes, dms, story_views")
        return
    
    action, amount = context.args
    if not amount.isdigit():
        await update.message.reply_text("Amount must be a number.")
        return
    
    set_daily_cap(action, int(amount))
    await update.message.reply_text(f"✅ Set daily cap for {action} to {amount}")

@auth_required
async def reset_limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = get_today_str()
    execute_db("UPDATE daily_limits SET follows=0, unfollows=0, likes=0, dms=0, story_views=0 WHERE day=?", (today,))
    await update.message.reply_text("✅ Daily limits reset to zero.")

# ---------------------------
# Scheduler for background tasks
# ---------------------------
def schedule_tasks():
    """Schedule background tasks."""
    # Auto-unfollow old follows every day at 2 AM
    schedule.every().day.at("02:00").do(auto_unfollow_old, 7, True)
    
    # View stories every 6 hours
    def scheduled_story_view():
        if ensure_login():
            try:
                followers = with_client(cl.user_followers, cl.user_id)
                auto_view_stories(followers, 0.05, True)
            except Exception as e:
                log.error(f"Scheduled story view failed: {e}")
    
    schedule.every(6).hours.do(scheduled_story_view)

def run_scheduler():
    """Run scheduled tasks in background."""
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# ---------------------------
# Flask keep-alive server
# ---------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Instagram Bot is running!"

@app.route('/status')
def status():
    return {
        "status": "running",
        "logged_in": ensure_login(),
        "stats": get_bot_stats()
    }

def run_flask():
    """Run Flask server for keep-alive."""
    app.run(host='0.0.0.0', port=5000, debug=False)

# ---------------------------
# Main execution
# ---------------------------
def main():
    """Main function to start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set. Exiting.")
        return
    
    # Initialize scheduler
    schedule_tasks()
    
    # Start background threads
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    
    scheduler_thread.start()
    flask_thread.start()
    
    log.info("Background threads started.")
    
    # Create Telegram application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("follow", follow_command))
    application.add_handler(CommandHandler("unfollow", unfollow_command))
    application.add_handler(CommandHandler("like_followers", like_followers_command))
    application.add_handler(CommandHandler("view_stories", view_stories_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("logout", logout_command))
    
    # Location management
    application.add_handler(CommandHandler("add_location", add_location_command))
    application.add_handler(CommandHandler("remove_location", remove_location_command))
    application.add_handler(CommandHandler("list_locations", list_locations_command))
    
    # Hashtag management
    application.add_handler(CommandHandler("add_default_hashtag", add_default_hashtag_command))
    application.add_handler(CommandHandler("remove_default_hashtag", remove_default_hashtag_command))
    application.add_handler(CommandHandler("list_default_hashtags", list_default_hashtags_command))
    application.add_handler(CommandHandler("add_hashtag", add_hashtag_command))
    application.add_handler(CommandHandler("remove_hashtag", remove_hashtag_command))
    application.add_handler(CommandHandler("list_hashtags", list_hashtags_command))
    
    # Blacklist management
    application.add_handler(CommandHandler("blacklist_add", blacklist_add_command))
    application.add_handler(CommandHandler("blacklist_remove", blacklist_remove_command))
    
    # DM and settings
    application.add_handler(CommandHandler("send_dm", send_dm_command))
    application.add_handler(CommandHandler("set_cap", set_cap_command))
    application.add_handler(CommandHandler("reset_limits", reset_limits_command))
    
    # Admin commands
    application.add_handler(CommandHandler("pending_requests", pending_requests_command))
    application.add_handler(CommandHandler("authorized_users", authorized_users_command))
    
    # Callback query handler for inline buttons
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    log.info("Instagram Automation Bot started!")
    log.info("Commands: /start, /help, /stats, /follow, /login, /add_location, /add_default_hashtag")
    
    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()
