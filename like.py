#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enhanced Auto-Like Logic Module
Handles all auto-like functionality for Instagram bot with advanced features
"""

import time
import random
import logging
from typing import Dict, List, Set, Optional
from datetime import datetime, timedelta
from instagrapi.exceptions import ClientError

from database import fetch_db, execute_db, bulk_insert, increment_limit, get_limits, get_daily_cap, reset_daily_limits_if_needed
from instagram_client import ensure_login, with_client, cl, safe_user_medias

log = logging.getLogger("like")

# Rate limiting and error handling
class SmartRateLimiter:
    def __init__(self):
        self.hourly_likes = 0
        self.hourly_reset_time = datetime.now() + timedelta(hours=1)
        self.error_count = 0
        self.last_error_time = None
        
    def check_hourly_limit(self, max_per_hour: int = 40) -> bool:
        """Check if hourly limit is reached"""
        now = datetime.now()
        if now >= self.hourly_reset_time:
            self.hourly_likes = 0
            self.hourly_reset_time = now + timedelta(hours=1)
        return self.hourly_likes < max_per_hour
    
    def increment_hourly(self):
        """Increment hourly counter"""
        self.hourly_likes += 1
    
    def get_error_backoff_time(self) -> int:
        """Get exponential backoff time based on error count"""
        if self.error_count == 0:
            return 0
        elif self.error_count == 1:
            return 60  # 1 minute
        elif self.error_count == 2:
            return 300  # 5 minutes
        else:
            return 1800  # 30 minutes
    
    def record_error(self):
        """Record an error and increment counter"""
        self.error_count += 1
        self.last_error_time = datetime.now()
    
    def reset_errors(self):
        """Reset error counter after successful operation"""
        self.error_count = 0
        self.last_error_time = None

rate_limiter = SmartRateLimiter()

def should_like_media(media, max_days_old: int = 30, min_likes_threshold: int = 50, max_likes_threshold: int = 10000) -> bool:
    """Smart filtering for media to like"""
    try:
        # Skip very old posts
        if hasattr(media, 'taken_at'):
            days_old = (datetime.now() - media.taken_at).days
            if days_old > max_days_old:
                return False
        
        # Skip posts with too few or too many likes (avoid bot detection)
        if hasattr(media, 'like_count'):
            if media.like_count < min_likes_threshold or media.like_count > max_likes_threshold:
                return False
        
        # Skip IGTV and Reels (optional - can be configured)
        if hasattr(media, 'media_type') and media.media_type in [2, 8]:  # Video types
            return False
            
        return True
    except Exception as e:
        log.warning(f"Error filtering media: {e}")
        return True  # Default to allowing like if we can't filter

def get_smart_delay(base_min: int = 5, base_max: int = 15, user_count: int = 0) -> float:
    """Generate smart delay with longer pauses between users"""
    # Base delay between likes
    delay = random.uniform(base_min, base_max)
    
    # Add longer pause every few users
    if user_count > 0 and user_count % 5 == 0:
        delay += random.uniform(30, 120)  # 30s to 2min extra pause
        
    # Random chance for extra long pause to look human
    if random.random() < 0.1:  # 10% chance
        delay += random.uniform(60, 300)  # 1-5 minute pause
        
    return delay

def handle_client_error(error: ClientError, user_id: str) -> Optional[str]:
    """Smart error handling with appropriate backoff"""
    error_msg = str(error).lower()
    
    if '429' in error_msg or 'too many requests' in error_msg:
        rate_limiter.record_error()
        backoff_time = rate_limiter.get_error_backoff_time()
        log.warning(f"Rate limit hit for user {user_id}. Backing off for {backoff_time}s")
        time.sleep(backoff_time)
        return None  # Continue trying
    
    elif '403' in error_msg or 'forbidden' in error_msg or 'blocked' in error_msg:
        rate_limiter.record_error()
        log.error(f"Account blocked/forbidden for user {user_id}. Stopping task.")
        return "❌ Instagram has blocked like actions. Please wait before retrying."
    
    else:
        rate_limiter.record_error()
        log.warning(f"Unknown client error for user {user_id}: {error}")
        time.sleep(60)  # Standard wait for unknown errors
        return None

def send_telegram_notification(message: str):
    """Send notification to Telegram (placeholder for integration)"""
    # TODO: Integrate with telegram handlers to send notifications
    log.info(f"📱 Telegram notification: {message}")

def auto_like_users(source_type: str, likes_per_user: int = 2, daily_cap_check: bool = True, 
                   hourly_cap_check: bool = True, smart_filtering: bool = True) -> str:
    """Unified auto-like function for followers/following"""
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    try:
        reset_daily_limits_if_needed()
        
        # Get user list based on source type
        if source_type == "followers":
            users_list = with_client(cl.user_followers_v1, cl.user_id)
            action_name = "followers"
        elif source_type == "following":
            users_list = with_client(cl.user_following, cl.user_id)
            action_name = "following"
        else:
            return f"❌ Invalid source type: {source_type}"
            
        # Pre-fetch liked posts to avoid repeated DB queries
        liked_posts: Set[str] = set()
        liked_rows = fetch_db("SELECT post_id FROM liked_posts")
        for row in liked_rows:
            liked_posts.add(row[0])
            
        count_liked = 0
        users_processed = 0
        users_skipped = 0
        
        for user in users_list:
            try:
                # Check limits
                if daily_cap_check and get_limits()["likes"] >= get_daily_cap("likes"):
                    log.info("Daily likes cap reached.")
                    send_telegram_notification("⚠️ Daily likes cap reached")
                    break
                    
                if hourly_cap_check and not rate_limiter.check_hourly_limit():
                    log.info("Hourly likes cap reached. Waiting for reset.")
                    send_telegram_notification("⏰ Hourly likes cap reached, pausing")
                    time.sleep(300)  # Wait 5 minutes
                    continue
                    
                user_id = str(user.pk)
                users_processed += 1
                
                # Get user's media
                medias = safe_user_medias(user_id, amount=likes_per_user * 2)  # Get extra for filtering
                if not medias:
                    users_skipped += 1
                    log.debug(f"No medias found for user {user_id}")
                    continue
                    
                user_liked_count = 0
                media_batch_inserts = []
                
                for media in medias[:likes_per_user]:  # Limit to requested amount
                    media_id = str(media.pk)
                    
                    # Check if already liked (using in-memory set)
                    if media_id in liked_posts:
                        continue
                        
                    # Apply smart filtering
                    if smart_filtering and not should_like_media(media):
                        continue
                        
                    try:
                        # Perform the like
                        with_client(cl.media_like, media.pk)
                        
                        # Prepare for batch insert
                        media_batch_inserts.append((media_id,))
                        liked_posts.add(media_id)  # Update in-memory cache
                        
                        increment_limit("likes", 1)
                        rate_limiter.increment_hourly()
                        count_liked += 1
                        user_liked_count += 1
                        
                        log.info(f"Liked media {media_id} from user {user_id} ({user.username})")
                        
                        # Smart delay between likes
                        delay = get_smart_delay(user_count=users_processed)
                        time.sleep(delay)
                        
                        # Reset error counter on successful like
                        rate_limiter.reset_errors()
                        
                    except ClientError as e:
                        error_result = handle_client_error(e, user_id)
                        if error_result:  # Fatal error, stop the task
                            send_telegram_notification(error_result)
                            return error_result
                        continue
                        
                    except Exception as like_error:
                        log.warning(f"Failed to like media {media_id} from user {user_id}: {like_error}")
                        continue
                        
                # Batch insert liked posts to database (more efficient)
                if media_batch_inserts:
                    try:
                        bulk_insert("liked_posts", ["post_id"], media_batch_inserts)
                    except Exception as db_error:
                        log.error(f"Database batch insert error: {db_error}")
                        # Fallback to individual inserts
                        for media_id_tuple in media_batch_inserts:
                            execute_db("INSERT OR REPLACE INTO liked_posts (post_id) VALUES (?)", media_id_tuple)
                    
                if user_liked_count > 0:
                    log.info(f"✅ Liked {user_liked_count} posts from user {user_id} ({user.username})")
                    
                # Send progress notification every 10 users
                if users_processed % 10 == 0:
                    send_telegram_notification(f"📊 Progress: Liked {count_liked} posts from {users_processed} users")
                    
            except ClientError as e:
                error_result = handle_client_error(e, user_id if 'user_id' in locals() else 'unknown')
                if error_result:
                    send_telegram_notification(error_result)
                    return error_result
                continue
                
            except Exception as e:
                log.exception(f"Unexpected error for user {user.pk}: {e}")
                continue
                
        # Final summary
        summary_msg = f"✅ Auto-like {action_name} completed!\n"
        summary_msg += f"• Liked: {count_liked} posts\n"
        summary_msg += f"• Users processed: {users_processed}\n"
        summary_msg += f"• Users skipped: {users_skipped}\n"
        summary_msg += f"• Hourly likes used: {rate_limiter.hourly_likes}/40"
        
        send_telegram_notification(summary_msg)
        return summary_msg
        
    except Exception as e:
        log.exception(f"Auto-like {source_type} overall error: {e}")
        error_msg = f"❌ An error occurred: {e}"
        send_telegram_notification(error_msg)
        return error_msg

def auto_like_followers(likes_per_user: int = 2, daily_cap_check: bool = True, 
                       hourly_cap_check: bool = True, smart_filtering: bool = True) -> str:
    """Auto-like posts from your followers"""
    return auto_like_users("followers", likes_per_user, daily_cap_check, hourly_cap_check, smart_filtering)

def auto_like_following(likes_per_user: int = 2, daily_cap_check: bool = True,
                       hourly_cap_check: bool = True, smart_filtering: bool = True) -> str:
    """Auto-like posts from users you're following"""
    return auto_like_users("following", likes_per_user, daily_cap_check, hourly_cap_check, smart_filtering)

def get_like_stats() -> Dict[str, int]:
    """Get current like statistics"""
    try:
        total_likes_today = get_limits()["likes"]
        hourly_likes = rate_limiter.hourly_likes
        daily_cap = get_daily_cap("likes")
        
        return {
            "total_likes_today": total_likes_today,
            "hourly_likes": hourly_likes,
            "daily_cap": daily_cap,
            "remaining_daily": daily_cap - total_likes_today,
            "remaining_hourly": 40 - hourly_likes
        }
    except Exception as e:
        log.error(f"Error getting like stats: {e}")
        return {}

def reset_hourly_limits():
    """Manual reset of hourly limits (admin function)"""
    global rate_limiter
    rate_limiter.hourly_likes = 0
    rate_limiter.hourly_reset_time = datetime.now() + timedelta(hours=1)
    log.info("Hourly like limits reset manually")
    return "✅ Hourly like limits reset"