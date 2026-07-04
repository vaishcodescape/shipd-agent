#!/usr/bin/env python3
# Logging onto Shipd via email verification 

from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from browser.session import (
    create_browser_context,
    launch_lightweight_browser,
    managed_browser,
)

REVIEWS_URL = "https://shipd.ai/quests/olympus/reviews"
SIGN_IN_URL = "https://shipd.ai/quests/olympus/sign-in?redirect=%2Freviews"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AUTH_DIR = REPO_ROOT / "auth-jwt"
AUTH_STATE_PATH = AUTH_DIR / "shipd-auth.json"
MANUAL_LOGIN_TIMEOUT_MS = 300_000
OTP_POLL_INTERVAL_SEC = 1.5
OTP_TIMEOUT_SEC = 120
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_OTP_SENDER = "notifications@shipd.ai"
DEFAULT_NAV_TIMEOUT_MS = 30_000

OTP_PATTERNS = (
    re.compile(
        r"(?:verification code|one-time password|otp|security code|code is)[:\s]+(\d{6})",
        re.IGNORECASE,
    ),
    re.compile(r"(\d{6})\s+is your (?:verification|security|login) code", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
)


@dataclass(frozen=True)
class AuthConfig:
    """Credentials and IMAP settings for headless OTP login."""

    auth_email: str = ""
    auth_password: str = ""
    imap_host: str = DEFAULT_IMAP_HOST
    imap_port: int = DEFAULT_IMAP_PORT
    otp_sender: str = DEFAULT_OTP_SENDER


def load_auth_config() -> AuthConfig:
    """Read auth settings from .env into an AuthConfig (loads .env as a side effect)."""
    load_dotenv(REPO_ROOT / ".env")
    return AuthConfig(
        auth_email=os.getenv("AUTH_EMAIL", "").strip(),
        auth_password=os.getenv("AUTH_PASSWORD", "").strip(),
        imap_host=os.getenv("IMAP_HOST", DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST,
        imap_port=int(os.getenv("IMAP_PORT", str(DEFAULT_IMAP_PORT))),
        otp_sender=os.getenv("OTP_SENDER", DEFAULT_OTP_SENDER).strip() or DEFAULT_OTP_SENDER,
    )


def launch_browser(playwright, *, headless: bool = True):
    """Launch Chromium headless by default (no visible browser window)."""
    return launch_lightweight_browser(playwright, headless=headless)


def new_context(browser, *, auth_state_path: Path):
    """Create a browser context, restoring the saved session if one exists."""
    return create_browser_context(browser, auth_state_path=auth_state_path)


def goto_page(
    page: Page,
    url: str,
    *,
    timeout: int = DEFAULT_NAV_TIMEOUT_MS,
) -> None:
    """Navigate without waiting for network idle (prefer element waits after)."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)


def ensure_signed_in(
    page: Page,
    landing_url: str,
    config: AuthConfig,
    *,
    headed: bool = False,
) -> None:
    """Navigate to landing_url and complete OTP sign-in if not already authenticated."""
    goto_page(page, landing_url)
    if "sign-in" not in page.url:
        return

    sign_in_with_email_otp(
        page,
        auth_email=config.auth_email,
        auth_password=config.auth_password,
        imap_host=config.imap_host,
        imap_port=config.imap_port,
        otp_sender=config.otp_sender,
        headed=headed,
    )
    goto_page(page, landing_url)
    if "sign-in" in page.url:
        raise RuntimeError("Sign-in did not complete; still on the sign-in page.")


def is_logged_in(page: Page) -> bool:
    return "sign-in" not in page.url and "/reviews" in page.url


def submit_email(page: Page, email_address: str) -> None:
    if "sign-in" not in page.url:
        goto_page(page, SIGN_IN_URL)

    email_input = page.locator("#email")
    email_input.wait_for(state="visible", timeout=30_000)
    email_input.fill(email_address)

    continue_btn = page.get_by_role("button", name="Continue", exact=True)
    continue_btn.wait_for(state="visible", timeout=10_000)
    continue_btn.click()


def wait_for_verification_input(page: Page) -> None:
    combined = page.locator(
        'input[autocomplete="one-time-code"], '
        'input[name="code"], '
        'input[inputmode="numeric"]'
    )
    try:
        combined.first.wait_for(state="visible", timeout=30_000)
        return
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Verification code input did not appear after submitting email."
        ) from exc


def enter_verification_code(page: Page, code: str) -> None:
    wait_for_verification_input(page)

    digit_inputs = page.locator('input[inputmode="numeric"]')
    if digit_inputs.count() >= len(code):
        first = digit_inputs.first
        first.click()
        first.press_sequentially(code, delay=0)
    else:
        entered = False
        for locator in (
            page.get_by_role("textbox", name="Enter verification code"),
            page.locator('input[autocomplete="one-time-code"]'),
            page.locator('input[name="code"]'),
        ):
            if locator.count() and locator.first.is_visible():
                target = locator.first
                target.click()
                target.press_sequentially(code, delay=0)
                entered = True
                break
        if not entered:
            raise RuntimeError("Could not find verification code input on the sign-in page.")

    verify_btn = page.get_by_role("button", name="Verify", exact=True)
    if verify_btn.count() and verify_btn.first.is_visible():
        verify_btn.first.click()


def wait_for_reviews(page: Page, timeout_ms: int = 30_000) -> None:
    page.wait_for_url("**/reviews**", timeout=timeout_ms, wait_until="commit")


def _decode_header_value(value: str) -> str:
    parts: list[str] = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _message_body(msg: Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)


def _message_from_sender(msg: Message, otp_sender: str) -> bool:
    from_header = _decode_header_value(msg.get("From", ""))
    _, sender_addr = parseaddr(from_header)
    return sender_addr.lower() == otp_sender.lower()


def extract_otp_from_message(msg: Message, *, otp_sender: str) -> str | None:
    if not _message_from_sender(msg, otp_sender):
        return None

    subject = _decode_header_value(msg.get("Subject", ""))
    sender = _decode_header_value(msg.get("From", ""))
    body = _message_body(msg)
    haystack = f"{subject}\n{sender}\n{body}"

    for pattern in OTP_PATTERNS:
        match = pattern.search(haystack)
        if match:
            return match.group(1)
    return None


def _imap_since_date(since_epoch: float) -> str:
    return datetime.fromtimestamp(since_epoch, tz=timezone.utc).strftime("%d-%b-%Y")


def _message_received_epoch(msg: Message) -> float | None:
    date_header = msg.get("Date")
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _try_fetch_otp(
    *,
    mailbox_email: str,
    password: str,
    imap_host: str,
    imap_port: int,
    since_epoch: float,
    otp_sender: str,
) -> str | None:
    # Allow a few seconds of clock skew between mail server and local time.
    min_received_epoch = since_epoch - 5

    with imaplib.IMAP4_SSL(imap_host, imap_port) as client:
        client.login(mailbox_email, password)
        client.select("INBOX")

        since_date = _imap_since_date(since_epoch)
        status, data = client.search(None, f'(FROM "{otp_sender}" SINCE {since_date})')
        if status != "OK" or not data or not data[0]:
            return None

        candidates: list[tuple[float, str]] = []
        for message_id in data[0].split():
            status, fetched = client.fetch(message_id, "(RFC822)")
            if status != "OK" or not fetched:
                continue

            raw_email = fetched[0][1]
            msg = email.message_from_bytes(raw_email)
            received_at = _message_received_epoch(msg)
            if received_at is None or received_at < min_received_epoch:
                continue

            otp = extract_otp_from_message(msg, otp_sender=otp_sender)
            if otp:
                candidates.append((received_at, otp))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]


def fetch_otp_from_imap(
    *,
    mailbox_email: str,
    password: str,
    imap_host: str,
    imap_port: int,
    since_epoch: float,
    otp_sender: str,
    timeout_sec: int = OTP_TIMEOUT_SEC,
) -> str:
    deadline = time.time() + timeout_sec

    while time.time() < deadline:
        try:
            otp = _try_fetch_otp(
                mailbox_email=mailbox_email,
                password=password,
                imap_host=imap_host,
                imap_port=imap_port,
                since_epoch=since_epoch,
                otp_sender=otp_sender,
            )
            if otp:
                return otp
        except imaplib.IMAP4.error as exc:
            # Auth/connection failures won't fix themselves on retry — fail fast.
            raise RuntimeError(
                "IMAP authentication failed. For Gmail, use an app password in "
                "AUTH_PASSWORD and ensure IMAP is enabled."
            ) from exc

        time.sleep(OTP_POLL_INTERVAL_SEC)

    raise RuntimeError(
        f"Verification code not found in mailbox within {timeout_sec}s. "
        f"Confirm AUTH_EMAIL receives OTP emails from {otp_sender} and "
        "AUTH_PASSWORD is a valid IMAP app password."
    )


def sign_in_with_email_otp(
    page: Page,
    *,
    auth_email: str,
    auth_password: str,
    imap_host: str,
    imap_port: int,
    otp_sender: str,
    headed: bool = False,
) -> None:
    if not auth_email:
        if headed:
            print("Complete email sign-in in the browser window...")
            if "sign-in" not in page.url:
                goto_page(page, SIGN_IN_URL)
            wait_for_reviews(page, timeout_ms=MANUAL_LOGIN_TIMEOUT_MS)
            return
        raise RuntimeError(
            "No saved session and AUTH_EMAIL is not set. Add AUTH_EMAIL to .env "
            "for headless login, or re-run with --headed to sign in manually."
        )

    submit_email(page, auth_email)
    since_epoch = time.time()
    wait_for_verification_input(page)

    if not auth_password:
        if headed:
            print(
                "Verification code input is ready. Enter the code from your inbox "
                "in the browser window..."
            )
            wait_for_reviews(page, timeout_ms=MANUAL_LOGIN_TIMEOUT_MS)
            return
        raise RuntimeError(
            "AUTH_PASSWORD is required for headless login (IMAP app password). "
            "Re-run with --headed to enter the verification code manually."
        )

    print(
        f"Polling {imap_host} for verification code from {otp_sender} "
        f"sent to {auth_email}..."
    )
    otp = fetch_otp_from_imap(
        mailbox_email=auth_email,
        password=auth_password,
        imap_host=imap_host,
        imap_port=imap_port,
        since_epoch=since_epoch,
        otp_sender=otp_sender,
    )
    print("Verification code received from mailbox.")

    enter_verification_code(page, otp)

    timeout_ms = MANUAL_LOGIN_TIMEOUT_MS if headed else 60_000
    try:
        wait_for_reviews(page, timeout_ms=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Sign-in did not reach /reviews after entering the verification code. "
            "The code may have expired; try again."
        ) from exc


def login(
    *,
    auth_email: str = "",
    auth_password: str = "",
    imap_host: str = DEFAULT_IMAP_HOST,
    imap_port: int = DEFAULT_IMAP_PORT,
    otp_sender: str = DEFAULT_OTP_SENDER,
    headless: bool = True,
    auth_state_path: Path = AUTH_STATE_PATH,
) -> None:
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    with managed_browser(
        headless=headless,
        auth_state_path=auth_state_path,
        lightweight=headless,
    ) as session:
        page = session.page
        context = session.context

        goto_page(page, REVIEWS_URL)

        if not is_logged_in(page):
            sign_in_with_email_otp(
                page,
                auth_email=auth_email,
                auth_password=auth_password,
                imap_host=imap_host,
                imap_port=imap_port,
                otp_sender=otp_sender,
                headed=not headless,
            )
            context.storage_state(path=str(auth_state_path))
            print(f"Logged in and saved session to {auth_state_path}")
        else:
            print("Reused existing session.")

        print(f"Ready at: {page.url}")
        if not headless:
            print("Press Enter to close the browser...")
            input()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Log in to Shipd Olympus reviews via email verification code. "
            "Runs headless (no browser window) by default."
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Open a visible browser window for manual sign-in. "
            "Default is headless background mode."
        ),
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=AUTH_STATE_PATH,
        help="Path to save/load Playwright auth state.",
    )
    return parser.parse_args()


def main() -> int:
    config = load_auth_config()

    args = parse_args()
    try:
        login(
            auth_email=config.auth_email,
            auth_password=config.auth_password,
            imap_host=config.imap_host,
            imap_port=config.imap_port,
            otp_sender=config.otp_sender,
            headless=not args.headed,
            auth_state_path=args.auth_state,
        )
    except (PlaywrightTimeoutError, RuntimeError, imaplib.IMAP4.error) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
