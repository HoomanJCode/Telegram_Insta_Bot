# 🔒 Security Policy

This project is an **educational demonstration** and is **not intended for production use**. Even so, security matters — thank you for helping keep this project and its users safe.

---

## 🗂️ Supported Versions

Only the most recent release is actively supported and receives security fixes.

| Version | Supported |
|---------|-----------|
| Latest release (`latest` Docker image tag) | ✅ |
| Older releases / tags | ❌ |

If you run the bot, please use the latest version:

```bash
docker pull ghcr.io/hoomanjcode/telegram_insta_bot:latest
```

---

## 🚨 Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.** Report them privately so they can be addressed before disclosure:

1. Go to the repository's **Security** tab
2. Click **Report a vulnerability** (GitHub Private Security Advisories)
3. Include the following details:
   - A description of the vulnerability and its potential impact
   - Steps to reproduce (if possible)
   - Affected components (e.g., cookie handling, downloader, config)
   - Your contact details if you'd like a follow-up

You should receive a response within a few days. Please allow time for a fix to be prepared before publicly disclosing the issue.

---

## 🛡️ Security Notes for Self-Hosters

- **Never commit or share your `.env` file** — it contains your bot token. It is already gitignored.
- **Never share cookie files** — they contain your Instagram session credentials.
- **Use the whitelist** — set `WHITELIST_USERS` to restrict the bot to your own Telegram user IDs.
- **Tokens are masked in logs** — but be careful with logs you share when reporting issues.
- **Cookies are stored locally** per user in `data/cookies/` — protect the `data/` directory on your server.
- **Files are auto-deleted** after `STORAGE_DAYS` (default: 2) to limit data retention.
- **The bot is educational** — do not expose it to untrusted users, and respect Instagram's Terms of Service and all applicable laws.