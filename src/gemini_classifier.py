"""
gemini_classifier.py — Gemini AI Email Classification Module
=============================================================

Sends email metadata (sender, subject, snippet) to Google's Gemini LLM
and receives back a structured JSON classification:

    {"important": true/false, "priority": "urgent"/"normal", "summary": "..."}

Uses the NEW `google-genai` unified SDK (not the deprecated
`google-generativeai` package).

Typical usage:
    from gemini_classifier import classify_with_retry
    result = classify_with_retry(sender, subject, snippet)
    if result["important"]:
        print(f"[{result['priority']}] {result['summary']}")
"""

# ─── Standard Library ────────────────────────────────────────────────
import json
import logging
import re
import time
from typing import Optional

# ─── Third-Party: Google GenAI (unified SDK) ─────────────────────────
from google import genai
from google.genai import types

# ─── Local Project Config ────────────────────────────────────────────
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_SYSTEM_PROMPT

# ─── Logger Setup ────────────────────────────────────────────────────
# All output in this module goes through the "gemini_classifier" logger,
# so the caller can control verbosity via standard logging configuration.
logger = logging.getLogger(__name__)

# =====================================================================
#  SAFE DEFAULT — returned when everything goes wrong so the pipeline
#  never crashes on a classification failure.
# =====================================================================
_SAFE_DEFAULT: dict = {
    "important": False,
    "priority": "normal",
    "summary": "",
}

# =====================================================================
#  Gemini Client (lazy singleton)
# =====================================================================
# We keep a module-level reference so the client is created only once
# and reused across every classify_email() call in the same process.
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    """Lazy-initialize and return the Gemini client.

    The client is created on the first call and cached in the module-level
    ``_client`` variable for all subsequent calls.  This avoids paying the
    setup cost until classification is actually needed.

    Returns:
        genai.Client: A ready-to-use Gemini API client.
    """
    global _client
    if _client is None:
        logger.info("Initializing Gemini client (model: %s)…", GEMINI_MODEL)
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# =====================================================================
#  JSON Response Parsing Helpers
# =====================================================================

def _strip_markdown_fences(text: str) -> str:
    """Remove optional markdown code fences from Gemini's response.

    Gemini sometimes wraps its JSON output like this::

        ```json
        {"important": true, ...}
        ```

    This helper strips those fences so ``json.loads()`` can parse the
    payload cleanly.

    Args:
        text: Raw response text from the model.

    Returns:
        The inner content with fences and surrounding whitespace removed.
    """
    # Pattern: optional ```json (or just ```) ... closing ```
    # re.DOTALL lets '.' match newlines inside the fenced block.
    pattern = r"```(?:json)?\s*(.*?)\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # No fences found — return the original text, just stripped.
    return text.strip()


def _parse_classification(raw_text: str) -> dict:
    """Parse Gemini's raw text response into a validated dict.

    Handles three common edge cases:
      1. Response wrapped in ```json … ``` markdown fences.
      2. Extra whitespace / newlines around the JSON.
      3. Missing keys — filled with safe defaults.

    Args:
        raw_text: The string returned by ``response.text``.

    Returns:
        A dict with guaranteed keys: ``important``, ``priority``, ``summary``.
    """
    # --- Step 1: Strip markdown fences (if present) -------------------
    cleaned = _strip_markdown_fences(raw_text)

    # --- Step 2: Attempt JSON parse -----------------------------------
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Failed to parse Gemini response as JSON: %s — raw text: %s",
            exc,
            raw_text[:200],  # log only the first 200 chars to avoid spam
        )
        # Return safe default so the pipeline continues.
        return dict(_SAFE_DEFAULT)

    # --- Step 3: Validate & fill missing fields -----------------------
    # Each field gets a default if the model omitted it.
    result = {
        "important": bool(data.get("important", False)),
        "priority": str(data.get("priority", "normal")),
        "summary": str(data.get("summary", "")),
    }

    return result


# =====================================================================
#  Core Classification Function
# =====================================================================

def classify_email(sender: str, subject: str, snippet: str) -> dict:
    """Ask Gemini to classify a single email.

    Builds a concise user prompt from the email metadata, sends it to
    Gemini with a low temperature for deterministic output, and parses
    the structured JSON response.

    Args:
        sender:  The ``From`` header (e.g. ``'John Doe <john@x.com>'``).
        subject: The email subject line.
        snippet: Gmail's auto-generated text preview (~first 100 chars).

    Returns:
        dict with keys:
            - ``important`` (bool): Whether the email needs attention.
            - ``priority`` (str):  ``"urgent"`` or ``"normal"``.
            - ``summary``  (str):  One-line human-readable summary.

    Raises:
        Exception: Propagates any API / network errors so the caller
                   (or ``classify_with_retry``) can decide how to handle them.
    """
    # ---- Build the user prompt with all email details ----------------
    prompt = (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Preview: {snippet}"
    )

    logger.debug("Classifying email — Subject: %s", subject)

    # ---- Call Gemini with low temperature for deterministic results ---
    client = _get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            # The system prompt (from config.py) tells Gemini to respond
            # with JSON: {"important": bool, "priority": str, "summary": str}
            system_instruction=GEMINI_SYSTEM_PROMPT,
            # Low temperature → more deterministic, less creative.
            temperature=0.1,
        ),
    )

    # ---- Extract and parse the response text -------------------------
    raw_text = response.text
    logger.debug("Raw Gemini response: %s", raw_text[:300])

    result = _parse_classification(raw_text)
    logger.info(
        "Classification result — important=%s, priority=%s, summary=%.60s",
        result["important"],
        result["priority"],
        result["summary"],
    )

    return result


# =====================================================================
#  Retry Wrapper with Exponential Backoff
# =====================================================================

def classify_with_retry(
    sender: str,
    subject: str,
    snippet: str,
    max_retries: int = 3,
) -> dict:
    """Classify an email, retrying on transient failures.

    Uses exponential backoff (2^attempt seconds) between retries.
    If every attempt fails, returns a **safe default** (not important)
    so the overall pipeline never crashes due to a single API hiccup.

    Args:
        sender:      The ``From`` header.
        subject:     The email subject line.
        snippet:     Gmail's text preview.
        max_retries: How many times to retry after the initial attempt.
                     Total attempts = 1 + max_retries.  Default is 3.

    Returns:
        dict with keys ``important``, ``priority``, ``summary``.
        On total failure the dict is the safe default (not important).
    """
    last_exception: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            return classify_email(sender, subject, snippet)

        except Exception as exc:
            last_exception = exc

            # --- Calculate exponential backoff delay ------------------
            # Attempt 1 → 2s, Attempt 2 → 4s, Attempt 3 → 8s, …
            delay = 2 ** attempt
            logger.warning(
                "Gemini classify attempt %d/%d failed: %s — "
                "retrying in %ds…",
                attempt,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

    # ---- All retries exhausted — return safe default -----------------
    logger.error(
        "All %d Gemini classify attempts failed for subject '%s'. "
        "Last error: %s — returning safe default (not important).",
        max_retries,
        subject,
        last_exception,
    )
    return dict(_SAFE_DEFAULT)


# =====================================================================
#  Summary-Only Helper (for rule-based pre-classified emails)
# =====================================================================

def generate_summary_only(sender: str, subject: str, snippet: str) -> str:
    """Generate ONLY a human-readable summary for an email that is
    already known to be important (e.g. matched by a rule-based filter).

    This does **not** re-classify the email; it just asks Gemini for a
    concise summary to display in the digest.

    Args:
        sender:  The ``From`` header.
        subject: The email subject line.
        snippet: Gmail's text preview.

    Returns:
        A short summary string.  If the API call fails, returns a
        sensible fallback built from the subject line.
    """
    # ---- Build a focused prompt that only asks for a summary ---------
    prompt = (
        f"Summarize this email in one short sentence.\n\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Preview: {snippet}"
    )

    try:
        client = _get_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                # Override system prompt with a simple summarization
                # instruction to avoid getting a JSON classification back.
                system_instruction=(
                    "You are a concise email summarizer. "
                    "Reply with ONLY a single short sentence summarizing "
                    "the email. No JSON, no formatting, just the summary."
                ),
                temperature=0.2,
            ),
        )

        summary = response.text.strip()
        logger.debug("Generated summary for '%s': %s", subject, summary)
        return summary

    except Exception as exc:
        # ---- Fallback: use the subject line as-is --------------------
        logger.warning(
            "Summary generation failed for '%s': %s — using subject as fallback.",
            subject,
            exc,
        )
        return f"Email from {sender} — {subject}"
