# 📸 Telegram Instagram Downloader Bot

> **Download Instagram reels, posts, stories, and profile pictures directly to Telegram.** This self-hosted Python bot takes any Instagram link you send and automatically downloads the media with gallery-dl, then delivers it to your chat — no manual saving, no external apps.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/HoomanJCode/Telegram_Insta_Bot/ci.yml?branch=master&label=CI)](https://github.com/HoomanJCode/Telegram_Insta_Bot/actions)
[![Docker](https://img.shields.io/badge/Docker-GHCR-blue.svg)](https://github.com/HoomanJCode/Telegram_Insta_Bot/pkgs/container/telegram_insta_bot)

<details>
<summary><b>⚠️ Disclaimer — educational project</b></summary>

This project is created for **educational purposes only**. It demonstrates Python programming concepts, Telegram Bot API integration, and web scraping techniques.

- This bot is **NOT intended for production use** or actual content downloading
- Downloading Instagram content may violate Instagram's Terms of Service
- Respect content creators' rights and intellectual property
- Users are solely responsible for complying with applicable laws and regulations
- The developers assume **NO liability** for any misuse of this software
- This project was built as a coding exercise using **Vibe Coding** methodology with DeepSeek AI assistance

</details>

---

## 📚 Table of Contents

- [Features](#-features)
- [Supported Content](#-supported-content)
- [Docker Deployment](#-docker-deployment-recommended)
- [Prerequisites](#-prerequisites)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Project Structure](#-project-structure)
- [Troubleshooting](#-troubleshooting)
- [CI/CD Pipeline](#-cicd-pipeline)
- [Security Notes](#-security-notes)
- [License](#-license)

---

## ✨ Features

- 🖼️ **Post Download** — all images from Instagram carousel posts, sent as media groups
- 🎬 **Reel Download** — Instagram Reels downloaded as MP4 video
- 📖 **Story Download** — Instagram stories (images and videos, before 24h expiry)
- 👤 **Profile Pictures** — download any public profile picture
- ⚡ **Auto-Download** — just send a link, the bot handles everything
- 📦 **Batch Image Upload** — multiple images sent as Telegram media groups (up to 10 per batch)
- 💾 **Smart Caching** — prevents re-downloading the same content
- 🔒 **Concurrent Protection** — download locks prevent duplicate downloads
- 🗑️ **Auto-Cleanup** — files deleted after configurable days (default: 2)
- 🍪 **Cookie Management** — per-user cookie storage with validation for private content
- 👥 **Whitelist System** — restrict the bot to specific Telegram users
- 📱 **Download History** — view and resend previously downloaded content
- 🔄 **Resend Support** — resend cached media without re-downloading

---

## 📥 Supported Content

Paste any of these Instagram URLs into the chat:

| Type | URL format | What you get |
|------|------------|--------------|
| 📷 **Posts** | `instagram.com/p/CODE/` | All images & videos from the post |
| 🎬 **Reels** | `instagram.com/reel/CODE/` | MP4 video download |
| 📖 **Stories** | `instagram.com/stories/USERNAME/` | Story images and videos |
| 👤 **Profiles** | `instagram.com/USERNAME/` | Profile picture download |

---

## 🐳 Docker Deployment (Recommended)

Using Docker is the easiest way to run the bot on a server — no need to install Python or dependencies manually.

### Quick Start (build locally)

```bash
# 1. Clone the repo
git clone https://github.com/HoomanJCode/Telegram_Insta_Bot.git
cd Telegram_Insta_Bot

# 2. Create your .env file
cp .env.example .env
nano .env   # Edit with your BOT_TOKEN etc.

# 3. Build and run
docker compose up -d
```

### Quick Start (use pre-built image)

```bash
# 1. Pull the latest image
docker pull ghcr.io/hoomanjcode/telegram_insta_bot:latest

# 2. Create your .env file
mkdir -p instagram-bot && cd instagram-bot
cat > .env << EOF
BOT_TOKEN=your_bot_token_here
WHITELIST_USERS=123456789,987654321
STORAGE_DAYS=2
MAX_TELEGRAM_FILE_SIZE=50
EOF

# 3. Run
docker compose up -d
```

### Useful Commands

```bash
docker compose up -d        # Start in background
docker compose down         # Stop the bot
docker compose logs -f      # Watch live logs
docker compose restart      # Restart the bot
docker compose pull         # Pull latest pre-built image
docker compose build        # Rebuild from source (local build)
```

### What You Need on Your Server

- [Docker](https://docs.docker.com/engine/install/) installed
- [Docker Compose](https://docs.docker.com/compose/install/) (usually included with Docker)

---

## 📋 Prerequisites

### System Requirements

- **Python 3.9+**
- Linux (recommended) / macOS / Windows
- gallery-dl (auto-installed if missing)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### No FFmpeg Required

Unlike YouTube downloader bots, **FFmpeg is not required** for Instagram downloads. gallery-dl handles all media types natively.

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

Create a `.env` file:

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
| `BOT_TOKEN` | Telegram Bot API token (from @BotFather) | **Required** |
| `WHITELIST_USERS` | Comma-separated Telegram user IDs allowed to use the bot | Empty (all allowed) |
| `ADMIN_USERS` | Comma-separated admin user IDs | Empty |
| `STORAGE_DAYS` | Days before downloaded files auto-delete | `2` |
| `MAX_TELEGRAM_FILE_SIZE` | Max size for Telegram upload (MB) | `50` |

---

## 📱 Usage

### Basic Flow

1. **Upload Cookies** — send `/cookies`, then upload your Instagram cookies file (required first step)
2. **Send Instagram Link** — just paste any Instagram URL
3. **Auto-Download** — the bot automatically downloads and sends all media

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and main menu |
| `/cookies` | Upload Instagram cookies file |
| `/recent` | View download history with resend option |
| `/help` | Help and usage information |

### Cookie Setup

1. Login to Instagram in your browser
2. Install the "Get cookies.txt LOCALLY" browser extension
3. Click **Export** (not Export As JSON)
4. Send the `.txt` file to the bot via `/cookies`

---

## 🗂️ Project Structure

```
Telegram_Insta_Bot/
├── bot.py                  # Main bot application
├── config.py               # Configuration handler
├── requirements.txt        # Python dependencies
├── Dockerfile              # Docker image build
├── docker-compose.yml      # Docker Compose config
├── .env                    # Environment variables
├── README.md               # Documentation
├── .github/
│   └── workflows/
│       ├── ci.yml          # Tests (on push/PR)
│       └── release.yml     # Build image + Release + Deploy (on tag)
├── core/
│   ├── downloader.py       # gallery-dl integration
│   ├── cache.py            # Download cache
│   └── cookies.py          # Cookie management
├── handlers/
│   ├── start.py            # /start command
│   ├── messages.py         # Message handling
│   ├── inline.py           # Inline mode
│   ├── callbacks.py        # Callback queries
│   └── cookies_handler.py  # Cookie upload flow
├── utils/
│   ├── helpers.py          # Utility functions
│   └── telegram_sender.py  # Message sending
├── data/                   # Runtime data (gitignored)
└── downloads/              # Downloaded files (gitignored)
```

---

## 🔧 Troubleshooting

### "gallery-dl not found" error

```bash
# Install manually
pip install gallery-dl
# The bot also auto-installs it on startup if missing
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
- The bot automatically splits larger posts into batches
- Individual images are sent as a fallback if the batch fails

---

## 🔄 CI/CD Pipeline

### Tests (on every push/PR)

- Python syntax validation
- Import checks for all modules

### Release (on tag push `v*`)

1. **Build Docker image** → pushed to [GitHub Container Registry](https://github.com/HoomanJCode/Telegram_Insta_Bot/pkgs/container/telegram_insta_bot) (public)
2. **GitHub Release** → created with changelog and pull commands
3. **Deploy to VPS** → auto-deploys via Docker (if secrets configured)

### How to release

```bash
git tag v0.1.0
git push origin v0.1.0
```

The pipeline will build, release, and deploy automatically.

### VPS Secrets (optional)

**Step 1: Generate SSH key on your VPS**

```bash
ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/github_deploy_key -N ""
cat ~/.ssh/github_deploy_key.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**Step 2: Add secrets in GitHub**

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `VPS_HOST` | Your server IP |
| `VPS_SSH_PRIVATE_KEY` | Output of `cat ~/.ssh/github_deploy_key` (the **private** key) |
| `BOT_TOKEN` | Telegram bot token |
| `WHITELIST_USERS` | Comma-separated user IDs (optional) |
| `ADMIN_USERS` | Comma-separated admin IDs (optional) |

> ⚠️ The `VPS_SSH_PRIVATE_KEY` must be the **private** key, not the public key. Include the full `-----BEGIN...` and `-----END...` lines.

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

This project is licensed under the [MIT License](LICENSE) — free to use, modify, and distribute with attribution. It was built for learning purposes; respect all applicable laws and terms of service.

---

**Built with ❤️ using Vibe Coding & DeepSeek AI** · *Educational project*