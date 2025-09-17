#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Utility Functions Module
Contains helper functions for access control, location management, etc.
"""

import logging
from datetime import datetime
from typing import List, Tuple, Optional

from database import fetch_db, execute_db

log = logging.getLogger("utils")

# Configuration from environment
import os
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

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
# Hashtag tier management functions
# ---------------------------
def add_hashtag(hashtag: str, tier: int = 2) -> str:
    """Add a hashtag with tier level."""
    hashtag = hashtag.lower().strip().replace("#", "")
    if tier not in [1, 2, 3]:
        return "❌ Tier must be 1, 2, or 3"
    
    execute_db("INSERT OR REPLACE INTO hashtags (tag, tier) VALUES (?, ?)", (hashtag, tier))
    return f"✅ Added hashtag #{hashtag} with tier {tier}"

def remove_hashtag(hashtag: str) -> str:
    """Remove a hashtag from the list."""
    hashtag = hashtag.lower().strip().replace("#", "")
    execute_db("DELETE FROM hashtags WHERE tag=?", (hashtag,))
    return f"✅ Removed hashtag #{hashtag}"

def list_hashtags() -> str:
    """List all hashtags with their tiers."""
    hashtags = fetch_db("SELECT tag, tier FROM hashtags ORDER BY tier, tag")
    if not hashtags:
        return "🏷️ No hashtags configured."
    
    result = "🏷️ Configured hashtags:\n"
    for tag, tier in hashtags:
        result += f"  • #{tag} (Tier {tier})\n"
    return result

def get_hashtags_by_tier(tier: int) -> List[str]:
    """Get hashtags by tier level."""
    hashtags = fetch_db("SELECT tag FROM hashtags WHERE tier=? ORDER BY tag", (tier,))
    return [tag[0] for tag in hashtags]

# ---------------------------
# General utility functions
# ---------------------------
def format_number(num: int) -> str:
    """Format number with appropriate suffix (K, M, B)."""
    if num >= 1000000000:
        return f"{num/1000000000:.1f}B"
    elif num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    else:
        return str(num)

def validate_username(username: str) -> bool:
    """Validate Instagram username format."""
    if not username:
        return False
    # Remove @ if present
    username = username.lstrip('@')
    # Check length and characters
    if len(username) < 1 or len(username) > 30:
        return False
    # Check for valid characters (letters, numbers, periods, underscores)
    import re
    return bool(re.match(r'^[a-zA-Z0-9._]+$', username))

def sanitize_hashtag(hashtag: str) -> str:
    """Sanitize hashtag input."""
    # Remove # if present
    hashtag = hashtag.lstrip('#')
    # Convert to lowercase and strip whitespace
    hashtag = hashtag.lower().strip()
    # Remove any invalid characters
    import re
    hashtag = re.sub(r'[^a-zA-Z0-9_]', '', hashtag)
    return hashtag

def get_time_ago(timestamp_str: str) -> str:
    """Get human-readable time ago from ISO timestamp."""
    try:
        timestamp = datetime.fromisoformat(timestamp_str)
        now = datetime.now()
        diff = now - timestamp
        
        if diff.days > 0:
            return f"{diff.days}d ago"
        elif diff.seconds >= 3600:
            hours = diff.seconds // 3600
            return f"{hours}h ago"
        elif diff.seconds >= 60:
            minutes = diff.seconds // 60
            return f"{minutes}m ago"
        else:
            return "Just now"
    except Exception:
        return "Unknown"