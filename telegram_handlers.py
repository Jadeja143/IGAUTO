#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Bot Handlers Module
Handles all Telegram bot commands and interactions
"""

import logging
import asyncio
import threading
from typing import Dict, List, Optional
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (fetch_db, execute_db, get_limits, get_daily_cap, 
                     get_database_stats, set_daily_cap, initialize_database)
from instagram_client import ensure_login, login_instagram, logout_instagram, cl
from like import auto_like_followers, auto_like_following
from follow import auto_follow_targeted, auto_follow_location, auto_unfollow_old, add_to_blacklist, remove_from_blacklist
from view_story import auto_view_followers_stories, auto_view_following_stories
from utils import (is_authorized, request_access, approve_access, deny_access, list_pending_requests,
                  add_location, remove_location, list_locations, get_default_locations,
                  add_default_hashtag, remove_default_hashtag, list_default_hashtags, get_default_hashtags)

log = logging.getLogger("telegram_handlers")

# Configuration from environment
import os
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

# Background task manager
class BackgroundTaskManager:
    def __init__(self):
        self.active_tasks: Dict[str, threading.Thread] = {}
        self.task_results: Dict[str, str] = {}
        self.task_status: Dict[str, str] = {}
        self.lock = threading.Lock()
    
    def start_task(self, task_id: str, task_name: str, func, *args, **kwargs):
        """Start a task in background thread"""
        with self.lock:
            if task_id in self.active_tasks and self.active_tasks[task_id].is_alive():
                return f"⚠️ Task '{task_name}' is already running"
            
            def task_wrapper():
                try:
                    self.task_status[task_id] = "running"
                    result = func(*args, **kwargs)
                    self.task_results[task_id] = result
                    self.task_status[task_id] = "completed"
                    log.info(f"Background task '{task_name}' completed: {result}")
                except Exception as e:
                    error_msg = f"❌ Task '{task_name}' failed: {e}"
                    self.task_results[task_id] = error_msg
                    self.task_status[task_id] = "failed"
                    log.exception(f"Background task '{task_name}' failed: {e}")
            
            thread = threading.Thread(target=task_wrapper, name=task_name)
            thread.daemon = True
            thread.start()
            
            self.active_tasks[task_id] = thread
            self.task_status[task_id] = "starting"
            
            return f"✅ Started '{task_name}' in background"
    
    def get_task_status(self, task_id: str) -> str:
        """Get status of a specific task"""
        if task_id not in self.task_status:
            return "not_found"
        return self.task_status[task_id]
    
    def get_task_result(self, task_id: str) -> str:
        """Get result of a completed task"""
        return self.task_results.get(task_id, "No result available")
    
    def list_active_tasks(self) -> List[str]:
        """List all active task IDs"""
        with self.lock:
            active = []
            for task_id, thread in self.active_tasks.items():
                if thread.is_alive() or self.task_status.get(task_id) == "completed":
                    status = self.task_status.get(task_id, "unknown")
                    active.append(f"{task_id}: {status}")
            return active
    
    def stop_task(self, task_id: str) -> str:
        """Attempt to stop a task (limited capability with threads)"""
        with self.lock:
            if task_id not in self.active_tasks:
                return f"❌ Task '{task_id}' not found"
            
            thread = self.active_tasks[task_id]
            if not thread.is_alive():
                return f"ℹ️ Task '{task_id}' is not running"
            
            # Note: Python threads cannot be forcefully stopped
            # We can only mark them for stopping if the task checks for it
            self.task_status[task_id] = "stop_requested"
            return f"⚠️ Stop requested for '{task_id}' (task must check for stop signal)"

# Global task manager instance
task_manager = BackgroundTaskManager()

def auth_required(func):
    """Decorator to check if user is authorized."""
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Fix: Add null checks
        if not update or not update.effective_user:
            log.warning("Received update without effective_user")
            return
            
        user_id = update.effective_user.id
        if not is_authorized(user_id):
            keyboard = [[InlineKeyboardButton("Request Access", callback_data=f"request_access_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Fix: Add null check for message
            if update.message:
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
        # Fix: Add null checks
        if not update or not update.effective_user:
            log.warning("Received update without effective_user")
            return
            
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            if update.message:
                await update.message.reply_text("🚫 This command is only available to admins.")
            return
        return await func(update, context)
    return wrapper

async def run_instagram_task(update, task_name: str, func, *args, background=True, **kwargs):
    """Run an Instagram task in background or foreground."""
    try:
        if background:
            # Generate unique task ID
            user_id = update.effective_user.id if update.effective_user else "unknown"
            task_id = f"{user_id}_{task_name.lower().replace(' ', '_')}"
            
            # Start background task
            start_msg = task_manager.start_task(task_id, task_name, func, *args, **kwargs)
            
            if update.message:
                await update.message.reply_text(
                    f"{start_msg}\n\n"
                    f"📋 Use /task_status {task_id} to check progress\n"
                    f"📋 Use /task_result {task_id} to get result when completed\n"
                    f"📋 Use /tasks to see all your active tasks"
                )
        else:
            # Run in foreground (old behavior)
            if update.message:
                await update.message.reply_text(f"🔄 Starting {task_name}...")
            result = await asyncio.get_event_loop().run_in_executor(None, func, *args, **kwargs)
            if update.message:
                await update.message.reply_text(result)
    except Exception as e:
        log.exception(f"Task {task_name} failed: %s", e)
        if update.message:
            await update.message.reply_text(f"❌ {task_name} failed: {e}")

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    if not update or not update.effective_user:
        return
        
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    
    if is_authorized(user_id):
        if update.message:
            await update.message.reply_text(
                f"Welcome back! You have access to the Instagram automation bot.\n\n"
                f"Available commands:\n"
                f"/help - Show all commands\n"
                f"/stats - Show bot statistics\n"
                f"/follow - Follow users from hashtags/locations\n"
                f"/like_followers - Like posts from followers\n"
                f"/view_stories - View stories from followers"
            )
    else:
        keyboard = [[InlineKeyboardButton("Request Access", callback_data=f"request_access_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.message:
            await update.message.reply_text(
                f"🔐 Welcome to the Instagram Automation Bot!\n\n"
                f"This bot requires admin approval to use. Please request access below.",
                reply_markup=reply_markup
            )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries from inline keyboards"""
    if not update or not update.callback_query:
        return
        
    query = update.callback_query
    await query.answer()
    
    if not query.data or not query.from_user:
        return
    
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.username or "Unknown"
    
    if data.startswith("request_access_"):
        # Handle access request
        # First check if user is blocked
        blocked_status = fetch_db("SELECT status FROM access_requests WHERE user_id=? AND status='blocked'", (str(user_id),))
        if blocked_status:
            await query.edit_message_text("🚫 You are blocked from using this bot. Contact the admin if you believe this is an error.")
            return
        
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
                        message += f"User: @{req_username} (ID: {req_user_id})\n"
                        message += f"Requested at: {requested_at}\n"
                        
                        await context.bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text=message,
                            reply_markup=reply_markup
                        )
                except Exception as e:
                    log.error(f"Could not notify admin: {e}")
    
    elif data.startswith("approve_"):
        # Handle approval - ADMIN ONLY
        if user_id != ADMIN_USER_ID:
            await query.edit_message_text("🚫 Unauthorized: Only admins can approve requests.")
            log.warning(f"Unauthorized approve attempt by user {user_id} (@{username})")
            return
        target_user_id = data.split("_")[1]
        result = approve_access(target_user_id, user_id)
        await query.edit_message_text(result)
    
    elif data.startswith("deny_"):
        # Handle denial - ADMIN ONLY  
        if user_id != ADMIN_USER_ID:
            await query.edit_message_text("🚫 Unauthorized: Only admins can deny requests.")
            log.warning(f"Unauthorized deny attempt by user {user_id} (@{username})")
            return
        target_user_id = data.split("_")[1]
        result = deny_access(target_user_id)
        await query.edit_message_text(result)

@auth_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message with all commands"""
    help_text = """
🤖 **Instagram Automation Bot Commands**

**Basic Commands:**
/start - Start the bot
/help - Show this help message
/stats - Show bot statistics and daily limits

**Instagram Actions (Background Tasks):**
/login - Login to Instagram account
/like_followers [likes_per_user] - Auto-like posts from followers
/follow <term> [amount] [type] - Follow users from hashtag or location
  Examples: /follow photography 20, /follow rajkot 15 location
/unfollow [days] - Unfollow old users who didn't follow back
/view_stories - View stories from followers/following

**Background Task Management:**
/tasks - List your active background tasks
/task_status <task_id> - Check status of a specific task
/task_result <task_id> - Get result of completed task

**Management:**
/add_hashtag <hashtag> <tier> - Add hashtag with tier (1-3)
/remove_hashtag <hashtag> - Remove hashtag
/list_hashtags - List all hashtags
/add_location <location> - Add default location
/remove_location <location> - Remove location
/list_locations - List all locations

**Blacklist:**
/blacklist_add <user_id> - Add user to blacklist
/blacklist_remove <user_id> - Remove user from blacklist

**Settings:**
/set_cap <action> <number> - Set daily cap (follows/likes/etc)
/reset_limits - Reset today's action counts

**Admin Only:**
/pending_requests - View pending access requests
/block_user <user_id> - Block user from bot access
/unblock_user <user_id> - Unblock previously blocked user
/list_blocked - View all blocked users
/authorized_users - View authorized users

💡 **New Features:**
• All tasks now run in background simultaneously
• Auto-adds # to hashtags if you forget
• Location-based following available
• Admin can block/approve users
"""
    
    if update.message:
        await update.message.reply_text(help_text)

@auth_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    try:
        limits = get_limits()
        caps = {
            'follows': get_daily_cap('follows'),
            'unfollows': get_daily_cap('unfollows'), 
            'likes': get_daily_cap('likes'),
            'dms': get_daily_cap('dms'),
            'story_views': get_daily_cap('story_views')
        }
        
        db_stats = get_database_stats()
        
        stats_text = f"""📊 **Bot Statistics**

🎯 **Daily Activity:**
  Follows: {limits['follows']}/{caps['follows']}
  Unfollows: {limits['unfollows']}/{caps['unfollows']}
  Likes: {limits['likes']}/{caps['likes']}
  DMs: {limits['dms']}/{caps['dms']}
  Story Views: {limits['story_views']}/{caps['story_views']}

📊 **Database Stats:**
  Currently Following: {db_stats['followed_count']}
  Blacklisted Users: {db_stats['blacklist_count']}
  Configured Hashtags: {db_stats['hashtag_count']}
  Default Hashtags: {db_stats['default_hashtag_count']}
  Stored Locations: {db_stats['location_count']}

👥 **Access Control:**
  Authorized Users: {db_stats['authorized_count']}
  Pending Requests: {db_stats['pending_count']}

🔐 **Instagram Status:** {'✅ Logged in' if ensure_login() else '❌ Not logged in'}
"""
        
        if update.message:
            await update.message.reply_text(stats_text)
    except Exception as e:
        log.exception("Error getting stats: %s", e)
        if update.message:
            await update.message.reply_text(f"❌ Error getting stats: {e}")

@auth_required
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Login to Instagram"""
    if not context.args or len(context.args) < 2:
        if update.message:
            await update.message.reply_text("Usage: /login <username> <password>")
        return
    
    username = context.args[0]
    password = context.args[1]
    
    await run_instagram_task(update, "Instagram Login", login_instagram, username, password)

@auth_required
async def like_followers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-like posts from followers"""
    likes_per_user = 2
    if context.args and len(context.args) > 0:
        try:
            likes_per_user = int(context.args[0])
        except ValueError:
            if update.message:
                await update.message.reply_text("❌ Invalid number. Using default value 2.")
    
    await run_instagram_task(update, "Auto-like followers", auto_like_followers, likes_per_user)

@auth_required
async def follow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Follow users from hashtag or location"""
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /follow <hashtag_or_location> [amount] [type]\nExamples:\n  /follow photography 20\n  /follow photography 20 hashtag\n  /follow rajkot 15 location")
        return
    
    search_term = context.args[0]
    amount = 20
    follow_type = "hashtag"  # default to hashtag
    
    # Parse amount
    if len(context.args) > 1:
        try:
            amount = int(context.args[1])
        except ValueError:
            if update.message:
                await update.message.reply_text("❌ Invalid amount. Using default value 20.")
    
    # Parse follow type
    if len(context.args) > 2:
        follow_type = context.args[2].lower()
        if follow_type not in ["hashtag", "location"]:
            if update.message:
                await update.message.reply_text("❌ Invalid type. Use 'hashtag' or 'location'. Using default 'hashtag'.")
            follow_type = "hashtag"
    
    # Auto-add # prefix for hashtags if missing
    if follow_type == "hashtag":
        if not search_term.startswith("#"):
            search_term = f"#{search_term}"
        hashtag = search_term.replace("#", "")
        await run_instagram_task(update, f"Auto-follow hashtag #{hashtag}", auto_follow_targeted, hashtag, amount)
    else:  # location
        from follow import auto_follow_location
        await run_instagram_task(update, f"Auto-follow location '{search_term}'", auto_follow_location, search_term, amount)

@auth_required
async def unfollow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unfollow old users"""
    wait_days = 7
    if context.args and len(context.args) > 0:
        try:
            wait_days = int(context.args[0])
        except ValueError:
            if update.message:
                await update.message.reply_text("❌ Invalid number. Using default value 7.")
    
    await run_instagram_task(update, "Auto-unfollow", auto_unfollow_old, wait_days)

@auth_required
async def view_stories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View stories from followers/following"""
    await run_instagram_task(update, "View stories", auto_view_followers_stories)

# Admin commands
@admin_required
async def pending_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View pending access requests"""
    try:
        requests = list_pending_requests()
        if not requests:
            if update.message:
                await update.message.reply_text("📝 No pending access requests.")
            return
        
        message = "📝 **Pending Access Requests:**\n\n"
        reply_markup = None
        for user_id, username, requested_at in requests:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"deny_{user_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            message += f"User: @{username} (ID: {user_id})\n"
            message += f"Requested: {requested_at}\n\n"
        
        if update.message:
            await update.message.reply_text(message, reply_markup=reply_markup)
    except Exception as e:
        log.exception("Error getting pending requests: %s", e)
        if update.message:
            await update.message.reply_text(f"❌ Error: {e}")

# Additional utility commands
@auth_required
async def set_cap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set daily cap for an action"""
    if not context.args or len(context.args) < 2:
        if update.message:
            await update.message.reply_text("Usage: /set_cap <action> <number>\nActions: follows, likes, unfollows, dms, story_views")
        return
    
    action = context.args[0]
    try:
        cap = int(context.args[1])
        set_daily_cap(action, cap)
        if update.message:
            await update.message.reply_text(f"✅ Daily cap for {action} set to {cap}")
    except ValueError:
        if update.message:
            await update.message.reply_text("❌ Invalid number")
    except Exception as e:
        if update.message:
            await update.message.reply_text(f"❌ Error: {e}")

@auth_required
async def blacklist_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add user to blacklist"""
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /blacklist_add <user_id>")
        return
    
    user_id = context.args[0]
    result = add_to_blacklist(user_id)
    if update.message:
        await update.message.reply_text(result)

# Background task management commands
@auth_required
async def task_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check status of a background task"""
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /task_status <task_id>")
        return
    
    task_id = context.args[0]
    status = task_manager.get_task_status(task_id)
    
    if status == "not_found":
        if update.message:
            await update.message.reply_text(f"❌ Task '{task_id}' not found")
    else:
        if update.message:
            await update.message.reply_text(f"📋 Task '{task_id}': {status}")

@auth_required
async def task_result_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get result of a completed background task"""
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /task_result <task_id>")
        return
    
    task_id = context.args[0]
    status = task_manager.get_task_status(task_id)
    
    if status == "not_found":
        if update.message:
            await update.message.reply_text(f"❌ Task '{task_id}' not found")
    elif status in ["completed", "failed"]:
        result = task_manager.get_task_result(task_id)
        if update.message:
            await update.message.reply_text(f"📋 Task '{task_id}' result:\n\n{result}")
    else:
        if update.message:
            await update.message.reply_text(f"⏳ Task '{task_id}' is still {status}. Please wait...")

@auth_required
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all active background tasks"""
    user_id = update.effective_user.id if update.effective_user else "unknown"
    user_prefix = f"{user_id}_"
    
    # Filter tasks for current user
    all_tasks = task_manager.list_active_tasks()
    user_tasks = [task for task in all_tasks if task.startswith(user_prefix)]
    
    if not user_tasks:
        if update.message:
            await update.message.reply_text("📋 No active background tasks")
    else:
        task_list = "📋 Your active background tasks:\n\n"
        for task in user_tasks:
            task_list += f"• {task.replace(user_prefix, '')}\n"
        
        if update.message:
            await update.message.reply_text(task_list)

# Admin commands for blocking users
@admin_required
async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block a user from using the bot"""
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /block_user <user_id>")
        return
    
    user_id = context.args[0]
    try:
        # Add to blacklist and remove from authorized users
        execute_db("DELETE FROM authorized_users WHERE user_id=?", (user_id,))
        execute_db("UPDATE access_requests SET status='blocked' WHERE user_id=?", (user_id,))
        
        if update.message:
            await update.message.reply_text(f"🚫 User {user_id} has been blocked from using the bot")
    except Exception as e:
        log.exception(f"Error blocking user {user_id}: {e}")
        if update.message:
            await update.message.reply_text(f"❌ Error blocking user: {e}")

@admin_required
async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unblock a user (remove block status)"""
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /unblock_user <user_id>")
        return
    
    user_id = context.args[0]
    try:
        # Update access request status to allow re-requesting
        execute_db("UPDATE access_requests SET status='pending' WHERE user_id=?", (user_id,))
        
        if update.message:
            await update.message.reply_text(f"✅ User {user_id} has been unblocked and can request access again")
    except Exception as e:
        log.exception(f"Error unblocking user {user_id}: {e}")
        if update.message:
            await update.message.reply_text(f"❌ Error unblocking user: {e}")

@admin_required
async def list_blocked_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all blocked users"""
    try:
        blocked_users = fetch_db("SELECT user_id, username FROM access_requests WHERE status='blocked' ORDER BY requested_at DESC")
        
        if not blocked_users:
            if update.message:
                await update.message.reply_text("🚫 No blocked users")
        else:
            message = "🚫 **Blocked Users:**\n\n"
            for user_id, username in blocked_users:
                message += f"• @{username} (ID: {user_id})\n"
            
            if update.message:
                await update.message.reply_text(message)
    except Exception as e:
        log.exception(f"Error listing blocked users: {e}")
        if update.message:
            await update.message.reply_text(f"❌ Error: {e}")