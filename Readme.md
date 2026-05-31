# Instagram Downloader Telegram Bot

> **⚠️ DISCLAIMER: EDUCATIONAL PROJECT**
> 
> This project is created for **educational purposes only**. It demonstrates Python programming concepts, Telegram Bot API integration, and web scraping techniques.
> 
> - This bot is **NOT intended for production use** or actual content downloading
> - Downloading Instagram content may violate Instagram's Terms of Service
> - Respect content creators' rights and intellectual property
> - Users are solely responsible for complying with applicable laws and regulations
> - The developers assume **NO liability** for any misuse of this software
> - This project was built as a coding exercise using **Vibe Coding** methodology with DeepSeek AI assistance

---

## 📚 About This Project

This Telegram bot downloads Instagram content (posts, reels, stories, profile pictures) and delivers them directly to Telegram. It demonstrates integration of Telegram Bot API, gallery-dl, async I/O, batch media uploading, and persistent caching in a single Python application.

**Development Methodology:** Created using **Vibe Coding** - AI-assisted development through natural language interaction with DeepSeek AI.

---

## 🚀 Features

- 🖼️ **Post Download** - All images from carousel posts as media groups
- 🎬 **Reel Download** - Instagram Reels as MP4 video
- 📖 **Story Download** - Story images and videos (before 24h expiry)
- 👤 **Profile Pictures** - Download profile pictures
- ⚡ **Auto-Download** - Just send a link, bot handles everything
- 📦 **Batch Image Upload** - Multiple images sent as Telegram media groups (up to 10 per batch)
- 💾 **Smart Caching** - Prevents re-downloading same content
- 🔒 **Concurrent Protection** - Download locks prevent duplicate downloads
- 🗑️ **Auto-Cleanup** - Files deleted after configurable days (default: 2)
- 🍪 **Cookie Management** - Per-user cookie storage with validation
- 👥 **Whitelist System** - Restrict bot to specific users
- 📱 **Download History** - View and resend previously downloaded content
- 🔄 **Resend Support** - Resend cached media without re-downloading

---

## 📋 Prerequisites

### System Requirements
- Python 3.8+
- Linux (recommended) / macOS / Windows
- gallery-dl (auto-installed if missing)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### No FFmpeg Required
Unlike the YouTube bot, FFmpeg is not required for Instagram downloads. gallery-dl handles all media types natively.

---

## 📦 Installation

### Step 1: Clone and Setup
```bash
git clone https://github.com/HoomanJCode/Telegram_Insta_Bot.git
cd Telegram_Insta_Bot
python3 -m venv venv
source venv/bin/activate
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```
### Step 3: Configure Environment
Create `.env` file:
```env
BOT_TOKEN=your_bot_token_here
WHITELIST_USERS=123456789,987654321
STORAGE_DAYS=2
MAX_TELEGRAM_FILE_SIZE=50
```

### Step 4: Create Required Directories
```bash
mkdir -p data/cookies downloads
```

### Step 5: Run
```bash
python bot.py
```

---

## ⚙️ Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | Telegram Bot API token | Required |
| `WHITELIST_USERS` | Comma-separated authorized user IDs | Empty (all allowed) |
| `STORAGE_DAYS` | Days before files auto-delete | 2 |
| `MAX_TELEGRAM_FILE_SIZE` | Max size for Telegram upload (MB) | 50 |

---

## 📱 Usage

### Basic Flow
1. **Upload Cookies** - `/cookies` - Required first step
2. **Send Instagram Link** - Just paste any Instagram URL
3. **Auto-Download** - Bot automatically downloads and sends all media

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and main menu |
| `/cookies` | Upload Instagram cookies file |
| `/recent` | View download history with resend option |
| `/help` | Help and usage information |

### Supported Link Types
- **📷 Posts** - `instagram.com/p/CODE/` - All images/videos from post
- **🎬 Reels** - `instagram.com/reel/CODE/` - Video download
- **📖 Stories** - `instagram.com/stories/USERNAME/` - Story content
- **👤 Profiles** - `instagram.com/USERNAME/` - Profile picture

### Cookie Setup
1. Login to Instagram in your browser
2. Install "Get cookies.txt LOCALLY" browser extension
3. Click **Export** (not Export As JSON)
4. Send the `.txt` file to bot via `/cookies`

---

## 🗂️ Project Structure

```
Telegram_Insta_Bot/
├── bot.py                  # Main bot application
├── config.py               # Configuration handler
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables
├── README.md              # Documentation
├── .github/
│   └── workflows/
│       └── deploy.yml     # CI/CD deployment
├── data/
│   ├── cookies/           # Per-user cookie files
│   ├── user_cookies.json  # Cookie paths
│   └── download_cache.json # Download cache
└── downloads/             # Downloaded files (auto-cleaned)
```

---

## 🔧 Troubleshooting

### "gallery-dl not found" error
```bash
# Install manually
pip install gallery-dl
# Bot auto-installs on startup if missing
```

### "Private account" errors
- The account used for cookies must follow the private account
- Re-login to Instagram and export fresh cookies

### "Story expired" errors
- Instagram stories expire after 24 hours
- Download stories soon after they're posted

### Rate limiting issues
- Instagram aggressively rate limits requests
- Wait a few minutes between downloads
- Use fresh cookies if rate limited frequently

### Cookies not working
- Ensure you clicked **Export** (not Export As JSON)
- Cookie file should be in Netscape format
- Re-login to Instagram and export fresh cookies
- Check that `sessionid` and `ds_user_id` are present

### Media group sending fails
- Telegram limits media groups to 10 items
- Bot automatically splits larger posts into batches
- Individual images sent as fallback if batch fails

---

## 🛡️ Security Notes

- Cookies stored locally per user in `data/cookies/`
- No sensitive data in logs (tokens masked)
- Whitelist system for access control
- Files auto-deleted after configured days
- Download cache cleaned with file cleanup
- **Never share your `.env` file or cookies**

---

## 📄 License

Educational project. Code can be used for learning purposes. Not intended for production deployment. Respect all applicable laws and terms of service.

---

**Built with ❤️ using Vibe Coding & DeepSeek AI**  
*For educational purposes only*