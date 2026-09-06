# 🤝 Contributing

Thanks for taking the time to contribute! 🎉

This is an **educational project** — it demonstrates Python programming, Telegram Bot API integration, and web scraping with gallery-dl. All contributions are welcome, whether it's a bug fix, a new feature, improved documentation, or better test coverage.

> ⚠️ Please remember this project is for **educational purposes only** and is not intended for production use. Do not submit changes that encourage misuse of Instagram content or violate its Terms of Service.

---

## 🧑‍💻 Setting Up the Dev Environment

### 1. Fork & Clone

```bash
git clone https://github.com/HoomanJCode/Telegram_Insta_Bot.git
cd Telegram_Insta_Bot
```

### 2. Create a Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate      # Linux / macOS
venv\Scripts\activate         # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and add your `BOT_TOKEN` (get one from [@BotFather](https://t.me/BotFather)). Other variables are optional.

### 5. Create Required Directories

```bash
mkdir -p data/cookies downloads
```

### 6. Run the Bot

```bash
python bot.py
```

---

## ✅ Testing Your Changes

The CI pipeline runs two checks on every push and pull request. Run the same checks locally before submitting:

### Syntax check (all Python files)

```bash
for f in $(find . -name "*.py" -not -path "./.git/*" -not -path "./venv/*"); do
  python -m py_compile "$f" || exit 1
done
```

### Import check (all modules load)

```bash
python -c "from config import Config; print('✅ config')"
python -c "from core.cache import FileIDCache; print('✅ cache')"
python -c "from core.cookies import load_cookie_ids, save_cookie_ids; print('✅ cookies')"
python -c "from core.downloader import check_gallery_dl; print('✅ downloader')"
python -c "from handlers.start import start_cmd; print('✅ handlers')"
python -c "from utils.helpers import check_admin, check_whitelist; print('✅ utils')"
python -c "from utils.telegram_sender import send_media_batch, resend_by_file_ids; print('✅ telegram_sender')"
```

If both pass, you're good to go.

---

## 📝 Commit Conventions

Please use [conventional commits](https://www.conventionalcommits.org/) so the changelog stays readable:

| Prefix | Purpose | Example |
|--------|---------|---------|
| `feat:` | New feature | `feat: Add video watermark removal` |
| `fix:` | Bug fix | `fix: Handle expired stories gracefully` |
| `docs:` | Documentation only | `docs: Explain cookie export steps` |
| `perf:` | Performance improvement | `perf: Parallelize media uploads` |
| `ci:` | CI/CD changes | `ci: Cache pip dependencies in release` |
| `chore:` | Housekeeping | `chore: Update dependencies` |

---

## 🌿 Branching & Pull Requests

- The default branch is `master` — open pull requests against it
- Create a feature branch for your work: `git checkout -b feat/my-awesome-feature`
- Keep pull requests small and focused on a single change
- Make sure the CI checks pass before requesting review
- Describe **what** changed and **why** in the pull request description

---

## 🐛 Reporting Issues

Found a bug or have an idea? Open an [issue](https://github.com/HoomanJCode/Telegram_Insta_Bot/issues) and include:

- A clear title and description
- Steps to reproduce the problem
- Expected vs. actual behavior
- Python version, OS, and bot logs (with tokens masked)
- Whether you're running via Docker or directly with Python

---

## 🛡️ Security

If you find a security vulnerability, **do not open a public issue**. Follow the instructions in [SECURITY.md](SECURITY.md) to report it privately.

---

**Thanks again for contributing!** ❤️