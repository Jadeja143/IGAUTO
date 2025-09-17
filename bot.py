#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Instagram Automation Bot - Main Entry Point
Modular version with clean separation of concerns
"""

import os
import sys
import time
import asyncio
import logging
import threading
from typing import Dict, List, Optional

import schedule
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# Import our modular components
from database import initialize_database
from instagram_client import ensure_login, cl
from telegram_handlers import (
    start, help_command, stats_command, login_command, handle_callback_query,
    like_followers_command, follow_command, unfollow_command, view_stories_command,
    pending_requests_command, set_cap_command, blacklist_add_command,
    task_status_command, task_result_command, tasks_command,
    block_user_command, unblock_user_command, list_blocked_command
)
from utils import (
    add_location, remove_location, list_locations,
    add_default_hashtag, remove_default_hashtag, list_default_hashtags,
    add_hashtag, remove_hashtag, list_hashtags
)
from comment import initialize_comment_tables

# ---------------------------
# Logging Setup
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("instagram-bot")

# SECURITY: Disable HTTP request logging to prevent token exposure
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING) 
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ---------------------------
# Configuration
# ---------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

# Safety defaults
FOLLOW_WAIT_DAYS_MIN = int(os.environ.get("FOLLOW_WAIT_DAYS_MIN", "7"))
FOLLOW_WAIT_DAYS_MAX = int(os.environ.get("FOLLOW_WAIT_DAYS_MAX", "14"))

# ---------------------------
# Validation
# ---------------------------
if not TELEGRAM_BOT_TOKEN:
    log.warning("TELEGRAM_BOT_TOKEN not set — Telegram will fail.")
if ALLOWED_USER_ID == 0:
    log.warning("ALLOWED_USER_ID not set or zero — Telegram auth will fail until fixed.")
if ADMIN_USER_ID == 0:
    log.warning("ADMIN_USER_ID not set or zero — Admin features will not work.")

# ---------------------------
# Flask Keep-Alive Server
# ---------------------------
app = Flask(__name__)

@app.route('/')
def keep_alive():
    """Keep-alive endpoint for hosting platforms"""
    return jsonify({
        "status": "alive",
        "bot": "Instagram Automation Bot",
        "version": "2.0.0 - Modular",
        "instagram_logged_in": ensure_login(),
        "timestamp": time.time()
    })

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        instagram_status = ensure_login()
        return jsonify({
            "status": "healthy",
            "instagram": "connected" if instagram_status else "disconnected",
            "database": "connected",  # Database is always available (SQLite)
            "telegram": "configured" if TELEGRAM_BOT_TOKEN else "not_configured"
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

def run_flask():
    """Run Flask server for keep-alive."""
    app.run(host='0.0.0.0', port=5000, debug=False)

# ---------------------------
# Scheduled Tasks
# ---------------------------
def scheduled_cleanup():
    """Run scheduled cleanup tasks"""
    try:
        log.info("Running scheduled cleanup...")
        # You can add automatic cleanup tasks here
        # For example: auto_unfollow_old(FOLLOW_WAIT_DAYS_MAX)
        log.info("Scheduled cleanup completed.")
    except Exception as e:
        log.exception("Scheduled cleanup error: %s", e)

def setup_scheduler():
    """Set up scheduled tasks"""
    # Run cleanup daily at 3 AM
    schedule.every().day.at("03:00").do(scheduled_cleanup)
    
    def run_scheduler():
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
            except Exception as e:
                log.exception("Scheduler error: %s", e)
                time.sleep(300)  # Wait 5 minutes on error
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    log.info("Scheduler started.")

# ---------------------------
# Additional Command Handlers
# ---------------------------
async def add_location_command(update: Update, context):
    """Add location command"""
    from telegram_handlers import auth_required
    
    @auth_required
    async def _handler(update, context):
        if not context.args:
            if update.message:
                await update.message.reply_text("Usage: /add_location <location>")
            return
        
        location = " ".join(context.args)
        result = add_location(location)
        if update.message:
            await update.message.reply_text(result)
    
    await _handler(update, context)

async def list_locations_command(update: Update, context):
    """List locations command"""
    from telegram_handlers import auth_required
    
    @auth_required
    async def _handler(update, context):
        result = list_locations()
        if update.message:
            await update.message.reply_text(result)
    
    await _handler(update, context)

async def add_hashtag_command(update: Update, context):
    """Add hashtag command"""
    from telegram_handlers import auth_required
    
    @auth_required
    async def _handler(update, context):
        if not context.args:
            if update.message:
                await update.message.reply_text("Usage: /add_hashtag <hashtag> [tier]")
            return
        
        hashtag = context.args[0]
        tier = 2
        if len(context.args) > 1:
            try:
                tier = int(context.args[1])
            except ValueError:
                if update.message:
                    await update.message.reply_text("❌ Invalid tier. Using default tier 2.")
        
        result = add_hashtag(hashtag, tier)
        if update.message:
            await update.message.reply_text(result)
    
    await _handler(update, context)

async def list_hashtags_command(update: Update, context):
    """List hashtags command"""
    from telegram_handlers import auth_required
    
    @auth_required
    async def _handler(update, context):
        result = list_hashtags()
        if update.message:
            await update.message.reply_text(result)
    
    await _handler(update, context)

# ---------------------------
# Telegram Bot Setup
# ---------------------------
async def setup_telegram_bot():
    """Set up and run the Telegram bot"""
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not provided. Telegram bot will not start.")
        return
    
    try:
        # Create application
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CommandHandler("login", login_command))
        
        # Action commands
        application.add_handler(CommandHandler("follow", follow_command))
        application.add_handler(CommandHandler("unfollow", unfollow_command))
        application.add_handler(CommandHandler("like_followers", like_followers_command))
        application.add_handler(CommandHandler("view_stories", view_stories_command))
        
        # Management commands
        application.add_handler(CommandHandler("add_location", add_location_command))
        application.add_handler(CommandHandler("list_locations", list_locations_command))
        application.add_handler(CommandHandler("add_hashtag", add_hashtag_command))
        application.add_handler(CommandHandler("list_hashtags", list_hashtags_command))
        
        # Utility commands
        application.add_handler(CommandHandler("blacklist_add", blacklist_add_command))
        application.add_handler(CommandHandler("set_cap", set_cap_command))
        
        # Background task management commands
        application.add_handler(CommandHandler("task_status", task_status_command))
        application.add_handler(CommandHandler("task_result", task_result_command))
        application.add_handler(CommandHandler("tasks", tasks_command))
        
        # Admin commands
        application.add_handler(CommandHandler("pending_requests", pending_requests_command))
        application.add_handler(CommandHandler("block_user", block_user_command))
        application.add_handler(CommandHandler("unblock_user", unblock_user_command))
        application.add_handler(CommandHandler("list_blocked", list_blocked_command))
        
        # Callback query handler
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        
        log.info("Telegram bot handlers registered.")
        
        # Start polling
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        log.info("Telegram bot started and polling for updates.")
        
        # Keep the bot running indefinitely
        while True:
            await asyncio.sleep(1)
        
    except Exception as e:
        log.exception("Telegram bot error: %s", e)
        raise

# ---------------------------
# Main Application
# ---------------------------
def main():
    """Main application entry point"""
    try:
        log.info("Starting Instagram Automation Bot (Modular Version)...")
        
        # Initialize database
        log.info("Initializing database...")
        initialize_database()
        initialize_comment_tables()
        log.info("Database and comment tables initialized.")
        
        # Start Flask server in background
        log.info("Starting Flask keep-alive server...")
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        log.info("Flask server started on port 5000.")
        
        # Start scheduler
        log.info("Starting scheduler...")
        setup_scheduler()
        
        # Check Instagram login
        if ensure_login():
            log.info("Instagram client ready.")
        else:
            log.warning("Instagram client not logged in. Use /login command.")
        
        log.info("Background threads started.")
        
        # Start Telegram bot
        log.info("Starting Telegram bot...")
        asyncio.run(setup_telegram_bot())
        
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    except Exception as e:
        log.exception("Fatal error: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    log.info("Instagram Automation Bot started!")
    log.info("Commands: /start, /help, /stats, /follow, /login, /add_location, /add_hashtag")
    main()