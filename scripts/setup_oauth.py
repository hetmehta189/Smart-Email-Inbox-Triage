"""
setup_oauth.py — One-Time OAuth Token Generator for Gmail Accounts
══════════════════════════════════════════════════════════════════════
Run this script ONCE per Gmail account to generate the OAuth token files
that the triage agent uses to access your email (read-only).

Usage:
    python scripts/setup_oauth.py --account 1    # For Gmail account 1
    python scripts/setup_oauth.py --account 2    # For Gmail account 2

What happens:
    1. Reads the OAuth client credentials from credentials/credentials_N.json
    2. Opens your web browser to Google's sign-in page
    3. You sign in and authorize read-only Gmail access
    4. The script saves the auth token to credentials/token_accountN.json
    5. Done! The triage agent will use this token automatically.

Prerequisites:
    - credentials/credentials_1.json (or _2.json) must exist
    - Download these from Google Cloud Console → APIs & Services → Credentials
    - See README.md for detailed setup instructions
══════════════════════════════════════════════════════════════════════
"""

# ── Standard Library ─────────────────────────────────────────
import argparse
import os
import sys
from pathlib import Path

# ── Third-Party ──────────────────────────────────────────────
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# ── Constants ────────────────────────────────────────────────
# Read-only scope — the agent can read your emails but NEVER
# modify, delete, or send anything on your behalf.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Project root is one level up from scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_DIR = PROJECT_ROOT / "credentials"


def setup_oauth(account_number: int) -> None:
    """
    Run the OAuth2 flow for one Gmail account.

    This opens your browser, lets you sign in with Google,
    and saves the resulting authentication token to a file.

    Args:
        account_number: Which account to set up (1 or 2).
    """

    # ── Determine file paths ─────────────────────────────────
    credentials_file = CREDENTIALS_DIR / f"credentials_{account_number}.json"
    token_file = CREDENTIALS_DIR / f"token_account{account_number}.json"

    # ── Check that the credentials file exists ───────────────
    if not credentials_file.exists():
        print(f"\n❌ ERROR: Credentials file not found!")
        print(f"   Expected location: {credentials_file}")
        print(f"\n   To fix this:")
        print(f"   1. Go to https://console.cloud.google.com/")
        print(f"   2. APIs & Services → Credentials")
        print(f"   3. Create OAuth 2.0 Client ID (Desktop App)")
        print(f"   4. Download the JSON file")
        print(f"   5. Save it as: {credentials_file}")
        sys.exit(1)

    # ── Check if a valid token already exists ─────────────────
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
            if creds and creds.valid:
                print(f"\n✅ Account {account_number} is already authorized!")
                print(f"   Token file: {token_file}")
                print(f"   If you want to re-authorize, delete the token file and run again.")
                return
            elif creds and creds.expired and creds.refresh_token:
                print(f"\n🔄 Token for account {account_number} is expired. Refreshing...")
                creds.refresh(Request())
                with open(token_file, "w") as f:
                    f.write(creds.to_json())
                print(f"✅ Token refreshed successfully!")
                print(f"   Token file: {token_file}")
                return
        except Exception:
            # Token file is corrupted or invalid — proceed with fresh auth
            print(f"\n⚠️  Existing token for account {account_number} is invalid. Starting fresh...")

    # ── Create credentials directory if needed ────────────────
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Run the OAuth2 flow ──────────────────────────────────
    print(f"\n🔐 Setting up Gmail access for Account {account_number}")
    print(f"   Credentials: {credentials_file}")
    print(f"\n   A browser window will open. Please:")
    print(f"   1. Sign in with the Gmail account for Account {account_number}")
    print(f"   2. Click 'Allow' to grant read-only email access")
    print(f"   3. Come back to this terminal when done\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_file),
            GMAIL_SCOPES,
        )
        # port=0 tells the server to pick any available port
        creds = flow.run_local_server(port=0)
    except Exception as e:
        print(f"\n❌ OAuth flow failed: {e}")
        print(f"\n   Common causes:")
        print(f"   - The credentials JSON file is invalid or corrupted")
        print(f"   - Your Google Cloud project doesn't have the Gmail API enabled")
        print(f"   - This Gmail address isn't listed as a Test User in the OAuth consent screen")
        sys.exit(1)

    # ── Save the token ───────────────────────────────────────
    with open(token_file, "w") as f:
        f.write(creds.to_json())

    print(f"\n✅ Success! Account {account_number} is now authorized.")
    print(f"   Token saved to: {token_file}")
    print(f"\n   Next steps:")
    if account_number == 1:
        print(f"   → Run this script again with --account 2 for your second Gmail account")
    else:
        print(f"   → Both accounts are set up! Run 'python src/main.py --dry-run' to test")


def main():
    """Parse command-line arguments and run OAuth setup."""

    parser = argparse.ArgumentParser(
        description="Set up Gmail OAuth2 tokens for the Email Triage Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/setup_oauth.py --account 1    # Set up first Gmail account
    python scripts/setup_oauth.py --account 2    # Set up second Gmail account
        """,
    )
    parser.add_argument(
        "--account",
        type=int,
        choices=[1, 2],
        required=True,
        help="Which Gmail account to authorize (1 or 2)",
    )

    args = parser.parse_args()
    setup_oauth(args.account)


if __name__ == "__main__":
    main()
