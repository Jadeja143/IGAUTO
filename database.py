#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Database Operations Module
Handles all SQLite database operations for Instagram bot
"""

import sqlite3
import threading
import logging
from datetime import datetime, date
from typing import Dict, List, Tuple, Optional

log = logging.getLogger("database")

# Database configuration
DB_FILE = "bot_data.sqlite"
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

def bulk_insert(table: str, columns: List[str], data: List[Tuple], replace: bool = True) -> int:
    """
    Efficient bulk insert using executemany for better performance.
    
    Args:
        table: Table name to insert into
        columns: List of column names
        data: List of tuples with data to insert
        replace: If True, uses INSERT OR REPLACE (default), otherwise INSERT OR IGNORE
        
    Returns:
        Number of rows affected
    """
    if not data:
        return 0
        
    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()
            
            # Build the query
            columns_str = ", ".join(columns)
            placeholders = ", ".join(["?" for _ in columns])
            
            if replace:
                query = f"INSERT OR REPLACE INTO {table} ({columns_str}) VALUES ({placeholders})"
            else:
                query = f"INSERT OR IGNORE INTO {table} ({columns_str}) VALUES ({placeholders})"
            
            # Execute bulk insert
            cur.executemany(query, data)
            rows_affected = cur.rowcount
            conn.commit()
            
            log.debug(f"Bulk inserted {rows_affected} rows into {table}")
            return rows_affected

def initialize_database():
    """Initialize all database tables"""
    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()
            
            # Basic tables
            cur.execute("""CREATE TABLE IF NOT EXISTS liked_posts (post_id TEXT PRIMARY KEY)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS viewed_stories (story_id TEXT PRIMARY KEY)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS unfollowed_users (user_id TEXT PRIMARY KEY)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS credentials (key TEXT PRIMARY KEY, value TEXT)""")
            
            # Advanced tables
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
            
            # Admin access control tables
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
            
            # Location and hashtag management tables
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
            
            # Caps table
            cur.execute("""CREATE TABLE IF NOT EXISTS caps (action TEXT PRIMARY KEY, cap INTEGER)""")
            
            conn.commit()

# Daily limits management
def get_today_str() -> str:
    """Get today's date as string"""
    return date.today().isoformat()

def reset_daily_limits_if_needed():
    """Reset daily limits if it's a new day"""
    today = get_today_str()
    result = fetch_db("SELECT 1 FROM daily_limits WHERE day=?", (today,))
    if not result:
        execute_db("INSERT OR REPLACE INTO daily_limits (day) VALUES (?)", (today,))

def increment_limit(action: str, amount: int = 1):
    """Increment daily limit counter for an action"""
    reset_daily_limits_if_needed()
    today = get_today_str()
    execute_db(f"UPDATE daily_limits SET {action} = {action} + ? WHERE day=?", (amount, today))

def get_limits() -> Dict[str, int]:
    """Get current daily limits"""
    reset_daily_limits_if_needed()
    today = get_today_str()
    result = fetch_db("SELECT follows, unfollows, likes, dms, story_views FROM daily_limits WHERE day=?", (today,))
    if result:
        r = result[0]
        return {"follows": r[0], "unfollows": r[1], "likes": r[2], "dms": r[3], "story_views": r[4]}
    return {"follows": 0, "unfollows": 0, "likes": 0, "dms": 0, "story_views": 0}

def set_daily_cap(action: str, cap: int):
    """Set daily cap for an action"""
    execute_db("INSERT OR REPLACE INTO caps (action, cap) VALUES (?, ?)", (action, cap))

def get_daily_cap(action: str) -> int:
    """Get daily cap for an action"""
    result = fetch_db("SELECT cap FROM caps WHERE action=?", (action,))
    if result:
        return int(result[0][0])
    # Default caps
    default_caps = {
        "follows": 50,
        "unfollows": 50,
        "likes": 200,
        "dms": 10,
        "story_views": 500,
    }
    return default_caps.get(action, 99999)

# Statistics
def get_database_stats() -> Dict[str, int]:
    """Get database statistics"""
    stats = {}
    
    # Count followed users
    result = fetch_db("SELECT COUNT(*) FROM followed_users")
    stats['followed_count'] = result[0][0] if result else 0
    
    # Count blacklisted users
    result = fetch_db("SELECT COUNT(*) FROM blacklist_users")
    stats['blacklist_count'] = result[0][0] if result else 0
    
    # Count hashtags
    result = fetch_db("SELECT COUNT(*) FROM hashtags")
    stats['hashtag_count'] = result[0][0] if result else 0
    
    # Count default hashtags
    result = fetch_db("SELECT COUNT(*) FROM default_hashtags")
    stats['default_hashtag_count'] = result[0][0] if result else 0
    
    # Count locations
    result = fetch_db("SELECT COUNT(*) FROM locations")
    stats['location_count'] = result[0][0] if result else 0
    
    # Count authorized users
    result = fetch_db("SELECT COUNT(*) FROM authorized_users")
    stats['authorized_count'] = result[0][0] if result else 0
    
    # Count pending requests
    result = fetch_db("SELECT COUNT(*) FROM access_requests WHERE status='pending'")
    stats['pending_count'] = result[0][0] if result else 0
    
    return stats