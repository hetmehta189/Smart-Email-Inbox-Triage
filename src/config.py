"""
config.py — Central Configuration for the Email Triage Agent
═══════════════════════════════════════════════════════════════
This file is the SINGLE PLACE to edit all agent behavior:
  • VIP sender lists (always flagged as important)
  • Important keyword lists (trigger importance without AI)
  • Urgent keyword lists (escalate to 🔴 priority)
  • The Gemini AI classification prompt
  • API keys and file paths (loaded from .env)

You do NOT need to touch any other file to tune the agent.
═══════════════════════════════════════════════════════════════
"""

# ── Standard Library ─────────────────────────────────────────
import os
from pathlib import Path

# ── Third-Party ──────────────────────────────────────────────
from dotenv import load_dotenv


# ══════════════════════════════════════════════════════════════
# PATH SETUP
# ══════════════════════════════════════════════════════════════
# PROJECT_ROOT points to the top-level folder (one level above src/).
# This ensures .env and credentials/ are found regardless of where
# you run the script from.

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════
# LOAD ENVIRONMENT VARIABLES FROM .env
# ══════════════════════════════════════════════════════════════
# load_dotenv() reads key=value pairs from the .env file in the
# project root and makes them available via os.getenv().

load_dotenv(PROJECT_ROOT / ".env")


# ══════════════════════════════════════════════════════════════
# GMAIL ACCOUNT CONFIGURATION
# ══════════════════════════════════════════════════════════════
# Each account needs:
#   - label:            The email address (shown in Slack notifications)
#   - credentials_file: The OAuth client JSON from Google Cloud Console
#   - token_file:       Auto-generated token (created by setup_oauth.py)
#
# To add a 3rd account, copy one of these blocks, change the label,
# and create new credentials_3.json + token_account3.json files.

GMAIL_ACCOUNTS = [
    {
        "label": os.getenv("GMAIL_ACCOUNT_1_LABEL", "Account 1"),
        "credentials_file": str(PROJECT_ROOT / "credentials" / "credentials_1.json"),
        "token_file": str(PROJECT_ROOT / "credentials" / "token_account1.json"),
    },
    {
        "label": os.getenv("GMAIL_ACCOUNT_2_LABEL", "Account 2"),
        "credentials_file": str(PROJECT_ROOT / "credentials" / "credentials_2.json"),
        "token_file": str(PROJECT_ROOT / "credentials" / "token_account2.json"),
    },
]


# ══════════════════════════════════════════════════════════════
# API KEYS & MODEL SETTINGS
# ══════════════════════════════════════════════════════════════

# Gemini API key — get yours free at https://aistudio.google.com/
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Which Gemini model to use for classification.
# "gemini-2.0-flash" is fast and cheap — great for classification tasks.
# Change to "gemini-2.5-flash" for better reasoning (still fast).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Slack Incoming Webhook URL — see .env.example for setup instructions.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")


# ══════════════════════════════════════════════════════════════
# FILE PATHS
# ══════════════════════════════════════════════════════════════

# SQLite database for tracking processed emails (deduplication).
# Auto-created on first run — you never need to touch this file.
DB_PATH = str(PROJECT_ROOT / "data" / "processed_emails.db")


# ══════════════════════════════════════════════════════════════
# GMAIL API SETTINGS
# ══════════════════════════════════════════════════════════════

# OAuth scope — gmail.readonly means we can READ emails but never
# modify, delete, or send anything. This is the safest option.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Maximum emails to fetch per label (INBOX / SPAM) per account per run.
# 50 is a good balance — covers most hourly volumes without being slow.
# Increase if you get 100+ emails per hour.
MAX_EMAILS_PER_LABEL = 50


# ══════════════════════════════════════════════════════════════
# ✏️  VIP SENDERS — EDIT THIS LIST
# ══════════════════════════════════════════════════════════════
# Emails from these addresses or domains are ALWAYS flagged as important,
# WITHOUT calling the Gemini API (saves API calls and is instant).
#
# Use full email addresses:  "boss@company.com"
# Use domain prefixes:       "@company.com"  (matches anyone @company.com)
#
# Examples (uncomment and replace with your own):

VIP_SENDERS = [
    # ── Work contacts ─────────────────────────────────
    # "boss@company.com",
    # "hr@company.com",
    # "@company.com",           # Anyone at your company
    #
    # ── Financial ─────────────────────────────────────
    # "@yourbank.com",
    # "@paypal.com",
    #
    # ── Education ─────────────────────────────────────
    # "@university.edu",
    #
    # ── Government / Official ─────────────────────────
    # "@gov.in",
    # "@incometax.gov.in",
]


# ══════════════════════════════════════════════════════════════
# ✏️  IMPORTANT KEYWORDS — EDIT THIS LIST
# ══════════════════════════════════════════════════════════════
# If ANY of these words appear in the email subject OR snippet (preview),
# the email is flagged important WITHOUT calling Gemini.
# Matching is case-insensitive ("OTP" matches "otp", "Otp", etc.).

IMPORTANT_KEYWORDS = [
    # ── Finance & Payments ────────────────────────────
    "invoice",
    "payment",
    "transaction",
    "refund",
    "bank statement",
    "tax",
    #
    # ── Security & Authentication ─────────────────────
    "OTP",
    "verification code",
    "password reset",
    "security alert",
    "two-factor",
    "2FA",
    "login attempt",
    #
    # ── Work & Career ─────────────────────────────────
    "interview",
    "offer letter",
    "contract",
    "deadline",
    "meeting invitation",
    "action required",
    #
    # ── Urgency ───────────────────────────────────────
    "urgent",
    "immediate",
    "asap",
    "time-sensitive",
    #
    # ── Legal & Medical ───────────────────────────────
    "legal",
    "court",
    "medical",
    "appointment",
    "prescription",
]


# ══════════════════════════════════════════════════════════════
# ✏️  URGENT KEYWORDS — EDIT THIS LIST
# ══════════════════════════════════════════════════════════════
# Subset of important keywords that escalate priority to 🔴 Urgent
# (instead of 🟡 Normal). These imply action needed within 24 hours.

URGENT_KEYWORDS = [
    "urgent",
    "OTP",
    "verification code",
    "password reset",
    "security alert",
    "action required",
    "deadline",
    "expiring",
    "expires today",
    "immediate",
    "asap",
    "time-sensitive",
    "login attempt",
]


# ══════════════════════════════════════════════════════════════
# 🤖 GEMINI CLASSIFICATION PROMPT — EDIT THIS TO TUNE THE AI
# ══════════════════════════════════════════════════════════════
# This is the system instruction sent to Gemini for every email
# that wasn't resolved by the VIP/keyword filters above.
#
# The model receives this prompt PLUS the email's From, Subject,
# and Preview text, and must respond with a JSON object.
#
# Tips for tuning:
#   - Be specific about what counts as "important" for YOUR workflow
#   - Add examples of emails you've missed or been wrongly notified about
#   - Keep it under ~500 words for best results

GEMINI_SYSTEM_PROMPT = """You are an email importance classifier for a busy professional.

Analyze the email metadata below and respond with a JSON object.

CLASSIFICATION RULES:
- "important" = true if the email:
  • Requires action or a reply from the recipient
  • Contains time-sensitive information (deadlines, meetings, appointments)
  • Is from a real person writing directly (not automated marketing)
  • Relates to finances, work, health, legal matters, or education
  • Could have negative consequences if missed or delayed
  • Is a legitimate email incorrectly flagged as spam

- "important" = false if the email is:
  • Marketing, promotions, or sales emails
  • Newsletter subscriptions or digest emails
  • Social media notifications (likes, follows, comments, friend requests)
  • Automated system notifications that need no human action
  • Spam or phishing attempts
  • Generic "welcome" or "thanks for signing up" emails

PRIORITY RULES:
- "urgent" — requires action within 24 hours (OTPs, expiring offers, same-day meetings, security alerts)
- "normal" — important but not time-critical (can wait a day or two)

SUMMARY RULES:
- Write exactly 1-2 sentences
- Focus on WHAT ACTION is needed (if any), not just what the email says
- Be specific — include dates, amounts, names, deadlines when present
- If no action is needed, describe what the email is about

Respond with ONLY this JSON structure — no markdown fences, no explanation, no extra text:
{"important": true, "priority": "urgent", "summary": "Brief action-oriented summary here."}
"""


# ══════════════════════════════════════════════════════════════
# LOGGING SETTINGS
# ══════════════════════════════════════════════════════════════

# Log level: "DEBUG" for verbose output, "INFO" for normal, "WARNING" for quiet
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# How many days of processed email records to keep in SQLite before cleanup
DEDUP_RETENTION_DAYS = 30

# How many hours back to look on the very first run (when SQLite is empty)
FIRST_RUN_LOOKBACK_HOURS = 24
