"""
slack_notifier.py — Slack Notification Module for Email Triage Agent
====================================================================

Sends formatted messages to a Slack channel using an Incoming Webhook URL.
Messages use Slack's Block Kit with mrkdwn formatting (NOT standard Markdown).

Slack mrkdwn quick reference:
    *bold*          — surrounds text with single asterisks
    _italic_        — surrounds text with underscores
    ~strikethrough~ — surrounds text with tildes
    `code`          — surrounds text with backticks

Key design decisions:
    - Every payload includes a top-level `text` field as a fallback so that
      mobile / desktop push notifications still show a readable preview.
    - Content-Type is always set to application/json (Slack requires this).
    - HTTP requests use a 10-second timeout to avoid hanging indefinitely.
    - Failed POSTs are retried up to 3 times with a 2-second pause between
      attempts, which handles transient network hiccups gracefully.
"""

# ──────────────────────────────────────────────────────────────────────
# Imports
# ──────────────────────────────────────────────────────────────────────

# Standard library
import logging
import time

# Third-party
import requests  # Used for the HTTP POST to the Slack webhook

# Local / project config
from config import SLACK_WEBHOOK_URL  # The Incoming Webhook URL from Slack

# ──────────────────────────────────────────────────────────────────────
# Logger setup — all log messages from this module are prefixed with
# "slack_notifier" so they're easy to filter in combined log output.
# ──────────────────────────────────────────────────────────────────────
logger = logging.getLogger("slack_notifier")

# ──────────────────────────────────────────────────────────────────────
# Constants — edit these if you want to tweak retry / timeout behaviour
# ──────────────────────────────────────────────────────────────────────

# How many seconds to wait before giving up on a single HTTP request
REQUEST_TIMEOUT_SECONDS: int = 10

# How many times to retry a failed POST before giving up entirely
MAX_RETRY_ATTEMPTS: int = 3

# How many seconds to wait between retry attempts
RETRY_DELAY_SECONDS: int = 2

# ──────────────────────────────────────────────────────────────────────
# Helper look-ups for friendly display values
# ──────────────────────────────────────────────────────────────────────

# Maps raw IMAP folder names to user-friendly display strings.
# "SPAM" gets a warning emoji so it visually jumps out in the Slack message.
FOLDER_DISPLAY_MAP: dict[str, str] = {
    "INBOX": "Inbox",
    "SPAM": "Spam ⚠️",
}

# Maps internal priority tags to coloured-emoji labels.
PRIORITY_DISPLAY_MAP: dict[str, str] = {
    "urgent": "🔴 Urgent",
    "normal": "🟡 Normal",
}


# ======================================================================
# Public API
# ======================================================================


def format_slack_message(email_data: dict) -> dict:
    """Build the Slack Block Kit payload for one important email.

    Args:
        email_data: A dictionary with the following keys:
            - sender_name   (str): Display name of the sender, e.g. "John Doe"
            - sender_email  (str): Sender's email address, e.g. "john@example.com"
            - account_label (str): Which monitored account received this email
            - subject       (str): The email subject line
            - summary       (str): AI-generated 1-2 sentence summary
            - date          (str): Human-readable received date, e.g. "July 8, 2026, 6:30 PM"
            - folder        (str): Raw IMAP folder name, e.g. "INBOX" or "SPAM"
            - priority      (str): Priority tag, either "urgent" or "normal"

    Returns:
        A dict payload ready to POST to the Slack webhook.  The payload
        uses a single section block with mrkdwn text plus a divider at
        the bottom to visually separate multiple notifications.
    """

    # ── Look up friendly display values ──────────────────────────────
    # If the folder or priority isn't in our map, fall back to the raw
    # value so we never crash on unexpected data.
    folder_display: str = FOLDER_DISPLAY_MAP.get(
        email_data.get("folder", ""), email_data.get("folder", "Unknown")
    )
    priority_display: str = PRIORITY_DISPLAY_MAP.get(
        email_data.get("priority", ""), email_data.get("priority", "Unknown")
    )

    # ── Build the mrkdwn-formatted message body ──────────────────────
    # Each line is a separate field.  We use *bold* labels so they
    # stand out visually in the Slack channel.
    message_lines: str = (
        f"🔔 *New Important Email*\n"
        f"*From:* {email_data.get('sender_name', 'Unknown')} <{email_data.get('sender_email', 'unknown')}>\n"
        f"*Account:* {email_data.get('account_label', 'Unknown')}\n"
        f"*Subject:* {email_data.get('subject', '(no subject)')}\n"
        f"*Summary:* {email_data.get('summary', 'No summary available')}\n"
        f"*Received:* {email_data.get('date', 'Unknown date')}\n"
        f"*Folder:* {folder_display}\n"
        f"*Priority tag:* {priority_display}"
    )

    # ── Assemble the Block Kit payload ───────────────────────────────
    # Structure:
    #   1. A "section" block containing the formatted message text.
    #   2. A "divider" block to visually separate this notification
    #      from the next one in the channel.
    #
    # The top-level "text" field is a REQUIRED fallback — Slack uses it
    # for push notifications on mobile and desktop when Block Kit
    # rendering isn't available.
    payload: dict = {
        # Fallback text for mobile / desktop push notifications
        "text": f"🔔 New Important Email from {email_data.get('sender_name', 'Unknown')}: {email_data.get('subject', '(no subject)')}",
        # Block Kit blocks for rich formatting in the channel
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message_lines,
                },
            },
            {
                "type": "divider",
            },
        ],
    }

    logger.debug(f"Formatted Slack payload for email: {email_data.get('subject', '(no subject)')}")
    return payload


def send_notification(email_data: dict) -> bool:
    """Send a formatted Slack notification for one important email.

    Builds the Block Kit payload via format_slack_message(), then POSTs
    it to the configured Slack webhook URL.

    Args:
        email_data: Same dict format accepted by format_slack_message().

    Returns:
        True  — the message was delivered successfully.
        False — all retry attempts failed.

    Retry behaviour:
        - Up to 3 attempts total (configurable via MAX_RETRY_ATTEMPTS).
        - 2-second pause between each attempt (configurable via RETRY_DELAY_SECONDS).
        - Logs a warning on each failed attempt and an error if all fail.
    """

    # Build the payload once — no need to rebuild on retries
    payload: dict = format_slack_message(email_data)

    # Attempt to send, retrying on failure
    return _post_to_slack(payload, description=f"notification for '{email_data.get('subject', '(no subject)')}'")


def send_test_message() -> bool:
    """Send a simple test message to verify the webhook works.

    Useful during initial setup — run this once after configuring
    SLACK_WEBHOOK_URL in config.py to make sure messages arrive
    in the correct channel.

    Returns:
        True  — test message delivered successfully.
        False — delivery failed after all retry attempts.
    """

    # A minimal Block Kit payload with a friendly test message
    payload: dict = {
        # Fallback text for push notifications
        "text": "✅ Email Triage Agent — Slack integration test successful!",
        # Rich Block Kit content
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "✅ *Email Triage Agent — Test Message*\n\n"
                        "If you can see this, the Slack webhook is "
                        "configured correctly and notifications will "
                        "be delivered to this channel."
                    ),
                },
            },
            {
                "type": "divider",
            },
        ],
    }

    logger.info("Sending test message to Slack...")
    return _post_to_slack(payload, description="test message")


def send_run_summary(stats: dict) -> bool:
    """Send a summary message at the end of each triage run.

    This gives a quick at-a-glance view of what the agent did during
    the latest run.  To avoid spamming the channel with "nothing found"
    messages, this function only sends if there were important emails.

    Args:
        stats: A dictionary with the following keys:
            - processed   (int): Total emails scanned in this run
            - important   (int): How many were classified as important
            - slack_sent  (int): How many Slack notifications were sent
            - gemini_calls(int): Number of Gemini API calls made

    Returns:
        True  — summary sent (or skipped because nothing important).
        False — delivery failed after all retry attempts.
    """

    # ── Guard: skip if no important emails were found ────────────────
    # We don't want to flood the channel with "0 important" messages
    # every time the agent runs and finds nothing noteworthy.
    important_count: int = stats.get("important", 0)
    if important_count == 0:
        logger.info("No important emails this run — skipping Slack summary.")
        return True  # Not a failure; we intentionally chose not to send

    # ── Build the summary text ───────────────────────────────────────
    summary_text: str = (
        f"📊 *Email Triage Run Summary*\n\n"
        f"*Emails processed:* {stats.get('processed', 0)}\n"
        f"*Important emails:* {important_count}\n"
        f"*Slack notifications sent:* {stats.get('slack_sent', 0)}\n"
        f"*Gemini API calls:* {stats.get('gemini_calls', 0)}"
    )

    payload: dict = {
        # Fallback text for push notifications
        "text": f"📊 Email Triage Summary — {important_count} important email(s) found",
        # Rich Block Kit content
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": summary_text,
                },
            },
            {
                "type": "divider",
            },
        ],
    }

    logger.info(f"Sending run summary to Slack ({important_count} important emails)...")
    return _post_to_slack(payload, description="run summary")


# ======================================================================
# Internal helpers (not part of the public API)
# ======================================================================


def _post_to_slack(payload: dict, description: str = "message") -> bool:
    """POST a JSON payload to the Slack webhook with retry logic.

    This is the single place where all HTTP communication with Slack
    happens, making it easy to adjust timeout / retry behaviour in one
    spot.

    Args:
        payload:     The complete JSON payload to send.
        description: A human-readable label for log messages, e.g.
                     "notification for 'Meeting reminder'" or "test message".

    Returns:
        True  — Slack responded with HTTP 200 and body "ok".
        False — All retry attempts failed.
    """

    # ── Validate that we have a webhook URL ──────────────────────────
    if not SLACK_WEBHOOK_URL:
        logger.error("SLACK_WEBHOOK_URL is not set in config.py — cannot send Slack messages.")
        return False

    # ── Headers — Slack requires application/json ────────────────────
    headers: dict = {
        "Content-Type": "application/json",
    }

    # ── Retry loop ───────────────────────────────────────────────────
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            logger.debug(f"Slack POST attempt {attempt}/{MAX_RETRY_ATTEMPTS} for {description}...")

            response = requests.post(
                SLACK_WEBHOOK_URL,
                json=payload,        # requests handles JSON serialisation
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            # ── Check for success ────────────────────────────────────
            # Slack webhooks return HTTP 200 with body "ok" on success.
            # Any other status code means something went wrong.
            if response.status_code == 200 and response.text == "ok":
                logger.info(f"Slack {description} sent successfully (attempt {attempt}).")
                return True
            else:
                # Non-200 or unexpected body — log and retry
                logger.warning(
                    f"Slack {description} failed (attempt {attempt}/{MAX_RETRY_ATTEMPTS}): "
                    f"HTTP {response.status_code} — {response.text}"
                )

        except requests.exceptions.Timeout:
            # The request took longer than REQUEST_TIMEOUT_SECONDS
            logger.warning(
                f"Slack {description} timed out (attempt {attempt}/{MAX_RETRY_ATTEMPTS})."
            )

        except requests.exceptions.ConnectionError:
            # Network-level failure (DNS, refused connection, etc.)
            logger.warning(
                f"Slack {description} connection error (attempt {attempt}/{MAX_RETRY_ATTEMPTS})."
            )

        except requests.exceptions.RequestException as exc:
            # Catch-all for any other requests-related exception
            logger.warning(
                f"Slack {description} request error (attempt {attempt}/{MAX_RETRY_ATTEMPTS}): {exc}"
            )

        # ── Wait before retrying (but not after the last attempt) ────
        if attempt < MAX_RETRY_ATTEMPTS:
            logger.debug(f"Waiting {RETRY_DELAY_SECONDS}s before retry...")
            time.sleep(RETRY_DELAY_SECONDS)

    # ── All attempts exhausted ───────────────────────────────────────
    logger.error(f"Failed to send Slack {description} after {MAX_RETRY_ATTEMPTS} attempts.")
    return False
