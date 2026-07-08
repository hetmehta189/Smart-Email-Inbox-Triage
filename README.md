# 📬 AI Email Triage Agent (Gmail → Gemini AI → Slack)

A Python automation that monitors **2 Gmail accounts** every hour (both Inbox and Spam), uses **Google Gemini AI** to classify emails by importance, and sends formatted **Slack notifications** for anything that matters, so you never miss an important email again.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![License](https://img.shields.io/badge/License-MIT-green)
![Schedule](https://img.shields.io/badge/Schedule-Every%201%20Hour-orange)

---

## ✨ Features

- 📧 **Dual Gmail Monitoring**: Scans 2 Gmail accounts (Inbox AND Spam folders)
- 🧠 **Smart Classification**: Rule-based pre-filter + Gemini AI for uncertain emails
- 💬 **Slack Notifications**: Rich, formatted notifications via Incoming Webhook
- 🔁 **Deduplication**: SQLite database ensures no duplicate notifications ever
- ⏰ **Hourly Schedule**: Run via cron, Task Scheduler, or Antigravity
- 🛡️ **Spam Rescue**: Catches legitimate emails wrongly flagged as spam
- ✏️ **Easily Customizable**: VIP senders, keywords, and AI prompt all in one file
- 🔒 **Read-Only**: Only reads emails, never modifies or sends anything

---

## 🏗️ Architecture

```
┌──────────────────┐      ┌──────────────────┐
│  Gmail Account 1 │      │  Gmail Account 2 │
│  (OAuth2 Token)  │      │  (OAuth2 Token)  │
└────────┬─────────┘      └─────────┬────────┘
         │  INBOX + SPAM            │  INBOX + SPAM
         └───────────┬──────────────┘
                     │
              ┌──────▼───────┐
              │ gmail_client │  Fetch unread emails
              └──────┬───────┘
                     │
              ┌──────▼───────┐
              │ dedupe_store │  SQLite: skip duplicates
              └──────┬───────┘
                     │
              ┌──────▼───────────────┐
              │  Rule-Based Filter   │  VIP senders + keywords
              └──────┬───────────────┘
                     │
           ┌─────────┴───────────┐
           │ Important           │ Uncertain
           │ (skip Gemini)       │
           │              ┌──────▼───────┐
           │              │ Gemini AI    │  classify + summarize
           │              └──────┬───────┘
           │                     │
           └─────────┬───────────┘
                     │ Important emails only
              ┌──────▼───────┐
              │ Slack        │  Formatted notification
              │ Webhook      │  via Block Kit + mrkdwn
              └──────────────┘
```

### How Classification Works

| Step | Method | Cost | When Used |
|------|--------|------|-----------|
| 1. VIP Sender Check | Rule-based | Free | Always (first check) |
| 2. Keyword Match | Rule-based | Free | If VIP didn't match |
| 3. Gemini AI | API call | ~Free tier | Only if rules didn't resolve |

This layered approach keeps Gemini API usage minimal — most routine emails are handled by the free rule-based filter.

---

## 📁 Project Structure

```
Email Extract Ai Automations/
├── src/
│   ├── main.py                 # Entry point — orchestrates the pipeline
│   ├── config.py               # ✏️ VIP lists, keywords, Gemini prompt
│   ├── gmail_client.py         # Gmail API auth + fetch (2 accounts)
│   ├── gemini_classifier.py    # Gemini AI classification + summarization
│   ├── slack_notifier.py       # Slack webhook notifications
│   └── dedupe_store.py         # SQLite deduplication store
├── scripts/
│   └── setup_oauth.py          # One-time OAuth token generator
├── credentials/                # OAuth tokens (gitignored)
├── data/                       # SQLite database (gitignored, auto-created)
├── .env                        # Your secrets (gitignored)
├── .env.example                # Template with setup instructions
├── .gitignore
├── requirements.txt
└── README.md                   # You are here
```

---

## 🚀 Setup Guide

### Prerequisites

- **Python 3.11+** installed
- **A Google Cloud account** (free)
- **2 Gmail accounts** you want to monitor
- **A Slack workspace** where you can create apps

---

### Step 1: Clone & Install Dependencies

```bash
# Navigate to the project folder
cd "Email Extract Ai Automations"

# Create a virtual environment (recommended)
python -m venv .venv

# Activate the virtual environment
.venv\Scripts\activate        # Windows (PowerShell)
# source .venv/bin/activate   # Mac/Linux

# Install all dependencies
pip install -r requirements.txt
```

---

### Step 2: Set Up Gmail API Credentials

You need OAuth credentials so the agent can read your emails.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. **Create a new project** → name it "Email Triage Agent"
3. Go to **APIs & Services** → **Library** → search **Gmail API** → click **Enable**
4. Go to **APIs & Services** → **OAuth consent screen**:
   - User Type: **External** → click **Create**
   - App name: "Email Triage Agent"
   - Add **both** Gmail addresses as **Test Users**
   - Click **Save and Continue** through the remaining steps
5. Go to **APIs & Services** → **Credentials**:
   - Click **Create Credentials** → **OAuth 2.0 Client ID**
   - Application type: **Desktop App**
   - Name: "Account 1"
   - Click **Create** → **Download JSON**
   - Save the downloaded file as `credentials/credentials_1.json`
6. **Repeat step 5** for your second account:
   - Create another OAuth 2.0 Client ID → name it "Account 2"
   - Download and save as `credentials/credentials_2.json`

> **Note:** Both accounts can use the same Google Cloud project — you just need separate OAuth client IDs.

---

### Step 3: Generate OAuth Tokens

Run the setup script once per account. It opens your browser for Google sign-in.

```bash
# Create the credentials folder if it doesn't exist
mkdir credentials

# Authorize Gmail Account 1 (opens browser)
python scripts/setup_oauth.py --account 1

# Authorize Gmail Account 2 (opens browser)
python scripts/setup_oauth.py --account 2
```

After each run, you'll see a confirmation message and a `token_accountN.json` file in the `credentials/` folder.

---

### Step 4: Get a Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/)
2. Click **"Get API Key"** → **Create API Key**
3. Copy the API key

---

### Step 5: Set Up Slack Incoming Webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From Scratch**
2. Name it **"Email Triage Agent"** → select your workspace → **Create App**
3. In the left sidebar: click **Incoming Webhooks** → toggle it **ON**
4. Scroll down → click **Add New Webhook to Workspace**
5. Choose the channel where you want notifications → click **Allow**
6. Copy the **Webhook URL** (starts with `https://hooks.slack.com/services/...`)

---

### Step 6: Configure Environment Variables

```bash
# Copy the template
cp .env.example .env
```

Edit `.env` with your values:

```env
GMAIL_ACCOUNT_1_LABEL=your_first_email@gmail.com
GMAIL_ACCOUNT_2_LABEL=your_second_email@gmail.com
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.0-flash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

---

### Step 7: Customize Your Filters (Optional)

Open `src/config.py` and edit these sections:

**VIP Senders** — emails from these are ALWAYS flagged important:
```python
VIP_SENDERS = [
    "boss@company.com",
    "@yourbank.com",       # Matches anyone @yourbank.com
    "@university.edu",
]
```

**Important Keywords** — trigger importance if found in subject/body:
```python
IMPORTANT_KEYWORDS = [
    "invoice", "payment", "urgent", "deadline",
    "interview", "OTP", "security alert",
    # Add your own keywords here...
]
```

**Urgent Keywords** — subset that escalates to 🔴 Urgent priority:
```python
URGENT_KEYWORDS = [
    "urgent", "OTP", "password reset", "deadline",
    # Add your own urgent triggers here...
]
```

---

## 🧪 Testing

### Dry Run (Recommended First Test)

This fetches real emails and classifies them, but **doesn't send Slack messages** — it just prints what would be sent:

```bash
cd "Email Extract Ai Automations"
python src/main.py --dry-run
```

You should see output like:
```
2026-07-08 18:30:00 | INFO    | Processing account 1: yourmail@gmail.com
2026-07-08 18:30:01 | INFO    | Fetched 15 emails (INBOX + SPAM)
2026-07-08 18:30:02 | INFO    | ✅ IMPORTANT (rule): Invoice #1234 from Vendor
==================================================
🔔 [DRY RUN] Would send Slack notification:
   From:     Vendor <billing@vendor.com>
   Subject:  Invoice #1234 from Vendor
   Priority: 🟡 Normal
==================================================
```

### Full Run

Once the dry run looks good, run the real thing:

```bash
python src/main.py
```

Check your Slack channel — you should see formatted notifications! 🎉

### Deduplication Test

Run it again immediately:

```bash
python src/main.py
```

You should see `Skipped (dedup): X` in the summary — no duplicate messages.

---

## ⏰ Scheduling (Run Every Hour)

### Windows — Task Scheduler

1. Open **Task Scheduler** (search for it in the Start menu)
2. Click **Create Basic Task**
3. Name: "Email Triage Agent" → **Next**
4. Trigger: **Daily** → **Next**
5. Set start time → check **Repeat task every: 1 hour** → for a duration of **Indefinitely**
6. Action: **Start a program** → **Next**
7. Program/script: Browse to your Python executable, e.g.:
   ```
   C:\Users\HEMAL\Desktop\Email Extract Ai Automations\.venv\Scripts\python.exe
   ```
8. Add arguments:
   ```
   src\main.py
   ```
9. Start in:
   ```
   C:\Users\HEMAL\Desktop\Email Extract Ai Automations
   ```
10. Click **Finish**

### Mac / Linux — cron

Open your crontab:
```bash
crontab -e
```

Add this line (runs every hour at minute 0):
```cron
0 * * * * cd /path/to/Email-Extract-Ai-Automations && /path/to/.venv/bin/python src/main.py >> logs/cron.log 2>&1
```

### Antigravity — Built-in Scheduler

In the Antigravity chat, use the `/schedule` command to set up hourly execution.

---

## ⚙️ Customization

### Changing the Gemini AI Prompt

The AI prompt is at the bottom of `src/config.py` in the `GEMINI_SYSTEM_PROMPT` variable. You can tune it to match your workflow. For example:

- Add specific examples of emails you've missed
- Change what counts as "urgent" for your needs
- Add industry-specific rules

### Switching Gemini Models

In `.env`, change `GEMINI_MODEL`:
```env
GEMINI_MODEL=gemini-2.5-flash    # Better reasoning, still fast
GEMINI_MODEL=gemini-1.5-pro      # More powerful, slightly slower
```

### Slack Message Format

The message format is in `src/slack_notifier.py` in the `format_slack_message()` function. Edit the `message_lines` string to change the layout.

---

## 🔒 Security

- ✅ **Read-only Gmail access** — `gmail.readonly` scope only
- ✅ **No secrets in code** — all credentials in `.env` (gitignored)
- ✅ **Separate tokens** — each Gmail account has its own token file
- ✅ **Auto token refresh** — OAuth tokens refresh automatically
- ✅ **Webhook URL is private** — stored in `.env`, never committed

---

## 🆓 Cost

| Service | Free Tier |
|---------|-----------|
| Gmail API | Unlimited for personal use |
| Gemini AI (2.0 Flash) | 1,500+ requests/day |
| Slack Incoming Webhooks | Unlimited |
| SQLite | Free (local file) |

The rule-based pre-filter minimizes Gemini API calls, keeping you well within free tier limits.

---

## ❓ Troubleshooting

| Issue | Solution |
|-------|----------|
| `FileNotFoundError: credentials_1.json` | Download OAuth client JSON from Google Cloud Console |
| `Token expired` | Delete `token_accountN.json` and re-run `setup_oauth.py` |
| `GEMINI_API_KEY not set` | Add your Gemini API key to `.env` |
| `SLACK_WEBHOOK_URL not set` | Create a Slack webhook and add the URL to `.env` |
| `No messages found in SPAM` | This is normal — the agent checks SPAM but it may be empty |
| `Rate limited (429)` | Wait a few minutes; the agent has built-in retry logic |
| Duplicate notifications | Run `python src/main.py` — dedup should prevent this. If it persists, check that `data/processed_emails.db` exists |

---

## 📄 License

MIT License — feel free to use, modify, and share!
