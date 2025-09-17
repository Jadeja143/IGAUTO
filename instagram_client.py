#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Instagram Client Module
Handles Instagram API connections and operations
"""

import os
import time
import random
import logging
import threading
from typing import Dict, List, Optional, Tuple, Any

from instagrapi import Client
from instagrapi.exceptions import ClientError, PleaseWaitFewMinutes, ClientUnauthorizedError, LoginRequired, BadPassword, ChallengeRequired
from pydantic import ValidationError

from database import fetch_db, execute_db

log = logging.getLogger("instagram_client")

# Instagram client setup
cl = Client()
cl.delay_range = [2, 7]
SESSION_FILE = "insta_session.json"
client_lock = threading.Lock()

# Monkey-patch extractor to handle Instagram API changes
from instagrapi import extractors as _ex
_orig_extract_media_v1 = _ex.extract_media_v1

def _patched_extract_media_v1(media):
    """Patched extractor to handle missing fields in Instagram API responses"""
    # Fix image_versions2.candidates missing scans_profile
    iv2 = media.get('image_versions2', {})
    for c in iv2.get('candidates', []) or []:
        c.setdefault('scans_profile', {})
        c.setdefault('estimated_scans_sizes', [])
    
    # Fix clips_metadata missing fields
    clips = media.get('clips_metadata')
    if clips:
        clips.setdefault('mashup_info', {})
        clips.setdefault('audio_ranking_info', {})
        clips.setdefault('original_sound_info', {})
        # Fix reusable_text_info when it's a list instead of dict
        if isinstance(clips.get('reusable_text_info'), list):
            clips['reusable_text_info'] = {}
        clips.setdefault('reusable_text_info', {})
    
    # Fix location when it's None
    if media.get('location') is None:
        media['location'] = {}
    
    # Fix carousel_media if present
    if 'carousel_media' in media and media['carousel_media']:
        for carousel_item in media['carousel_media']:
            carousel_item = _fix_media_fields(carousel_item)
    
    return _orig_extract_media_v1(media)

def _fix_media_fields(media_dict):
    """Helper to fix media fields recursively"""
    # Fix image_versions2.candidates missing scans_profile
    iv2 = media_dict.get('image_versions2', {})
    for c in iv2.get('candidates', []) or []:
        c.setdefault('scans_profile', {})
        c.setdefault('estimated_scans_sizes', [])
    
    # Fix clips_metadata missing fields
    clips = media_dict.get('clips_metadata')
    if clips:
        clips.setdefault('mashup_info', {})
        clips.setdefault('audio_ranking_info', {})
        clips.setdefault('original_sound_info', {})
        if isinstance(clips.get('reusable_text_info'), list):
            clips['reusable_text_info'] = {}
        clips.setdefault('reusable_text_info', {})
    
    return media_dict

# Apply the patch
_ex.extract_media_v1 = _patched_extract_media_v1

# Environment variables
IG_USERNAME_ENV = os.environ.get("IG_USERNAME")
IG_PASSWORD_ENV = os.environ.get("IG_PASSWORD")

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
    'read': TokenBucket(60, 60, 3600),    # 60 req/hour for reads
    'like': TokenBucket(40, 40, 3600),    # 40 req/hour for likes
    'follow': TokenBucket(15, 15, 3600),  # 15 req/hour for follows
    'story': TokenBucket(120, 120, 3600), # 120 req/hour for story views
    'search': TokenBucket(30, 30, 3600),  # 30 req/hour for hashtag/location search
}

# Cooldown tracking
cooldowns = {}

def with_client(func, *args, **kwargs):
    """Thread-safe wrapper for Instagram client operations"""
    with client_lock:
        try:
            return func(*args, **kwargs)
        except (PleaseWaitFewMinutes, ClientError) as e:
            log.warning(f"Instagram API error: {e}")
            time.sleep(60)  # Wait on rate limit
            raise

def ig_call(func, bucket_type: str, *args, **kwargs):
    """Make Instagram API call with rate limiting"""
    bucket = rate_buckets.get(bucket_type, rate_buckets['read'])
    
    if not bucket.consume():
        log.warning(f"Rate limit hit for {bucket_type}, waiting...")
        time.sleep(60)
        
    return with_client(func, *args, **kwargs)

def ensure_login() -> bool:
    """Ensure Instagram client is logged in"""
    try:
        if cl.user_id:
            return True
    except:
        pass
    
    # Try to load session
    try:
        if os.path.exists(SESSION_FILE):
            cl.load_settings(SESSION_FILE)
            if cl.user_id:
                log.info("Instagram session loaded successfully")
                return True
    except Exception as e:
        log.warning(f"Could not load session: {e}")
    
    # Try login with environment variables
    if IG_USERNAME_ENV and IG_PASSWORD_ENV:
        try:
            cl.login(IG_USERNAME_ENV, IG_PASSWORD_ENV)
            cl.dump_settings(SESSION_FILE)
            # Set secure permissions
            os.chmod(SESSION_FILE, 0o600)
            log.info("Instagram session saved with secure permissions.")
            log.info(f"Instagram login successful for {IG_USERNAME_ENV}")
            return True
        except Exception as e:
            log.error(f"Instagram login failed: {e}")
            return False
    
    # Try login with database credentials
    try:
        creds = fetch_db("SELECT value FROM credentials WHERE key IN ('username', 'password')")
        if len(creds) == 2:
            username = creds[0][0] if creds[0] else None
            password = creds[1][0] if creds[1] else None
            
            if username and password:
                cl.login(username, password)
                cl.dump_settings(SESSION_FILE)
                os.chmod(SESSION_FILE, 0o600)
                log.info("Instagram session saved with secure permissions.")
                log.info(f"Instagram login successful for {username}")
                return True
    except Exception as e:
        log.error(f"Database credential login failed: {e}")
    
    return False

def login_instagram(username: str, password: str) -> str:
    """Login to Instagram with username and password"""
    try:
        cl.login(username, password)
        
        # Save session
        cl.dump_settings(SESSION_FILE)
        os.chmod(SESSION_FILE, 0o600)
        
        # Save credentials to database
        execute_db("INSERT OR REPLACE INTO credentials (key, value) VALUES (?, ?)", ("username", username))
        execute_db("INSERT OR REPLACE INTO credentials (key, value) VALUES (?, ?)", ("password", password))
        
        log.info("Instagram session saved with secure permissions.")
        log.info(f"Instagram login successful for {username}")
        
        # Send Telegram notification
        success_msg = f"✅ Successfully logged in to Instagram as @{username}"
        _send_telegram_notification(success_msg)
        
        return success_msg
    except Exception as e:
        log.error(f"Instagram login failed: {e}")
        error_msg = f"❌ Instagram login failed: {e}"
        _send_telegram_notification(error_msg)
        return error_msg

def _send_telegram_notification(message: str):
    """Send notification to admin via Telegram bot"""
    try:
        import os
        import asyncio
        from telegram import Bot
        
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
        
        if TELEGRAM_BOT_TOKEN and ADMIN_USER_ID:
            async def send_message():
                bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await bot.send_message(chat_id=ADMIN_USER_ID, text=message)
            
            # Run in new event loop to avoid conflicts
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_message())
                loop.close()
                log.info(f"📱 Telegram notification sent: {message}")
            except Exception as e:
                log.warning(f"Failed to send Telegram notification: {e}")
        else:
            log.warning("Telegram notification not sent - missing token or admin ID")
    except Exception as e:
        log.error(f"Error in Telegram notification: {e}")

def logout_instagram() -> str:
    """Logout from Instagram"""
    try:
        # Clear session file
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        
        # Clear saved credentials
        execute_db("DELETE FROM credentials WHERE key IN ('username', 'password')")
        
        # Reset client
        cl.user_id = None
        
        log.info("Instagram logout successful")
        return "✅ Successfully logged out from Instagram"
    except Exception as e:
        log.error(f"Instagram logout failed: {e}")
        return f"❌ Logout failed: {e}"

# Safe API methods with error handling
def safe_user_medias(user_id, amount=50):
    """Safely get user medias with proper validation and KeyError handling."""
    try:
        # Try the main method first (uses both GraphQL and v1)
        medias = with_client(cl.user_medias, user_id, amount=amount)
        return medias
    except (ValidationError, KeyError) as e:
        log.warning(f"Error getting user medias for {user_id} (trying v1 fallback): {e}")
        try:
            # Direct v1 API call as fallback
            medias = with_client(cl.user_medias_v1, user_id, amount=amount)
            return medias
        except (ValidationError, KeyError) as e2:
            log.warning(f"v1 fallback also failed for user {user_id}: {e2}")
            try:
                # Last resort: try user info and return empty if issues persist
                user_info = with_client(cl.user_info, user_id)
                if user_info and user_info.media_count > 0:
                    log.warning(f"User {user_id} has {user_info.media_count} media but extraction failed, returning empty")
                return []
            except Exception as e3:
                log.error(f"All methods failed for user {user_id}: {e3}")
                return []
    except Exception as e:
        log.error(f"Unexpected error getting user medias for {user_id}: {e}")
        return []

def safe_hashtag_medias_recent(hashtag, amount=50):
    """Safely get hashtag medias with proper validation and KeyError handling."""
    try:
        # Try recent hashtag medias first
        medias = with_client(cl.hashtag_medias_recent, hashtag, amount=amount)
        return medias
    except (ValidationError, KeyError) as e:
        log.warning(f"Error getting recent hashtag medias for #{hashtag}, trying top medias: {e}")
        try:
            # Try top hashtag medias as fallback
            medias = with_client(cl.hashtag_medias_top, hashtag, amount=amount)
            return medias
        except (ValidationError, KeyError) as e2:
            log.warning(f"Both recent and top hashtag methods failed for #{hashtag}: {e2}")
            try:
                # Try hashtag info to check if hashtag exists
                hashtag_info = with_client(cl.hashtag_info, hashtag)
                if hashtag_info and hashtag_info.media_count > 0:
                    log.warning(f"Hashtag #{hashtag} has {hashtag_info.media_count} media but extraction failed")
                return []
            except Exception as e3:
                log.error(f"All methods failed for hashtag #{hashtag}: {e3}")
                return []
    except Exception as e:
        log.error(f"Unexpected error getting hashtag medias for #{hashtag}: {e}")
        return []

def safe_location_medias_recent(location_pk, amount=50):
    """Safely get location medias with proper validation and KeyError handling."""
    try:
        # Try recent location medias first
        medias = with_client(cl.location_medias_recent, location_pk, amount=amount)
        return medias
    except (ValidationError, KeyError) as e:
        log.warning(f"Error getting recent location medias for {location_pk}, trying top medias: {e}")
        try:
            # Try top location medias as fallback
            medias = with_client(cl.location_medias_top, location_pk, amount=amount)
            return medias
        except (ValidationError, KeyError) as e2:
            log.warning(f"Both recent and top location methods failed for {location_pk}: {e2}")
            try:
                # Try location info to check if location exists
                location_info = with_client(cl.location_info, location_pk)
                if location_info:
                    log.warning(f"Location {location_pk} exists but media extraction failed")
                return []
            except Exception as e3:
                log.error(f"All methods failed for location {location_pk}: {e3}")
                return []
    except Exception as e:
        log.error(f"Unexpected error getting location medias for {location_pk}: {e}")
        return []