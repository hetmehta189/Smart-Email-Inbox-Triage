"""
dedupe_store.py — SQLite-based deduplication for the email triage agent.

PURPOSE:
    Every time the agent processes an email and fires a Slack notification,
    we record the Gmail message ID + account label here.  On the next hourly
    run we check this store *before* sending anything, so duplicates are
    impossible even if the Gmail API returns the same message twice.

SCHEMA:
    processed_emails (
        message_id    TEXT     — Gmail's immutable message ID
        account_label TEXT     — human label like "work" or "personal"
        processed_at  TIMESTAMP — UTC timestamp, auto-filled
        was_important BOOLEAN  — 1 if the AI classified it as important
        PRIMARY KEY (message_id, account_label)
    )

USAGE:
    from dedupe_store import DedupeStore

    with DedupeStore() as store:
        if not store.is_processed(msg_id, "work"):
            send_slack(msg)
            store.mark_processed(msg_id, "work", was_important=True)
"""

# ──────────────────────────────────────────────
# Standard-library imports
# ──────────────────────────────────────────────
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────
# Project imports
# ──────────────────────────────────────────────
from config import DB_PATH  # e.g. "data/processed_emails.db"

# ──────────────────────────────────────────────
# Module-level logger — inherits the root logger's
# handler/format so it plays nicely with main.py
# ──────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# SQL constants (kept here so they're easy to find)
# ──────────────────────────────────────────────

# Table creation — IF NOT EXISTS makes it safe to call on every startup.
_SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id    TEXT NOT NULL,
    account_label TEXT NOT NULL,
    processed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    was_important BOOLEAN DEFAULT 0,
    PRIMARY KEY (message_id, account_label)
);
"""

# Check whether a specific (message_id, account_label) pair exists.
_SQL_IS_PROCESSED = """
SELECT 1
  FROM processed_emails
 WHERE message_id = ?
   AND account_label = ?
 LIMIT 1;
"""

# INSERT OR IGNORE silently skips if the row already exists
# (same composite PK).  This handles any unlikely race condition
# if two processes run at the exact same time.
_SQL_MARK_PROCESSED = """
INSERT OR IGNORE INTO processed_emails
    (message_id, account_label, processed_at, was_important)
VALUES
    (?, ?, ?, ?);
"""

# Delete rows older than a given cutoff date to keep the DB small.
_SQL_CLEANUP = """
DELETE FROM processed_emails
 WHERE processed_at < ?;
"""

# Aggregate stats for the optional end-of-run log line.
_SQL_STATS_TOTAL = "SELECT COUNT(*) FROM processed_emails;"
_SQL_STATS_IMPORTANT = "SELECT COUNT(*) FROM processed_emails WHERE was_important = 1;"
_SQL_STATS_LAST_RUN = "SELECT MAX(processed_at) FROM processed_emails;"


class DedupeStore:
    """SQLite-based deduplication store for processed emails.

    Designed to be used as a context manager so the database connection
    is always cleanly committed and closed, even if an exception occurs.

    Example
    -------
    >>> with DedupeStore() as store:
    ...     if not store.is_processed(msg_id, account):
    ...         # process the email …
    ...         store.mark_processed(msg_id, account, was_important=True)
    """

    # ------------------------------------------------------------------ #
    #  Lifecycle: init / enter / exit / close
    # ------------------------------------------------------------------ #

    def __init__(self, db_path: Optional[str] = None) -> None:
        """Open (or create) the SQLite database.

        Parameters
        ----------
        db_path : str, optional
            Full path to the ``.db`` file.  Falls back to ``DB_PATH``
            from ``config.py`` when not supplied — this is the normal
            production path; the parameter exists mainly for unit tests.

        Side effects
        ------------
        * Creates the parent ``data/`` directory if it doesn't exist.
        * Creates the ``processed_emails`` table on first run.
        """
        # Resolve the database file path ---------------------------------
        # Priority: explicit argument  →  config constant
        self.db_path: str = db_path or DB_PATH
        logger.info("DedupeStore using database: %s", self.db_path)

        # Make sure the parent directory exists (e.g. data/)
        # parents=True  → create intermediate dirs
        # exist_ok=True → don't error if already there
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # Open the SQLite connection -------------------------------------
        # check_same_thread=False lets the connection be shared across
        # threads if we ever move to async; for our hourly cron this is
        # a single-threaded script, but it's a safe default.
        self.conn: sqlite3.Connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
        )

        # Return rows as sqlite3.Row so we can access columns by name
        self.conn.row_factory = sqlite3.Row

        # Create the table if this is a fresh database
        self._init_db()

    def __enter__(self) -> "DedupeStore":
        """Context-manager entry — just returns *self*."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-manager exit — commit pending changes, then close.

        We commit even when an exception occurred so that any emails
        already marked as processed are not lost (partial progress is
        better than re-sending every notification on the next run).
        """
        self.close()
        # Returning None (falsy) lets any exception propagate normally.

    def close(self) -> None:
        """Commit outstanding writes and close the database connection.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self.conn is not None:
            try:
                self.conn.commit()
                logger.debug("Database changes committed.")
            except sqlite3.Error as exc:
                logger.warning("Error committing database: %s", exc)
            finally:
                self.conn.close()
                self.conn = None  # prevent double-close
                logger.debug("Database connection closed.")

    # ------------------------------------------------------------------ #
    #  Private helpers
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        """Create the ``processed_emails`` table if it doesn't exist.

        Called once during ``__init__``.  The ``IF NOT EXISTS`` clause
        makes this safe to run on every startup.
        """
        try:
            self.conn.execute(_SQL_CREATE_TABLE)
            self.conn.commit()
            logger.debug("Database table 'processed_emails' is ready.")
        except sqlite3.Error as exc:
            logger.error("Failed to initialise database table: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def is_processed(self, message_id: str, account_label: str) -> bool:
        """Check whether we have already handled this email.

        Parameters
        ----------
        message_id : str
            Gmail's unique, immutable message ID (e.g. ``"18f3a…"``).
        account_label : str
            Human-readable label for the Gmail account (e.g. ``"work"``).

        Returns
        -------
        bool
            ``True`` if a row with this composite key already exists,
            meaning we have sent the Slack notification previously.
        """
        try:
            cursor = self.conn.execute(
                _SQL_IS_PROCESSED,
                (message_id, account_label),
            )
            found = cursor.fetchone() is not None

            if found:
                logger.debug(
                    "Message %s [%s] already processed — skipping.",
                    message_id,
                    account_label,
                )
            return found

        except sqlite3.Error as exc:
            # If the DB read fails we return False (not processed) so the
            # caller will attempt to send the notification.  A duplicate
            # Slack message is better than a silently dropped one.
            logger.error(
                "DB error checking message %s [%s]: %s — treating as NOT processed.",
                message_id,
                account_label,
                exc,
            )
            return False

    def mark_processed(
        self,
        message_id: str,
        account_label: str,
        was_important: bool = False,
    ) -> None:
        """Record that we have successfully processed this email.

        Parameters
        ----------
        message_id : str
            Gmail's unique message ID.
        account_label : str
            Label for the Gmail account.
        was_important : bool, optional
            ``True`` if the AI classifier flagged this email as
            important/urgent.  Defaults to ``False``.

        Notes
        -----
        Uses ``INSERT OR IGNORE`` so calling this twice with the same
        key is harmless — the second call is silently ignored.
        """
        # Generate a UTC timestamp string for the processed_at column
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        try:
            self.conn.execute(
                _SQL_MARK_PROCESSED,
                (message_id, account_label, now_utc, int(was_important)),
            )
            # Commit immediately so the record survives a crash later in
            # the same run — we *never* want to re-send this notification.
            self.conn.commit()

            logger.info(
                "Marked message %s [%s] as processed (important=%s).",
                message_id,
                account_label,
                was_important,
            )
        except sqlite3.Error as exc:
            logger.error(
                "Failed to mark message %s [%s] as processed: %s",
                message_id,
                account_label,
                exc,
            )
            raise

    def cleanup_old_entries(self, days: int = 30) -> None:
        """Delete records older than *days* days to keep the DB small.

        Parameters
        ----------
        days : int, optional
            Retention window in days.  Defaults to 30.

        This is safe to call on every run.  Gmail message IDs are
        immutable, so once a message is old enough to fall outside our
        polling window it will never reappear in a query anyway.
        """
        # Calculate the cutoff timestamp in UTC
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        logger.info("Cleaning up entries older than %d days (before %s).", days, cutoff)

        try:
            cursor = self.conn.execute(_SQL_CLEANUP, (cutoff,))
            removed = cursor.rowcount  # number of rows deleted
            self.conn.commit()

            if removed > 0:
                logger.info("Removed %d old entries from the dedupe store.", removed)
            else:
                logger.debug("No old entries to clean up.")

        except sqlite3.Error as exc:
            logger.error("Cleanup failed: %s", exc)
            raise

    def get_stats(self) -> dict:
        """Return summary counts — handy for end-of-run log lines.

        Returns
        -------
        dict
            ``{"total": int, "important": int, "last_run": str | None}``

            * **total** — number of rows in the table.
            * **important** — how many were flagged as important.
            * **last_run** — ISO-ish timestamp of the most recent
              ``processed_at`` value, or ``None`` if the table is empty.
        """
        try:
            total = self.conn.execute(_SQL_STATS_TOTAL).fetchone()[0]
            important = self.conn.execute(_SQL_STATS_IMPORTANT).fetchone()[0]
            last_run = self.conn.execute(_SQL_STATS_LAST_RUN).fetchone()[0]

            stats = {
                "total": total,
                "important": important,
                "last_run": last_run,  # str or None
            }
            logger.debug("DedupeStore stats: %s", stats)
            return stats

        except sqlite3.Error as exc:
            logger.error("Failed to retrieve stats: %s", exc)
            # Return safe defaults so the caller doesn't crash
            return {"total": 0, "important": 0, "last_run": None}
