#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enhanced Story Viewing Logic Module
Handles all story viewing functionality for Instagram bot with advanced features
"""

import time
import random
import logging
from typing import Dict, List, Set, Optional
from datetime import datetime, timedelta

from instagrapi.exceptions import ClientError, LoginRequired

from database import fetch_db, execute_db, bulk_insert, increment_limit, get_limits, get_daily_cap, reset_daily_limits_if_needed
from instagram_client import ensure_login, with_client, cl

log = logging.getLogger("story_viewer")

# Rate limiting for story operations
class StoryRateLimiter:
    def __init__(self):
        self.hourly_views = 0
        self.hourly_reset_time = datetime.now() + timedelta(hours=1)
        self.error_count = 0
        self.last_error_time = None
        
    def check_hourly_limit(self, max_per_hour: int = 50) -> bool:
        """Check if hourly story view limit is reached"""
        now = datetime.now()
        if now >= self.hourly_reset_time:
            self.hourly_views = 0
            self.hourly_reset_time = now + timedelta(hours=1)
        return self.hourly_views < max_per_hour
    
    def increment_hourly(self):
        """Increment hourly counter"""
        self.hourly_views += 1
    
    def get_error_backoff_time(self) -> int:
        """Get adaptive backoff time based on error count"""
        if self.error_count == 0:
            return 0
        elif self.error_count == 1:
            return 60  # 1 minute
        elif self.error_count == 2:
            return 300  # 5 minutes
        else:
            return 1800  # 30 minutes - pause the whole task
    
    def record_error(self):
        """Record an error and increment counter"""
        self.error_count += 1
        self.last_error_time = datetime.now()
    
    def reset_errors(self):
        """Reset error counter after successful operation"""
        self.error_count = 0
        self.last_error_time = None

story_rate_limiter = StoryRateLimiter()

def get_smart_story_delay(base_min: int = 3, base_max: int = 8, story_count: int = 0) -> float:
    """Generate smart delay with longer pauses between story batches"""
    # Base delay between story views
    delay = random.uniform(base_min, base_max)
    
    # Add longer pause every few stories
    if story_count > 0 and story_count % 10 == 0:
        delay += random.uniform(30, 90)  # 30s to 1.5min extra pause
        
    # Random chance for extra long pause
    if random.random() < 0.12:  # 12% chance
        delay += random.uniform(60, 180)  # 1-3 minute pause
        
    return delay

def handle_story_client_error(error: Exception, story_id: str) -> Optional[str]:
    """Smart error handling for story operations with adaptive backoff"""
    error_msg = str(error).lower()
    
    # Handle LoginRequired specifically
    if isinstance(error, LoginRequired) or 'login_required' in error_msg:
        story_rate_limiter.record_error()
        log.error(f"Login required for story {story_id}. Account session expired.")
        return "❌ Instagram session expired. Please re-login and retry."
    
    elif '429' in error_msg or 'too many requests' in error_msg:
        story_rate_limiter.record_error()
        backoff_time = story_rate_limiter.get_error_backoff_time()
        log.warning(f"Rate limit hit for story {story_id}. Backing off for {backoff_time}s")
        
        if backoff_time >= 1800:  # 30 minutes - stop the task
            return "❌ Story viewing rate limit exceeded. Pausing task for 30 minutes."
        else:
            time.sleep(backoff_time)
            return None  # Continue trying
    
    elif '403' in error_msg or 'forbidden' in error_msg or 'blocked' in error_msg:
        story_rate_limiter.record_error()
        log.error(f"Account blocked/forbidden for viewing story {story_id}. Stopping task.")
        return "❌ Instagram has blocked story viewing. Please wait before retrying."
    
    elif 'story not found' in error_msg or 'expired' in error_msg:
        log.debug(f"Story {story_id} not found or expired")
        return None  # Continue with next story
    
    else:
        story_rate_limiter.record_error()
        log.warning(f"Unknown client error for story {story_id}: {error}")
        backoff_time = min(story_rate_limiter.get_error_backoff_time(), 300)  # Max 5 min
        time.sleep(backoff_time)
        return None

def send_story_telegram_notification(message: str):
    """Send story-related notification to Telegram (placeholder for integration)"""
    # TODO: Integrate with telegram handlers to send notifications
    log.info(f"📱 Story notification: {message}")

# Backward compatibility wrapper
def auto_view_stories(users_dict: Dict[str, str], daily_cap_check: bool = True,
                     hourly_cap_check: bool = True, batch_size: int = 50) -> str:
    """Backward compatibility wrapper for fetch_story_info"""
    log.warning("auto_view_stories is deprecated, use fetch_story_info instead")
    return fetch_story_info(users_dict, daily_cap_check, hourly_cap_check, batch_size)

def fetch_story_info(users_dict: Dict[str, str], daily_cap_check: bool = True,
                    hourly_cap_check: bool = True, batch_size: int = 50) -> str:
    """
    Fetch story metadata for users (renamed from auto_view_stories for clarity).
    Note: This fetches story metadata but doesn't mark stories as "viewed" in Instagram.
    The Instagram API doesn't provide a public method to mark stories as viewed.
    """
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    try:
        reset_daily_limits_if_needed()
        
        # Pre-fetch viewed stories to avoid repeated DB queries
        viewed_stories: Set[str] = set()
        viewed_rows = fetch_db("SELECT story_id FROM viewed_stories")
        for row in viewed_rows:
            viewed_stories.add(row[0])
            
        count_processed = 0
        users_processed = 0
        users_skipped = 0
        story_batch_inserts = []
        
        for user_id, username in users_dict.items():
            try:
                # Check limits
                if daily_cap_check and get_limits()["story_views"] >= get_daily_cap("story_views"):
                    log.info("Daily story views cap reached.")
                    send_story_telegram_notification("⚠️ Daily story views cap reached")
                    break
                    
                if hourly_cap_check and not story_rate_limiter.check_hourly_limit():
                    log.info("Hourly story views cap reached. Waiting for reset.")
                    send_story_telegram_notification("⏰ Hourly story views cap reached, pausing")
                    time.sleep(300)  # Wait 5 minutes
                    continue
                    
                users_processed += 1
                
                # Get user's stories
                try:
                    stories = with_client(cl.user_stories, user_id)
                    if not stories:
                        users_skipped += 1
                        log.debug(f"No active stories for user {user_id} ({username})")
                        continue
                        
                except ClientError as e:
                    error_result = handle_story_client_error(e, user_id)
                    if error_result:  # Fatal error, stop the task
                        send_story_telegram_notification(error_result)
                        return error_result
                    users_skipped += 1
                    continue
                    
                except Exception as stories_fetch_error:
                    log.warning(f"Could not fetch stories for user {user_id} ({username}): {stories_fetch_error}")
                    users_skipped += 1
                    continue
                    
                user_stories_processed = 0
                
                for story in stories:
                    story_id = str(story.pk)
                    
                    # Check if already processed (using in-memory set)
                    if story_id in viewed_stories:
                        continue
                        
                    try:
                        # Fetch story metadata (this doesn't mark as viewed in Instagram)
                        story_info = with_client(cl.story_info, story.pk)
                        
                        if story_info:
                            # Prepare for batch insert
                            story_batch_inserts.append((story_id,))
                            viewed_stories.add(story_id)  # Update in-memory cache
                            
                            increment_limit("story_views", 1)
                            story_rate_limiter.increment_hourly()
                            count_processed += 1
                            user_stories_processed += 1
                            
                            log.debug(f"Fetched story info {story_id} from {username}")
                            
                            # Smart delay between story fetches
                            delay = get_smart_story_delay(story_count=count_processed)
                            time.sleep(delay)
                            
                            # Reset error counter on successful fetch
                            story_rate_limiter.reset_errors()
                            
                    except ClientError as e:
                        error_result = handle_story_client_error(e, story_id)
                        if error_result:  # Fatal error, stop the task
                            send_story_telegram_notification(error_result)
                            return error_result
                        continue
                        
                    except Exception as story_error:
                        log.warning(f"Failed to fetch story info {story_id} from {username}: {story_error}")
                        continue
                        
                    # Batch insert stories to database when we hit batch_size
                    if len(story_batch_inserts) >= batch_size:
                        try:
                            bulk_insert("viewed_stories", ["story_id"], story_batch_inserts)
                            story_batch_inserts = []  # Clear the batch
                        except Exception as db_error:
                            log.error(f"Database batch insert error: {db_error}")
                            # Fallback to individual inserts
                            for story_id_tuple in story_batch_inserts:
                                execute_db("INSERT OR REPLACE INTO viewed_stories (story_id) VALUES (?)", story_id_tuple)
                            story_batch_inserts = []
                            
                if user_stories_processed > 0:
                    log.info(f"✅ Processed {user_stories_processed} stories from {username}")
                    
                # Send progress notification every 20 users
                if users_processed % 20 == 0:
                    send_story_telegram_notification(f"📊 Progress: Processed {count_processed} stories from {users_processed} users")
                    
            except Exception as user_error:
                log.exception(f"Story processing error for user {user_id} ({username}): {user_error}")
                users_skipped += 1
                continue
                
        # Final batch insert for any remaining stories
        if story_batch_inserts:
            try:
                bulk_insert("viewed_stories", ["story_id"], story_batch_inserts)
            except Exception as db_error:
                log.error(f"Final database batch insert error: {db_error}")
                for story_id_tuple in story_batch_inserts:
                    execute_db("INSERT OR REPLACE INTO viewed_stories (story_id) VALUES (?)", story_id_tuple)
                    
        # Final summary
        summary_msg = f"✅ Story info fetching completed!\n"
        summary_msg += f"• Stories processed: {count_processed}\n"
        summary_msg += f"• Users processed: {users_processed}\n"
        summary_msg += f"• Users skipped: {users_skipped}\n"
        summary_msg += f"• Hourly views used: {story_rate_limiter.hourly_views}/50"
        
        send_story_telegram_notification(summary_msg)
        return summary_msg
        
    except Exception as e:
        log.exception(f"Story info fetching overall error: {e}")
        error_msg = f"❌ An error occurred: {e}"
        send_story_telegram_notification(error_msg)
        return error_msg

def fetch_stories_from_source(source_type: str, daily_cap_check: bool = True,
                             hourly_cap_check: bool = True, batch_size: int = 50) -> str:
    """Unified function to fetch story info from followers or following"""
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    try:
        # Get user list based on source type
        if source_type == "followers":
            users_list = with_client(cl.user_followers_v1, cl.user_id)
            action_name = "followers"
        elif source_type == "following":
            users_list = with_client(cl.user_following, cl.user_id)
            action_name = "following"
        else:
            return f"❌ Invalid source type: {source_type}"
            
        # Convert to dict format expected by fetch_story_info
        users_dict = {str(user.pk): user.username for user in users_list}
        
        log.info(f"Fetching story info from {len(users_dict)} {action_name}")
        
        # Use the unified function
        result = fetch_story_info(users_dict, daily_cap_check, hourly_cap_check, batch_size)
        
        # Update the result message to include source type
        if result.startswith("✅"):
            result = result.replace("Story info fetching completed!", f"Story info fetching from {action_name} completed!")
        
        return result
        
    except Exception as e:
        log.exception(f"Fetch stories from {source_type} error: {e}")
        error_msg = f"❌ An error occurred: {e}"
        send_story_telegram_notification(error_msg)
        return error_msg

def auto_view_followers_stories(daily_cap_check: bool = True, hourly_cap_check: bool = True) -> str:
    """Fetch story info from your followers (renamed for clarity)"""
    return fetch_stories_from_source("followers", daily_cap_check, hourly_cap_check)

def auto_view_following_stories(daily_cap_check: bool = True, hourly_cap_check: bool = True) -> str:
    """Fetch story info from users you're following (renamed for clarity)"""
    return fetch_stories_from_source("following", daily_cap_check, hourly_cap_check)

# Backward compatibility - keeping old function names but with deprecation warning
def auto_view_stories(users_dict: Dict, daily_cap_check: bool = True) -> str:
    """
    DEPRECATED: Use fetch_story_info() instead.
    This function name was misleading as it doesn't actually 'view' stories in Instagram.
    """
    log.warning("auto_view_stories() is deprecated. Use fetch_story_info() instead.")
    return fetch_story_info(users_dict, daily_cap_check)

def get_user_stories_count(user_id: str) -> int:
    """Get the number of active stories for a user"""
    try:
        if not ensure_login():
            return 0
        stories = with_client(cl.user_stories, user_id)
        return len(stories)
    except Exception as e:
        log.warning(f"Could not get stories count for user {user_id}: {e}")
        return 0

def get_story_stats() -> Dict[str, any]:
    """Get comprehensive story viewing statistics"""
    try:
        daily_limits = get_limits()
        total_viewed_stories = len(fetch_db("SELECT story_id FROM viewed_stories"))
        
        return {
            "total_story_views_today": daily_limits["story_views"],
            "hourly_story_views": story_rate_limiter.hourly_views,
            "daily_story_cap": get_daily_cap("story_views"),
            "total_stories_in_db": total_viewed_stories,
            "remaining_daily_views": get_daily_cap("story_views") - daily_limits["story_views"],
            "remaining_hourly_views": 50 - story_rate_limiter.hourly_views,
            "error_count": story_rate_limiter.error_count,
            "last_error_time": story_rate_limiter.last_error_time.isoformat() if story_rate_limiter.last_error_time else None
        }
    except Exception as e:
        log.error(f"Error getting story stats: {e}")
        return {}

def reset_story_hourly_limits():
    """Manual reset of hourly story limits (admin function)"""
    global story_rate_limiter
    story_rate_limiter.hourly_views = 0
    story_rate_limiter.hourly_reset_time = datetime.now() + timedelta(hours=1)
    story_rate_limiter.error_count = 0
    story_rate_limiter.last_error_time = None
    log.info("Hourly story view limits reset manually")
    return "✅ Hourly story view limits and errors reset"

def clear_old_viewed_stories(days_old: int = 30) -> str:
    """Clean up old viewed stories from database to save space"""
    try:
        cutoff_date = datetime.now() - timedelta(days=days_old)
        
        # Since we don't track when stories were viewed, we'll use a different approach
        # We'll clean up stories older than X days based on story ID patterns (if possible)
        # For now, let's just provide a count of total stories
        
        total_stories = len(fetch_db("SELECT story_id FROM viewed_stories"))
        
        # If we had timestamps, we could do:
        # result = execute_db("DELETE FROM viewed_stories WHERE viewed_at < ?", (cutoff_date.isoformat(),))
        
        return f"ℹ️ Database contains {total_stories} viewed stories. Consider manual cleanup if needed."
        
    except Exception as e:
        log.error(f"Error clearing old viewed stories: {e}")
        return f"❌ Error clearing old stories: {e}"