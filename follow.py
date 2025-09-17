#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enhanced Follow/Unfollow Logic Module
Handles all follow and unfollow functionality for Instagram bot with advanced features
"""

import time
import random
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple

from instagrapi.exceptions import ClientError

from database import fetch_db, execute_db, bulk_insert, increment_limit, get_limits, get_daily_cap, reset_daily_limits_if_needed
from instagram_client import ensure_login, with_client, cl, safe_hashtag_medias_recent, safe_location_medias_recent

log = logging.getLogger("follow")

# Enhanced rate limiting and error handling
class FollowRateLimiter:
    def __init__(self):
        self.hourly_follows = 0
        self.hourly_unfollows = 0
        self.hourly_reset_time = datetime.now() + timedelta(hours=1)
        self.error_count = 0
        self.last_error_time = None
        
    def check_hourly_follow_limit(self, max_per_hour: int = 15) -> bool:
        """Check if hourly follow limit is reached"""
        now = datetime.now()
        if now >= self.hourly_reset_time:
            self.hourly_follows = 0
            self.hourly_unfollows = 0
            self.hourly_reset_time = now + timedelta(hours=1)
        return self.hourly_follows < max_per_hour
    
    def check_hourly_unfollow_limit(self, max_per_hour: int = 20) -> bool:
        """Check if hourly unfollow limit is reached"""
        now = datetime.now()
        if now >= self.hourly_reset_time:
            self.hourly_follows = 0
            self.hourly_unfollows = 0
            self.hourly_reset_time = now + timedelta(hours=1)
        return self.hourly_unfollows < max_per_hour
    
    def increment_hourly_follows(self):
        """Increment hourly follow counter"""
        self.hourly_follows += 1
    
    def increment_hourly_unfollows(self):
        """Increment hourly unfollow counter"""
        self.hourly_unfollows += 1
    
    def get_error_backoff_time(self) -> int:
        """Get exponential backoff time based on error count"""
        if self.error_count == 0:
            return 0
        elif self.error_count == 1:
            return 300  # 5 minutes
        elif self.error_count == 2:
            return 900  # 15 minutes
        else:
            return 3600  # 1 hour
    
    def record_error(self):
        """Record an error and increment counter"""
        self.error_count += 1
        self.last_error_time = datetime.now()
    
    def reset_errors(self):
        """Reset error counter after successful operation"""
        self.error_count = 0
        self.last_error_time = None

follow_rate_limiter = FollowRateLimiter()

def should_follow_user(user, min_followers: int = 50, max_followers: int = 50000, 
                      min_following: int = 10, max_following_ratio: float = 3.0,
                      min_posts: int = 3, max_days_since_post: int = 180) -> Tuple[bool, str]:
    """Smart filtering for users to follow"""
    try:
        # Check if account is public
        if hasattr(user, 'is_private') and user.is_private:
            return False, "private account"
        
        # Check follower count
        if hasattr(user, 'follower_count'):
            if user.follower_count < min_followers or user.follower_count > max_followers:
                return False, f"followers: {user.follower_count}"
        
        # Check following count and ratio
        if hasattr(user, 'following_count') and hasattr(user, 'follower_count'):
            if user.following_count < min_following:
                return False, "too few following"
            
            # Avoid users who follow way more than their followers (likely bots)
            if user.follower_count > 0:
                ratio = user.following_count / user.follower_count
                if ratio > max_following_ratio:
                    return False, f"follow ratio: {ratio:.1f}"
        
        # Check post count
        if hasattr(user, 'media_count') and user.media_count < min_posts:
            return False, f"posts: {user.media_count}"
        
        # Check if account is active (last post within X days)
        # Note: This would require fetching user's recent media, which is expensive
        # For now, we'll skip this check to avoid API overhead
        
        return True, "passed all filters"
        
    except Exception as e:
        log.warning(f"Error filtering user {getattr(user, 'pk', 'unknown')}: {e}")
        return True, "filter error - allowing"

def get_smart_follow_delay(base_min: int = 10, base_max: int = 30, action_count: int = 0) -> float:
    """Generate smart delay with longer pauses between actions"""
    # Base delay between follows
    delay = random.uniform(base_min, base_max)
    
    # Add longer pause every few actions
    if action_count > 0 and action_count % 3 == 0:
        delay += random.uniform(60, 180)  # 1-3 min extra pause
        
    # Random chance for extra long pause
    if random.random() < 0.15:  # 15% chance
        delay += random.uniform(120, 600)  # 2-10 minute pause
        
    return delay

def handle_follow_client_error(error: ClientError, user_id: str, action: str = "follow") -> Optional[str]:
    """Smart error handling for follow/unfollow actions"""
    error_msg = str(error).lower()
    
    if '429' in error_msg or 'too many requests' in error_msg:
        follow_rate_limiter.record_error()
        backoff_time = follow_rate_limiter.get_error_backoff_time()
        log.warning(f"Rate limit hit for {action} user {user_id}. Backing off for {backoff_time}s")
        time.sleep(backoff_time)
        return None  # Continue trying
    
    elif '403' in error_msg or 'forbidden' in error_msg or 'blocked' in error_msg:
        follow_rate_limiter.record_error()
        log.error(f"Account blocked/forbidden for {action} user {user_id}. Stopping task.")
        return f"❌ Instagram has blocked {action} actions. Please wait before retrying."
    
    elif 'user not found' in error_msg or 'does not exist' in error_msg:
        log.warning(f"User {user_id} not found or deleted")
        return None  # Continue with next user
    
    else:
        follow_rate_limiter.record_error()
        log.warning(f"Unknown client error for {action} user {user_id}: {error}")
        time.sleep(120)  # Wait 2 minutes for unknown errors
        return None

def send_follow_telegram_notification(message: str):
    """Send follow-related notification to Telegram"""
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
            log.info(f"📱 Follow notification: {message}")
    except Exception as e:
        log.error(f"Error in Telegram notification: {e}")
        log.info(f"📱 Follow notification: {message}")

def auto_follow_from_source(source_type: str, source_identifier: str, amount: int = 20,
                           daily_cap_check: bool = True, hourly_cap_check: bool = True,
                           smart_filtering: bool = True) -> str:
    """Unified follow function for hashtags and locations"""
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    try:
        reset_daily_limits_if_needed()
        
        # Pre-load blacklist and followed users for efficient checking
        blacklist_users: Set[str] = set()
        blacklist_rows = fetch_db("SELECT user_id FROM blacklist_users")
        for row in blacklist_rows:
            blacklist_users.add(row[0])
            
        followed_users: Set[str] = set()
        followed_rows = fetch_db("SELECT user_id FROM followed_users")
        for row in followed_rows:
            followed_users.add(row[0])
            
        log.info(f"Loaded {len(blacklist_users)} blacklisted and {len(followed_users)} already followed users")
        
        # Get media based on source type
        if source_type == "hashtag":
            medias = safe_hashtag_medias_recent(source_identifier, amount=amount * 3)
            action_name = f"hashtag #{source_identifier}"
        elif source_type == "location":
            locations = with_client(cl.location_search, source_identifier)
            if not locations:
                return f"❌ No location found for '{source_identifier}'"
            location = locations[0]
            medias = safe_location_medias_recent(location.pk, amount=amount * 3)
            action_name = f"location '{source_identifier}'"
        else:
            return f"❌ Invalid source type: {source_type}"
            
        if not medias:
            return f"❌ No media found for {action_name}"
            
        count_followed = 0
        users_processed = 0
        users_skipped = 0
        skip_reasons: Dict[str, int] = {}
        
        for media in medias:
            try:
                # Check limits
                if daily_cap_check and get_limits()["follows"] >= get_daily_cap("follows"):
                    log.info("Daily follows cap reached.")
                    send_follow_telegram_notification("⚠️ Daily follows cap reached")
                    break
                    
                if hourly_cap_check and not follow_rate_limiter.check_hourly_follow_limit():
                    log.info("Hourly follows cap reached. Waiting for reset.")
                    send_follow_telegram_notification("⏰ Hourly follows cap reached, pausing")
                    time.sleep(600)  # Wait 10 minutes
                    continue
                    
                if count_followed >= amount:
                    break
                    
                user_id = str(media.user.pk)
                users_processed += 1
                
                # Quick checks using in-memory sets
                if user_id in blacklist_users:
                    users_skipped += 1
                    skip_reasons["blacklisted"] = skip_reasons.get("blacklisted", 0) + 1
                    continue
                    
                if user_id in followed_users:
                    users_skipped += 1
                    skip_reasons["already_followed"] = skip_reasons.get("already_followed", 0) + 1
                    continue
                    
                # Get full user info for smart filtering
                if smart_filtering:
                    try:
                        full_user_info = with_client(cl.user_info, user_id)
                        should_follow, reason = should_follow_user(full_user_info)
                        if not should_follow:
                            users_skipped += 1
                            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                            continue
                    except Exception as user_info_error:
                        log.warning(f"Could not get user info for {user_id}: {user_info_error}")
                        # Continue without filtering if we can't get user info
                        pass
                        
                # Follow the user
                try:
                    with_client(cl.user_follow, user_id)
                    
                    # Update database and in-memory cache
                    execute_db("INSERT OR REPLACE INTO followed_users (user_id, followed_at) VALUES (?, ?)", 
                              (user_id, datetime.now().isoformat()))
                    followed_users.add(user_id)
                    
                    increment_limit("follows", 1)
                    follow_rate_limiter.increment_hourly_follows()
                    count_followed += 1
                    
                    username = getattr(media.user, 'username', 'unknown')
                    log.info(f"Followed user {user_id} ({username}) from {action_name}")
                    
                    # Smart delay
                    delay = get_smart_follow_delay(action_count=count_followed)
                    time.sleep(delay)
                    
                    # Reset error counter on successful follow
                    follow_rate_limiter.reset_errors()
                    
                except ClientError as e:
                    error_result = handle_follow_client_error(e, user_id, "follow")
                    if error_result:  # Fatal error, stop the task
                        send_follow_telegram_notification(error_result)
                        return error_result
                    continue
                    
                except Exception as follow_error:
                    log.warning(f"Failed to follow user {user_id}: {follow_error}")
                    continue
                    
                # Send progress notification every 5 follows
                if count_followed % 5 == 0:
                    send_follow_telegram_notification(f"📈 Progress: Followed {count_followed} users from {action_name}")
                    
            except Exception as e:
                log.exception(f"Unexpected error processing media from user {getattr(media.user, 'pk', 'unknown')}: {e}")
                continue
                
        # Final summary
        summary_msg = f"✅ Auto-follow from {action_name} completed!\n"
        summary_msg += f"• Followed: {count_followed} users\n"
        summary_msg += f"• Users processed: {users_processed}\n"
        summary_msg += f"• Users skipped: {users_skipped}\n"
        
        if skip_reasons:
            summary_msg += f"• Skip reasons: {dict(skip_reasons)}\n"
            
        summary_msg += f"• Hourly follows used: {follow_rate_limiter.hourly_follows}/15"
        
        send_follow_telegram_notification(summary_msg)
        return summary_msg
        
    except Exception as e:
        log.exception(f"Auto-follow from {source_type} overall error: {e}")
        error_msg = f"❌ An error occurred: {e}"
        send_follow_telegram_notification(error_msg)
        return error_msg

def auto_follow_targeted(hashtag: str, amount: int = 20, daily_cap_check: bool = True,
                        hourly_cap_check: bool = True, smart_filtering: bool = True) -> str:
    """Follow users from a specific hashtag"""
    return auto_follow_from_source("hashtag", hashtag, amount, daily_cap_check, hourly_cap_check, smart_filtering)

def auto_follow_location(location_name: str, amount: int = 20, daily_cap_check: bool = True,
                        hourly_cap_check: bool = True, smart_filtering: bool = True) -> str:
    """Follow users from a specific location"""
    return auto_follow_from_source("location", location_name, amount, daily_cap_check, hourly_cap_check, smart_filtering)

def auto_unfollow_old(wait_days: int = 7, daily_cap_check: bool = True, 
                     hourly_cap_check: bool = True, unfollow_strategy: str = "not_following_back") -> str:
    """Enhanced unfollow function with multiple strategies"""
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    try:
        reset_daily_limits_if_needed()
        cutoff_date = datetime.now() - timedelta(days=wait_days)
        
        # Pre-load blacklist for efficient checking
        blacklist_users: Set[str] = set()
        blacklist_rows = fetch_db("SELECT user_id FROM blacklist_users")
        for row in blacklist_rows:
            blacklist_users.add(row[0])
            
        # Get users to potentially unfollow based on strategy
        if unfollow_strategy == "not_following_back":
            old_follows = fetch_db(
                "SELECT user_id, followed_at FROM followed_users WHERE followed_at < ? ORDER BY followed_at ASC", 
                (cutoff_date.isoformat(),)
            )
        elif unfollow_strategy == "all_old":
            old_follows = fetch_db(
                "SELECT user_id, followed_at FROM followed_users WHERE followed_at < ? ORDER BY followed_at ASC", 
                (cutoff_date.isoformat(),)
            )
        else:
            return f"❌ Invalid unfollow strategy: {unfollow_strategy}"
            
        if not old_follows:
            return f"✅ No users to unfollow (followed more than {wait_days} days ago)"
            
        count_unfollowed = 0
        users_processed = 0
        users_skipped = 0
        skip_reasons: Dict[str, int] = {}
        
        for row in old_follows:
            try:
                # Check limits
                if daily_cap_check and get_limits()["unfollows"] >= get_daily_cap("unfollows"):
                    log.info("Daily unfollows cap reached.")
                    send_follow_telegram_notification("⚠️ Daily unfollows cap reached")
                    break
                    
                if hourly_cap_check and not follow_rate_limiter.check_hourly_unfollow_limit():
                    log.info("Hourly unfollows cap reached. Waiting for reset.")
                    send_follow_telegram_notification("⏰ Hourly unfollows cap reached, pausing")
                    time.sleep(600)  # Wait 10 minutes
                    continue
                    
                user_id = row[0]
                followed_at = row[1]
                users_processed += 1
                
                # Check blacklist
                if user_id in blacklist_users:
                    users_skipped += 1
                    skip_reasons["blacklisted"] = skip_reasons.get("blacklisted", 0) + 1
                    continue
                    
                # Check if user follows us back (for not_following_back strategy)
                if unfollow_strategy == "not_following_back":
                    try:
                        friendship = with_client(cl.user_friendship, user_id)
                        if friendship.followed_by:  # User follows us back
                            users_skipped += 1
                            skip_reasons["follows_back"] = skip_reasons.get("follows_back", 0) + 1
                            continue
                    except Exception as friendship_error:
                        log.warning(f"Could not check friendship for user {user_id}: {friendship_error}")
                        users_skipped += 1
                        skip_reasons["friendship_check_failed"] = skip_reasons.get("friendship_check_failed", 0) + 1
                        continue
                        
                # Unfollow user
                try:
                    with_client(cl.user_unfollow, user_id)
                    
                    # Update database
                    execute_db("DELETE FROM followed_users WHERE user_id=?", (user_id,))
                    execute_db("INSERT OR REPLACE INTO unfollowed_users (user_id) VALUES (?)", (user_id,))
                    
                    increment_limit("unfollows", 1)
                    follow_rate_limiter.increment_hourly_unfollows()
                    count_unfollowed += 1
                    
                    # Calculate how long ago we followed them
                    try:
                        followed_date = datetime.fromisoformat(followed_at)
                        days_ago = (datetime.now() - followed_date).days
                    except:
                        days_ago = wait_days  # fallback
                        
                    log.info(f"Unfollowed user {user_id} (followed {days_ago} days ago)")
                    
                    # Smart delay
                    delay = get_smart_follow_delay(base_min=15, base_max=45, action_count=count_unfollowed)
                    time.sleep(delay)
                    
                    # Reset error counter on successful unfollow
                    follow_rate_limiter.reset_errors()
                    
                except ClientError as e:
                    error_result = handle_follow_client_error(e, user_id, "unfollow")
                    if error_result:  # Fatal error, stop the task
                        send_follow_telegram_notification(error_result)
                        return error_result
                    continue
                    
                except Exception as unfollow_error:
                    log.warning(f"Failed to unfollow user {user_id}: {unfollow_error}")
                    continue
                    
                # Send progress notification every 10 unfollows
                if count_unfollowed % 10 == 0:
                    send_follow_telegram_notification(f"📉 Progress: Unfollowed {count_unfollowed} users")
                    
            except Exception as e:
                log.exception(f"Unexpected unfollow error for user {user_id}: {e}")
                continue
                
        # Final summary
        strategy_name = "who didn't follow back" if unfollow_strategy == "not_following_back" else "old follows"
        summary_msg = f"✅ Auto-unfollow completed!\n"
        summary_msg += f"• Unfollowed: {count_unfollowed} users {strategy_name}\n"
        summary_msg += f"• Users processed: {users_processed}\n"
        summary_msg += f"• Users skipped: {users_skipped}\n"
        
        if skip_reasons:
            summary_msg += f"• Skip reasons: {dict(skip_reasons)}\n"
            
        summary_msg += f"• Hourly unfollows used: {follow_rate_limiter.hourly_unfollows}/20"
        
        send_follow_telegram_notification(summary_msg)
        return summary_msg
        
    except Exception as e:
        log.exception(f"Auto-unfollow overall error: {e}")
        error_msg = f"❌ An error occurred: {e}"
        send_follow_telegram_notification(error_msg)
        return error_msg

def add_to_blacklist(user_id: str) -> str:
    """Add user to blacklist to prevent future follows"""
    try:
        execute_db("INSERT OR REPLACE INTO blacklist_users (user_id) VALUES (?)", (user_id,))
        return f"✅ User {user_id} added to blacklist."
    except Exception as e:
        log.exception("Error adding user to blacklist: %s", e)
        return f"❌ Error adding user to blacklist: {e}"

def remove_from_blacklist(user_id: str) -> str:
    """Remove user from blacklist"""
    try:
        execute_db("DELETE FROM blacklist_users WHERE user_id=?", (user_id,))
        return f"✅ User {user_id} removed from blacklist."
    except Exception as e:
        log.exception("Error removing user from blacklist: %s", e)
        return f"❌ Error removing user from blacklist: {e}"

def get_follow_stats() -> Dict[str, any]:
    """Get comprehensive follow/unfollow statistics"""
    try:
        daily_limits = get_limits()
        total_followed = len(fetch_db("SELECT user_id FROM followed_users"))
        total_blacklisted = len(fetch_db("SELECT user_id FROM blacklist_users"))
        total_unfollowed_today = daily_limits["unfollows"]
        
        return {
            "total_follows_today": daily_limits["follows"],
            "total_unfollows_today": total_unfollowed_today,
            "hourly_follows": follow_rate_limiter.hourly_follows,
            "hourly_unfollows": follow_rate_limiter.hourly_unfollows,
            "daily_follow_cap": get_daily_cap("follows"),
            "daily_unfollow_cap": get_daily_cap("unfollows"),
            "currently_following": total_followed,
            "blacklisted_users": total_blacklisted,
            "remaining_daily_follows": get_daily_cap("follows") - daily_limits["follows"],
            "remaining_daily_unfollows": get_daily_cap("unfollows") - total_unfollowed_today,
            "remaining_hourly_follows": 15 - follow_rate_limiter.hourly_follows,
            "remaining_hourly_unfollows": 20 - follow_rate_limiter.hourly_unfollows
        }
    except Exception as e:
        log.error(f"Error getting follow stats: {e}")
        return {}

def reset_follow_hourly_limits():
    """Manual reset of hourly follow limits (admin function)"""
    global follow_rate_limiter
    follow_rate_limiter.hourly_follows = 0
    follow_rate_limiter.hourly_unfollows = 0
    follow_rate_limiter.hourly_reset_time = datetime.now() + timedelta(hours=1)
    log.info("Hourly follow/unfollow limits reset manually")
    return "✅ Hourly follow/unfollow limits reset"