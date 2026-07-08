"""
main.py — Email Triage Agent Orchestrator
══════════════════════════════════════════════════════════════════════
This is the entry point for the Email Triage Agent.
Run it directly or schedule it to run every hour.

Usage:
    python src/main.py              # Full run (fetch → classify → notify)
    python src/main.py --dry-run    # Test run (no Slack messages sent)

What it does:
    1. Authenticates with Gmail API for each configured account
    2. Fetches unread emails from INBOX and SPAM folders
    3. Skips any emails already processed (SQLite deduplication)
    4. For each new email:
       a. Checks the rule-based pre-filter (VIP senders + keywords)
       b. If rules match → flags as important (no Gemini API call needed)
       c. If rules don't match → calls Gemini AI for classification
    5. For each important email → sends a formatted Slack notification
    6. Marks all processed emails in SQLite (prevents duplicates next run)
    7. Cleans up old database entries (>30 days)
    8. Logs a summary of the run

Pipeline flow:
    Gmail API → SQLite Dedup → Rule Filter → Gemini AI → Slack Webhook
══════════════════════════════════════════════════════════════════════
"""

# ── Standard Library ─────────────────────────────────────────
import sys
import logging
from datetime import datetime
import time


# ── Local Modules ────────────────────────────────────────────
from config import (
    GMAIL_ACCOUNTS,
    VIP_SENDERS,
    IMPORTANT_KEYWORDS,
    URGENT_KEYWORDS,
    SLACK_WEBHOOK_URL,
    GEMINI_API_KEY,
    LOG_LEVEL,
    DEDUP_RETENTION_DAYS,
)
from gmail_client import fetch_all_new_emails
from gemini_classifier import classify_with_retry, generate_summary_only
from slack_notifier import send_notification, send_run_summary
from dedupe_store import DedupeStore


# ══════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════

def setup_logging(level_name: str = "INFO") -> None:
    """Configure logging for the entire agent.

    All modules use logging.getLogger(__name__), so this single
    configuration controls output everywhere.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# Get the logger for this module
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# RULE-BASED PRE-FILTER
# ══════════════════════════════════════════════════════════════

def check_vip_sender(sender_email: str) -> bool:
    """Check if the sender's email matches any VIP sender or domain.

    Args:
        sender_email: The sender's email address (e.g., "boss@company.com")

    Returns:
        True if the sender is on the VIP list.
    """
    if not sender_email:
        return False

    sender_lower = sender_email.lower().strip()

    for vip in VIP_SENDERS:
        vip_lower = vip.lower().strip()

        if vip_lower.startswith("@"):
            # Domain match: "@company.com" matches "anyone@company.com"
            if sender_lower.endswith(vip_lower):
                return True
        else:
            # Exact email match
            if sender_lower == vip_lower:
                return True

    return False


def check_keywords(subject: str, snippet: str) -> dict:
    """Check if the email subject or snippet contains any important keywords.

    Args:
        subject: The email subject line.
        snippet: Gmail's auto-generated text preview (~100 chars).

    Returns:
        dict with:
            matched (bool): Whether any keyword was found.
            keyword (str):  The keyword that matched (or "").
            is_urgent (bool): Whether the keyword is in the URGENT list.
    """
    # Combine subject and snippet for searching
    text_to_search = f"{subject} {snippet}".lower()

    # Check urgent keywords first (they're a subset of important)
    for keyword in URGENT_KEYWORDS:
        if keyword.lower() in text_to_search:
            return {"matched": True, "keyword": keyword, "is_urgent": True}

    # Check regular important keywords
    for keyword in IMPORTANT_KEYWORDS:
        if keyword.lower() in text_to_search:
            return {"matched": True, "keyword": keyword, "is_urgent": False}

    return {"matched": False, "keyword": "", "is_urgent": False}


def apply_rule_filter(email: dict) -> dict:
    """Apply all rule-based filters to a single email.

    This is the FIRST pass — fast, free, no API calls. If the rules
    resolve the email as important, we skip the Gemini API call
    (saves money and latency).

    Args:
        email: dict with keys: sender_email, subject, snippet

    Returns:
        dict with:
            matched (bool):  True if any rule flagged this email.
            priority (str):  "urgent" or "normal"
            reason (str):    Human-readable explanation of why it matched.
    """
    # ── Check 1: Is the sender a VIP? ────────────────────────
    if check_vip_sender(email.get("sender_email", "")):
        return {
            "matched": True,
            "priority": "urgent",  # VIP senders are always treated as urgent
            "reason": f"VIP sender: {email.get('sender_email', '')}",
        }

    # ── Check 2: Do keywords match? ──────────────────────────
    keyword_result = check_keywords(
        email.get("subject", ""),
        email.get("snippet", ""),
    )
    if keyword_result["matched"]:
        return {
            "matched": True,
            "priority": "urgent" if keyword_result["is_urgent"] else "normal",
            "reason": f"Keyword match: \"{keyword_result['keyword']}\"",
        }

    # ── No rules matched — needs Gemini classification ───────
    return {"matched": False, "priority": "normal", "reason": ""}


# ══════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════

def run_triage(dry_run: bool = False) -> dict:
    """Run the full email triage pipeline once.

    This is the main function that:
    1. Fetches emails from both Gmail accounts
    2. Filters out duplicates via SQLite
    3. Applies rule-based pre-filter
    4. Calls Gemini AI for unresolved emails
    5. Sends Slack notifications for important emails

    Args:
        dry_run: If True, prints what would be sent but doesn't post to Slack.

    Returns:
        dict with run statistics.
    """
    # ── Track statistics for this run ────────────────────────
    stats = {
        "processed": 0,       # Total new emails seen
        "important": 0,       # Emails flagged as important
        "slack_sent": 0,      # Successful Slack notifications
        "gemini_calls": 0,    # Number of Gemini API calls made
        "skipped_dedup": 0,   # Emails skipped (already processed)
        "errors": 0,          # Errors encountered
    }

    run_start = datetime.now()
    logger.info("=" * 60)
    logger.info("Email Triage Agent — Starting run")
    logger.info("=" * 60)

    if dry_run:
        logger.info("🔍 DRY RUN MODE — no Slack messages will be sent")

    # ── Validate configuration ───────────────────────────────
    if not GEMINI_API_KEY:
        logger.error("❌ GEMINI_API_KEY is not set in .env — cannot classify emails")
        return stats

    if not SLACK_WEBHOOK_URL or SLACK_WEBHOOK_URL == "PASTE_YOUR_SLACK_WEBHOOK_URL_HERE":
        if not dry_run:
            logger.error("❌ SLACK_WEBHOOK_URL is not set in .env — cannot send notifications")
            logger.info("   Set up a Slack webhook (see .env.example) or use --dry-run to test")
            return stats
        else:
            logger.warning("⚠️  SLACK_WEBHOOK_URL not set, but that's OK in dry-run mode")

    # ── Open the deduplication database ──────────────────────
    with DedupeStore() as store:

        # ── Process each Gmail account ───────────────────────
        for account_index, account in enumerate(GMAIL_ACCOUNTS, start=1):
            account_label = account["label"]
            logger.info(f"\n📧 Processing account {account_index}: {account_label}")
            logger.info("-" * 50)

            # ── Fetch emails from Gmail ──────────────────────
            try:
                emails = fetch_all_new_emails(account)
                logger.info(f"   Fetched {len(emails)} emails (INBOX + SPAM)")
            except FileNotFoundError as e:
                logger.error(f"   ❌ Credentials not found for {account_label}: {e}")
                logger.error(f"      Run: python scripts/setup_oauth.py --account {account_index}")
                stats["errors"] += 1
                continue
            except Exception as e:
                logger.error(f"   ❌ Failed to fetch emails for {account_label}: {e}")
                stats["errors"] += 1
                continue

            # ── Process each email ───────────────────────────
            for email in emails:
                # ── Skip if already processed (dedup) ────────
                if store.is_processed(email["id"], account_label):
                    stats["skipped_dedup"] += 1
                    continue

                stats["processed"] += 1

                # ── Apply rule-based pre-filter ──────────────
                rule_result = apply_rule_filter(email)

                if rule_result["matched"]:
                    # ── Rules say it's important ─────────────
                    logger.info(f"   ✅ IMPORTANT (rule): {email.get('subject', '(no subject)')}")
                    logger.info(f"      Reason: {rule_result['reason']}")

                    important = True
                    priority = rule_result["priority"]

                    # Get a summary from Gemini (even though rules already flagged it,
                    # we want a nice AI-generated summary for the Slack message)
                    try:
                        summary = generate_summary_only(
                            email.get("sender", ""),
                            email.get("subject", ""),
                            email.get("snippet", ""),
                        )
                        stats["gemini_calls"] += 1
                    except Exception:
                        # Fallback summary if Gemini fails
                        summary = f"{rule_result['reason']}. {email.get('subject', '')}"

                else:
                    # ── Rules didn't match → ask Gemini ──────
                    logger.debug(f"   🤖 Calling Gemini for: {email.get('subject', '(no subject)')}")

                    gemini_result = classify_with_retry(
                        email.get("sender", ""),
                        email.get("subject", ""),
                        email.get("snippet", ""),
                    )
                    stats["gemini_calls"] += 1

                    important = gemini_result.get("important", False)
                    priority = gemini_result.get("priority", "normal")
                    summary = gemini_result.get("summary", "")

                    if important:
                        logger.info(f"   ✅ IMPORTANT (Gemini): {email.get('subject', '(no subject)')}")
                    else:
                        logger.debug(f"   ⬜ Not important: {email.get('subject', '(no subject)')}")

                # ── Send Slack notification if important ─────
                if important:
                    stats["important"] += 1

                    # Build the notification payload
                    notification_data = {
                        "sender_name": email.get("sender_name", "Unknown"),
                        "sender_email": email.get("sender_email", "unknown@email.com"),
                        "account_label": account_label,
                        "subject": email.get("subject", "(no subject)"),
                        "summary": summary,
                        "date": email.get("date", "Unknown date"),
                        "folder": email.get("folder", "INBOX"),
                        "priority": priority,
                    }

                    if dry_run:
                        # ── Dry run: print instead of sending ─
                        print(f"\n{'='*50}")
                        print(f"🔔 [DRY RUN] Would send Slack notification:")
                        print(f"   From:     {notification_data['sender_name']} <{notification_data['sender_email']}>")
                        print(f"   Account:  {notification_data['account_label']}")
                        print(f"   Subject:  {notification_data['subject']}")
                        print(f"   Summary:  {notification_data['summary']}")
                        print(f"   Folder:   {notification_data['folder']}")
                        print(f"   Priority: {'🔴 Urgent' if priority == 'urgent' else '🟡 Normal'}")
                        print(f"{'='*50}")
                    else:
                        # ── Real run: send to Slack ───────────
                        success = send_notification(notification_data)
                        if success:
                            stats["slack_sent"] += 1
                            logger.info(f"   📨 Slack notification sent!")
                        else:
                            logger.warning(f"   ⚠️  Failed to send Slack notification")
                            stats["errors"] += 1

                # ── Mark as processed (regardless of importance) ──
                store.mark_processed(email["id"], account_label, important)

                # Add a 4 to 5-second sleep to ensure you stay under 15 requests per minute
                print("Sleeping to avoid rate limits...")
                time.sleep(4.5)

        # ── Cleanup old entries ──────────────────────────────
        store.cleanup_old_entries(days=DEDUP_RETENTION_DAYS)

        # ── Get database stats ───────────────────────────────
        db_stats = store.get_stats()

    # ── Log run summary ──────────────────────────────────────
    duration = (datetime.now() - run_start).total_seconds()

    logger.info("\n" + "=" * 60)
    logger.info("📊 Run Summary")
    logger.info("=" * 60)
    logger.info(f"   Duration:         {duration:.1f}s")
    logger.info(f"   Emails processed: {stats['processed']}")
    logger.info(f"   Skipped (dedup):  {stats['skipped_dedup']}")
    logger.info(f"   Important:        {stats['important']}")
    logger.info(f"   Slack sent:       {stats['slack_sent']}")
    logger.info(f"   Gemini API calls: {stats['gemini_calls']}")
    logger.info(f"   Errors:           {stats['errors']}")
    logger.info(f"   DB total records: {db_stats.get('total', 'N/A')}")
    logger.info("=" * 60)

    # ── Send Slack run summary (only if there were important emails) ──
    if not dry_run and stats["important"] > 0:
        send_run_summary(stats)

    return stats


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Set up logging before anything else
    setup_logging(LOG_LEVEL)

    # Check for --dry-run flag
    dry_run = "--dry-run" in sys.argv

    # Run the pipeline
    try:
        stats = run_triage(dry_run=dry_run)

        # Exit with error code if there were critical errors
        if stats["errors"] > 0 and stats["processed"] == 0:
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\n⏹️  Agent stopped by user (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"💥 Unexpected error: {e}", exc_info=True)
        sys.exit(1)
