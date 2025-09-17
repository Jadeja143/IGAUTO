# Instagram Automation Bot

## Overview

An advanced Instagram automation bot that provides comprehensive social media management through Telegram control. The system automates Instagram engagement activities including following/unfollowing users, hashtag-based targeting, story viewing, direct messaging, and engagement analytics. Built as a single-file Python application with SQLite persistence, it's designed for cloud deployment with keep-alive functionality and smart rate limiting to avoid Instagram's detection systems.

## User Preferences

Preferred communication style: Simple, everyday language.

## Recent Changes

### September 16, 2025
- ✅ **Project Import Complete**: Successfully imported Instagram automation bot from GitHub
- ✅ **Python 3.11 Setup**: Installed Python runtime and all required dependencies
- ✅ **Modular Architecture**: Configured to use modular `bot.py` entry point instead of monolithic `main.py`
- ✅ **Flask Server**: Running on port 5000 with 0.0.0.0 host binding for Replit compatibility
- ✅ **Database Initialized**: SQLite database and all tables created successfully
- ✅ **Telegram Bot**: Configured and polling for updates (requires TELEGRAM_BOT_TOKEN)
- ✅ **Instagram Session**: Existing session loaded successfully
- ✅ **Background Services**: Scheduler and keep-alive services running

## Environment Setup

### Required Environment Variables

The bot requires these environment variables to function properly:

**Telegram Bot Configuration:**
- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token (✅ Already configured)
- `ALLOWED_USER_ID`: Primary user ID allowed to use the bot (✅ Already configured)
- `ADMIN_USER_ID`: Admin user ID for management functions (✅ Already configured)

**Instagram Credentials (Optional):**
- `IG_USERNAME`: Instagram username (⚠️ Not set - can use /login command instead)
- `IG_PASSWORD`: Instagram password (⚠️ Not set - can use /login command instead)

**Rate Limiting Configuration (Optional):**
- `DAILY_FOLLOWS`: Daily follow limit (default: 50)
- `DAILY_UNFOLLOWS`: Daily unfollow limit (default: 50)
- `DAILY_LIKES`: Daily likes limit (default: 200)
- `DAILY_DMS`: Daily DMs limit (default: 10)
- `DAILY_STORY_VIEWS`: Daily story views limit (default: 500)

### Getting Started

1. **Instagram Login**: Use the Telegram bot command `/login <username> <password>` to authenticate
2. **Bot Commands**: Send `/help` to the Telegram bot to see all available commands
3. **Access Control**: Only users configured in ALLOWED_USER_ID and ADMIN_USER_ID can use the bot

## System Architecture

### Core Application Design
- **Modular architecture**: Application split into logical modules (`bot.py`, `database.py`, `instagram_client.py`, `telegram_handlers.py`) for maintainability
- **Event-driven control**: Telegram bot serves as the primary interface for all bot operations and monitoring
- **Persistent state management**: SQLite database stores user actions, rate limits, blacklists, follow timestamps, and hashtag configurations
- **Threaded execution**: Separate threads handle Telegram bot operations, scheduled tasks, and Flask keep-alive service

### Instagram Integration
- **instagrapi client**: Primary Instagram API wrapper providing robust session management and Instagram operation capabilities
- **Smart rate limiting**: Built-in delays and daily caps to mimic human behavior and avoid detection
- **Session persistence**: Maintains Instagram login sessions across restarts
- **Error handling**: Comprehensive exception handling for Instagram API limitations and challenges

### Data Persistence Layer
- **SQLite database**: Lightweight, file-based database for storing:
  - User follow/unfollow history with timestamps
  - Daily action counters and rate limits
  - Blacklisted users and accounts
  - Hashtag tiers and targeting configurations
  - DM templates and engagement rules

### Automation Features
- **Targeted following**: Hashtag-based user discovery with configurable tiers
- **Smart unfollowing**: Time-based unfollowing with blacklist protection
- **Story engagement**: Automated story viewing with optional emoji reactions
- **Direct messaging**: Personalized DM campaigns with human-like delays
- **Scheduled operations**: Background job scheduler for automated daily tasks

### Deployment Architecture
- **Flask keep-alive**: HTTP server on port 5000 configured for Replit environment with 0.0.0.0 host binding
- **Docker containerization**: Complete containerized deployment with system dependencies
- **Cloud-ready**: Designed for platforms like Koyeb, Heroku with Procfile configuration
- **Environment-based configuration**: All sensitive credentials managed through environment variables

### Control Interface
- **Telegram bot integration**: Complete remote control through Telegram commands
- **Real-time monitoring**: Live status updates and action confirmations
- **Configuration management**: Dynamic bot settings adjustment without restarts
- **Safety controls**: Emergency stop functionality and manual override capabilities

## External Dependencies

### Instagram API
- **instagrapi**: Primary Instagram automation library providing session management, media operations, and user interactions
- **Session handling**: Manages Instagram login persistence and challenge resolution

### Telegram Integration
- **python-telegram-bot v20.3**: Async Telegram bot framework for command handling and user interaction
- **Webhook support**: Supports both polling and webhook modes for Telegram updates

### Database
- **SQLite**: Built-in Python database for local data persistence without external database requirements

### Web Framework
- **Flask**: Lightweight HTTP server for keep-alive endpoints and health monitoring

### Task Scheduling
- **schedule**: Python job scheduling library for automated background tasks and maintenance operations

### HTTP Client
- **requests**: HTTP library for external API calls and keep-alive ping functionality

### System Dependencies
- **gcc**: Required for compiling certain Python packages
- **git**: Version control system dependency for package installations