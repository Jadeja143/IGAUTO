#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Auto-Comment Logic Module
Handles auto-comment functionality for Instagram bot
"""

import time
import random
import logging
from typing import List, Dict, Optional

from instagrapi.exceptions import ClientError

from database import fetch_db, execute_db, increment_limit, get_limits, get_daily_cap, reset_daily_limits_if_needed
from instagram_client import ensure_login, with_client, cl, safe_hashtag_medias_recent, safe_user_medias

log = logging.getLogger("comment")

# Default comment templates
DEFAULT_COMMENTS = [
    "Nice! 🔥",
    "Love this! ❤️",
    "Amazing! ✨",
    "Great post! 👍",
    "Awesome! 🙌",
    "Beautiful! 😍",
    "Perfect! 💯",
    "Incredible! 🤩",
    "Fantastic! 🌟",
    "Outstanding! 👏"
]

def auto_comment_hashtag(hashtag: str, amount: int = 10, comments: Optional[List[str]] = None, daily_cap_check: bool = True) -> str:
    """Auto-comment on posts from a specific hashtag"""
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    if comments is None:
        comments = DEFAULT_COMMENTS
        
    try:
        reset_daily_limits_if_needed()
        
        # Get daily cap for comments (we'll use 'dms' field for comments)
        if daily_cap_check and get_limits()["dms"] >= get_daily_cap("dms"):
            return "📝 Daily comment cap reached."
            
        medias = safe_hashtag_medias_recent(hashtag, amount=amount * 2)  # Get extra in case some filtered
        count_commented = 0
        
        for m in medias:
            try:
                if daily_cap_check and get_limits()["dms"] >= get_daily_cap("dms"):
                    log.info("Daily comments cap reached.")
                    break
                    
                # Check if already commented on this post
                result = fetch_db("SELECT 1 FROM commented_posts WHERE post_id=?", (str(m.pk),))
                if result:
                    continue
                
                # Skip our own posts
                if str(m.user.pk) == str(cl.user_id):
                    continue
                    
                # Choose random comment
                comment_text = random.choice(comments)
                
                # Post comment
                with_client(cl.media_comment, m.pk, comment_text)
                
                # Save to database
                execute_db("INSERT OR REPLACE INTO commented_posts (post_id) VALUES (?)", (str(m.pk),))
                increment_limit("dms", 1)  # Using dms field for comments
                count_commented += 1
                
                log.info("Commented on media %s with: %s", m.pk, comment_text)
                
                if count_commented >= amount:
                    break
                    
                # Wait between comments to avoid spam detection
                time.sleep(random.uniform(15, 45))
                
            except ClientError as e:
                log.warning("Comment error for media %s: %s", m.pk, e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected comment error for media %s: %s", m.pk, e)
                
        return f"✅ Auto-comment done! Commented on {count_commented} posts from #{hashtag}."
    except Exception as e:
        log.exception("Auto-comment overall error: %s", e)
        return f"An error occurred: {e}"

def auto_comment_followers(comments_per_user: int = 1, comments: Optional[List[str]] = None, daily_cap_check: bool = True) -> str:
    """Auto-comment on recent posts from your followers"""
    if not ensure_login():
        return "🚫 Instagram not logged in."
        
    if comments is None:
        comments = DEFAULT_COMMENTS
        
    try:
        reset_daily_limits_if_needed()
        
        # Get followers list
        followers_list = with_client(cl.user_followers_v1, cl.user_id)
        count_commented = 0
        
        for follower in followers_list:
            try:
                if daily_cap_check and get_limits()["dms"] >= get_daily_cap("dms"):
                    log.info("Daily comments cap reached.")
                    break
                    
                user_id = str(follower.pk)
                
                # Get recent medias from this user
                medias = safe_user_medias(user_id, amount=comments_per_user * 2)
                if not medias:
                    continue
                    
                user_comment_count = 0
                for m in medias:
                    if user_comment_count >= comments_per_user:
                        break
                        
                    # Check if already commented
                    result = fetch_db("SELECT 1 FROM commented_posts WHERE post_id=?", (str(m.pk),))
                    if result:
                        continue
                        
                    try:
                        # Choose random comment
                        comment_text = random.choice(comments)
                        
                        # Post comment
                        with_client(cl.media_comment, m.pk, comment_text)
                        
                        # Save to database
                        execute_db("INSERT OR REPLACE INTO commented_posts (post_id) VALUES (?)", (str(m.pk),))
                        increment_limit("dms", 1)
                        count_commented += 1
                        user_comment_count += 1
                        
                        log.info("Commented on media %s from user %s with: %s", m.pk, user_id, comment_text)
                        
                        # Wait between comments
                        time.sleep(random.uniform(10, 30))
                        
                    except Exception as comment_error:
                        log.warning(f"Failed to comment on media {m.pk} from user {user_id}: {comment_error}")
                        continue
                        
            except ClientError as e:
                log.warning("Comment error user %s: %s", follower.pk, e)
                time.sleep(60)
            except Exception as e:
                log.exception("Unexpected comment error for user %s: %s", follower.pk, e)
                
        return f"✅ Auto-comment followers done! Commented on {count_commented} posts."
    except Exception as e:
        log.exception("Auto-comment followers overall error: %s", e)
        return f"An error occurred: {e}"

def get_comment_templates() -> List[str]:
    """Get list of available comment templates"""
    try:
        result = fetch_db("SELECT template FROM comment_templates ORDER BY template")
        if result:
            return [row[0] for row in result]
        return DEFAULT_COMMENTS
    except Exception:
        return DEFAULT_COMMENTS

def add_comment_template(template: str) -> str:
    """Add a new comment template"""
    try:
        # Create table if it doesn't exist
        execute_db("""CREATE TABLE IF NOT EXISTS comment_templates (
            template TEXT PRIMARY KEY,
            added_at TEXT
        )""")
        
        execute_db("INSERT OR REPLACE INTO comment_templates (template, added_at) VALUES (?, ?)",
                   (template, time.time()))
        return f"✅ Added comment template: {template}"
    except Exception as e:
        log.exception("Error adding comment template: %s", e)
        return f"❌ Error adding template: {e}"

def remove_comment_template(template: str) -> str:
    """Remove a comment template"""
    try:
        execute_db("DELETE FROM comment_templates WHERE template=?", (template,))
        return f"✅ Removed comment template: {template}"
    except Exception as e:
        log.exception("Error removing comment template: %s", e)
        return f"❌ Error removing template: {e}"

def list_comment_templates() -> str:
    """List all comment templates"""
    try:
        templates = get_comment_templates()
        if not templates:
            return "💬 No custom comment templates. Using defaults."
        
        result = "💬 Comment templates:\n"
        for template in templates:
            result += f"  • {template}\n"
        return result
    except Exception as e:
        log.exception("Error listing comment templates: %s", e)
        return f"❌ Error: {e}"

# Initialize commented_posts table
def initialize_comment_tables():
    """Initialize comment-related database tables"""
    try:
        execute_db("""CREATE TABLE IF NOT EXISTS commented_posts (
            post_id TEXT PRIMARY KEY,
            commented_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        
        execute_db("""CREATE TABLE IF NOT EXISTS comment_templates (
            template TEXT PRIMARY KEY,
            added_at TEXT
        )""")
    except Exception as e:
        log.exception("Error initializing comment tables: %s", e)