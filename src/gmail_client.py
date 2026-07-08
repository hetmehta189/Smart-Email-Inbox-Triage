"""
gmail_client.py — Gmail API Integration for the Email Triage Agent
===================================================================

This module talks to the Gmail API on behalf of **two separate Gmail accounts**.
For each account it:

  1. Authenticates via OAuth2 (token refresh or browser-based consent).
  2. Pulls unread message IDs from INBOX and SPAM.
  3. Fetches full message details (sender, subject, snippet, date, labels).
  4. Returns a flat list of structured dicts ready for the AI classifier.

Key design decisions
--------------------
* **Read-only scope** — we never modify or send mail.
* **includeSpamTrash=True** — Gmail hides SPAM results unless you ask.
* **Pagination** — we follow `nextPageToken` so we never silently miss mail.
* **Separate token files** — each account gets its own `token_*.json` so
  refresh tokens don't collide.

Dependencies (in requirements.txt):
  google-api-python-client, google-auth-oauthlib, google-auth-httplib2
"""

# ── Standard library ─────────────────────────────────────────────────
import logging
import os
import re
from pathlib import Path

# ── Third-party: Google API / Auth ───────────────────────────────────
from google.auth.transport.requests import Request          # token refresh
from google.oauth2.credentials import Credentials           # load/save tokens
from google_auth_oauthlib.flow import InstalledAppFlow      # first-time consent
from googleapiclient.discovery import build                 # build service obj
from googleapiclient.errors import HttpError                # API error wrapper

# ── Local project config ─────────────────────────────────────────────
# GMAIL_SCOPES  : list[str]  — e.g. ['https://www.googleapis.com/auth/gmail.readonly']
# MAX_EMAILS_PER_LABEL : int — max messages to pull per label (INBOX / SPAM)
from config import GMAIL_SCOPES, MAX_EMAILS_PER_LABEL

# ── Module logger ────────────────────────────────────────────────────
# All output goes through the logging module — no print() calls.
logger = logging.getLogger(__name__)


# =====================================================================
#  1. AUTHENTICATION
# =====================================================================

def authenticate(credentials_file: str, token_file: str) -> object:
    """Authenticate with the Gmail API using OAuth2 and return a service object.

    Flow:
      • If *token_file* exists and the stored credentials are still valid,
        load them directly — no network call required.
      • If the token is expired **but** a refresh_token is present, silently
        refresh it in the background.
      • If neither applies (first run, or refresh token revoked), open the
        user's default browser so they can grant consent via Google's OAuth
        screen (``InstalledAppFlow``).
      • In every case the (possibly updated) token is saved back to
        *token_file* for next time.

    Args:
        credentials_file: Path to the ``credentials.json`` downloaded from
            the Google Cloud Console (OAuth 2.0 Client ID).
        token_file: Path where the access/refresh token will be cached.

    Returns:
        A ``googleapiclient.discovery.Resource`` object bound to the
        Gmail v1 API (i.e. the "service" object you call ``.users()`` on).

    Raises:
        FileNotFoundError: If *credentials_file* does not exist.
        Exception: Re-raises any unexpected Google auth errors after logging.
    """
    creds = None  # will hold our Credentials object

    # ── Step 1: Try loading an existing token ────────────────────────
    if os.path.exists(token_file):
        logger.info("Loading cached token from '%s'", token_file)
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    # ── Step 2: Refresh or re-authorize ──────────────────────────────
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token expired but we can silently refresh it
            logger.info("Token expired — refreshing via refresh_token …")
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh failed (e.g. token revoked) — fall through to
                # the full browser-based consent flow below.
                logger.warning(
                    "Token refresh failed — will re-authorize via browser."
                )
                creds = None

        if not creds:
            # Either no token existed, or refresh failed.
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"Credentials file not found: {credentials_file}. "
                    "Download it from the Google Cloud Console → "
                    "APIs & Services → Credentials → OAuth 2.0 Client IDs."
                )

            logger.info(
                "Starting browser-based OAuth flow using '%s' …",
                credentials_file,
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, GMAIL_SCOPES
            )
            # This opens the default browser and waits for the user to
            # grant permission.  Port 0 means "pick a free port".
            creds = flow.run_local_server(port=0)

        # ── Step 3: Persist the token for future runs ────────────────
        # Make sure the parent directory exists (e.g. tokens/)
        Path(token_file).parent.mkdir(parents=True, exist_ok=True)
        with open(token_file, "w", encoding="utf-8") as token_fh:
            token_fh.write(creds.to_json())
        logger.info("Token saved to '%s'", token_file)

    # ── Step 4: Build and return the Gmail service object ────────────
    service = build("gmail", "v1", credentials=creds)
    logger.info("Gmail service built successfully.")
    return service


# =====================================================================
#  2. FETCH MESSAGE IDs FROM A LABEL
# =====================================================================

def fetch_messages_from_label(
    service: object,
    label_id: str,
    max_results: int = 50,
) -> list[dict]:
    """Fetch message IDs from a given Gmail label (e.g. INBOX or SPAM).

    Gmail's ``messages().list()`` returns pages of ``{'id': ..., 'threadId': ...}``
    dicts.  We follow ``nextPageToken`` until we've collected up to
    *max_results* messages or run out of pages.

    **CRITICAL**: For the SPAM label, Gmail requires ``includeSpamTrash=True``
    in the API call — without it the response is always empty, even when
    there IS spam.  This function detects the SPAM (and TRASH) labels and
    sets the flag automatically.

    Args:
        service: Gmail API service object (from ``authenticate``).
        label_id: The Gmail label to query.  Built-in labels use
            uppercase strings: ``'INBOX'``, ``'SPAM'``, ``'TRASH'``, etc.
        max_results: Maximum number of message IDs to collect.
            Defaults to 50.

    Returns:
        A list of dicts, each containing at minimum an ``'id'`` key:
        ``[{'id': '18a...f3'}, {'id': '18a...b7'}, ...]``
        Returns an empty list if no messages are found or an error occurs.
    """
    messages: list[dict] = []
    page_token: str | None = None

    # Gmail hides SPAM and TRASH results unless you explicitly opt in.
    # This is the single most common gotcha when querying the SPAM folder.
    include_spam_trash = label_id in ("SPAM", "TRASH")

    logger.info(
        "Fetching messages from label='%s' (includeSpamTrash=%s, max=%d)",
        label_id,
        include_spam_trash,
        max_results,
    )

    try:
        while True:
            # ── Build the API request ────────────────────────────────
            # maxResults caps each *page* (max 500 per page).
            # We set it to the remaining count so we don't over-fetch.
            remaining = max_results - len(messages)
            page_size = min(remaining, 500)  # Gmail caps at 500

            request_kwargs: dict = {
                "userId": "me",
                "labelIds": [label_id],
                "maxResults": page_size,
                "includeSpamTrash": include_spam_trash,
            }

            # Only add pageToken if we're continuing pagination
            if page_token:
                request_kwargs["pageToken"] = page_token

            response = (
                service.users()
                .messages()
                .list(**request_kwargs)
                .execute()
            )

            # ── Collect message stubs ────────────────────────────────
            batch = response.get("messages", [])
            messages.extend(batch)

            logger.debug(
                "  Page returned %d messages (total so far: %d)",
                len(batch),
                len(messages),
            )

            # ── Pagination: stop if we hit the cap or no more pages ──
            page_token = response.get("nextPageToken")
            if not page_token or len(messages) >= max_results:
                break

    except HttpError as err:
        logger.error(
            "Gmail API error while listing messages from '%s': %s",
            label_id,
            err,
        )
        return []

    logger.info(
        "Fetched %d message IDs from label='%s'", len(messages), label_id
    )
    return messages


# =====================================================================
#  3. GET FULL MESSAGE DETAILS
# =====================================================================

def get_message_details(service: object, message_id: str) -> dict:
    """Fetch and parse the full details of a single Gmail message.

    Calls ``messages().get(format='full')`` which returns the entire
    message payload including headers, body parts, snippet, and labels.

    The Gmail headers are stored as a list of ``{name, value}`` dicts —
    this function extracts the three we care about (From, Subject, Date)
    and normalises them into top-level keys.

    Args:
        service: Gmail API service object.
        message_id: The Gmail message ID string (e.g. ``'18ab1c...'``).

    Returns:
        A dict with the following keys::

            {
                "id":           str,   # Gmail message ID
                "sender":       str,   # raw From header, e.g. "Jane <jane@x.com>"
                "sender_name":  str,   # parsed display name, e.g. "Jane"
                "sender_email": str,   # parsed email, e.g. "jane@x.com"
                "subject":      str,   # email subject line
                "snippet":      str,   # ~100-char plain-text preview from Gmail
                "date":         str,   # Date header value
                "label_ids":    list,  # e.g. ['INBOX', 'UNREAD', 'CATEGORY_PERSONAL']
            }

        Returns an empty dict ``{}`` if the API call fails.
    """
    try:
        # format='full' gives us headers + body parts.
        # format='metadata' would be lighter but omits the body.
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as err:
        logger.error(
            "Gmail API error fetching message '%s': %s", message_id, err
        )
        return {}

    # ── Extract headers we care about ────────────────────────────────
    # Headers arrive as:  [{"name": "From", "value": "..."}, ...]
    headers = msg.get("payload", {}).get("headers", [])

    # Build a quick lookup: header_name (lowercase) → value
    header_map: dict[str, str] = {
        h["name"].lower(): h["value"] for h in headers
    }

    raw_from = header_map.get("from", "")
    subject = header_map.get("subject", "(no subject)")
    date = header_map.get("date", "")

    # ── Parse the sender into name + email ───────────────────────────
    sender_name, sender_email = parse_sender(raw_from)

    # ── Snippet comes from the top-level response, NOT from headers ──
    snippet = msg.get("snippet", "")

    # ── Label IDs tell us which folder(s) this message lives in ──────
    label_ids = msg.get("labelIds", [])

    return {
        "id": message_id,
        "sender": raw_from,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "snippet": snippet,
        "date": date,
        "label_ids": label_ids,
    }


# =====================================================================
#  4. PARSE SENDER HEADER
# =====================================================================

def parse_sender(from_header: str) -> tuple[str, str]:
    """Parse a raw ``From`` header into a (display_name, email) tuple.

    Gmail ``From`` headers come in several flavours:

    +-----------------------------------------+--------------------+--------------------+
    | Raw header                              | name               | email              |
    +=========================================+====================+====================+
    | ``John Doe <john@example.com>``         | ``John Doe``       | ``john@example.com``|
    | ``<john@example.com>``                  | ``""``             | ``john@example.com``|
    | ``john@example.com``                    | ``""``             | ``john@example.com``|
    | ``"Doe, John" <john@example.com>``      | ``Doe, John``      | ``john@example.com``|
    +-----------------------------------------+--------------------+--------------------+

    Args:
        from_header: The raw value of the ``From`` header.

    Returns:
        A ``(name, email)`` tuple.  Either part may be an empty string
        if parsing fails.
    """
    if not from_header:
        return ("", "")

    # ── Pattern: "Display Name <email@domain>"  ──────────────────────
    # The display name may optionally be wrapped in double quotes.
    match = re.match(
        r'^"?(.+?)"?\s*<([^>]+)>',  # group 1 = name, group 2 = email
        from_header.strip(),
    )

    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip()
        return (name, email)

    # ── Fallback: bare email address (no angle brackets) ─────────────
    # e.g. "user@example.com" with nothing else.
    bare_email = from_header.strip().strip("<>")
    return ("", bare_email)


# =====================================================================
#  5. MAIN ORCHESTRATOR — FETCH ALL NEW EMAILS FOR ONE ACCOUNT
# =====================================================================

def fetch_all_new_emails(account_config: dict) -> list[dict]:
    """Fetch all unread emails from INBOX + SPAM for a single Gmail account.

    This is the **main entry point** that the rest of the application calls.
    It chains together authentication → listing → detail fetching.

    Args:
        account_config: A dict describing one Gmail account::

            {
                "label":            "personal@gmail.com",  # human-readable name
                "credentials_file": "credentials/creds_personal.json",
                "token_file":       "tokens/token_personal.json",
            }

    Returns:
        A list of enriched email dicts.  Each dict has all the fields from
        ``get_message_details`` **plus**:

        * ``"folder"``        — ``"INBOX"`` or ``"SPAM"``
        * ``"account_label"`` — the label from the config (so the caller
          knows which mailbox this came from)

        Returns an empty list if authentication fails or no messages are found.
    """
    label = account_config["label"]
    credentials_file = account_config["credentials_file"]
    token_file = account_config["token_file"]

    logger.info("━" * 60)
    logger.info("Processing account: %s", label)
    logger.info("━" * 60)

    # ── 1. Authenticate ──────────────────────────────────────────────
    try:
        service = authenticate(credentials_file, token_file)
    except FileNotFoundError as err:
        logger.error("Authentication failed for '%s': %s", label, err)
        return []
    except Exception as err:
        logger.error(
            "Unexpected auth error for '%s': %s", label, err, exc_info=True
        )
        return []

    # ── 2. Fetch message IDs from both INBOX and SPAM ────────────────
    # We process each folder separately so we can tag emails with the
    # correct "folder" value downstream.
    folders_to_check: list[tuple[str, str]] = [
        ("INBOX", "INBOX"),
        ("SPAM", "SPAM"),
    ]

    all_emails: list[dict] = []

    for folder_name, label_id in folders_to_check:
        logger.info("── Checking %s for account '%s' ──", folder_name, label)

        # Fetch the list of message ID stubs
        message_stubs = fetch_messages_from_label(
            service, label_id, max_results=MAX_EMAILS_PER_LABEL
        )

        if not message_stubs:
            logger.info("  No messages found in %s.", folder_name)
            continue

        logger.info(
            "  Found %d messages in %s — fetching details …",
            len(message_stubs),
            folder_name,
        )

        # ── 3. Fetch full details for each message ───────────────────
        for stub in message_stubs:
            msg_id = stub["id"]
            details = get_message_details(service, msg_id)

            if not details:
                # get_message_details already logged the error
                continue

            # ── 4. Enrich with folder + account metadata ─────────────
            details["folder"] = folder_name
            details["account_label"] = label

            all_emails.append(details)

    logger.info(
        "Account '%s': collected %d total emails (INBOX + SPAM).",
        label,
        len(all_emails),
    )
    return all_emails
