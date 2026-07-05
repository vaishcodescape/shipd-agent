# Load a saved agent review JSON and submit it on Shipd via Playwright.

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# workflow/review.py shadows the review package when this script is run as
# `python workflow/submit_from_json.py` (sys.path[0] is workflow/).
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from auth import (
    AUTH_STATE_PATH,
    AuthConfig,
    ensure_signed_in,
    goto_page,
    load_auth_config,
    managed_browser,
)
from review.review_bundles import (
    PENDING_SUBMIT_PATH,
    load_review_bundle,
)
from review.result import is_review_complete, review_failure_reason
from workflow.submit import submit_review


def run_submit_from_json(
    review_json: Path,
    *,
    quest: str | None = None,
    review_url: str | None = None,
    config: AuthConfig | None = None,
    headless: bool = True,
    auth_state_path: Path = AUTH_STATE_PATH,
    finalize: bool = True,
) -> None:
    """Open the Shipd review page and fill the submit form from saved JSON."""
    review, bundle_url, bundle_quest, _repo_path = load_review_bundle(review_json)
    target_url = (review_url or bundle_url).strip()
    target_quest = (quest or bundle_quest or "olympus").strip().lower()

    if not target_url:
        raise ValueError("review_url is required (in JSON or via --review-url).")

    if not is_review_complete(review):
        raise ValueError(
            "Refusing to submit an incomplete review: "
            f"{review_failure_reason(review)}"
        )

    config = config or load_auth_config()
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    with managed_browser(
        headless=headless,
        auth_state_path=auth_state_path,
        lightweight=headless,
    ) as session:
        page = session.page
        context = session.context

        ensure_signed_in(page, target_url, config, headed=not headless)
        if target_url not in page.url:
            goto_page(page, target_url)

        confirmed = submit_review(
            page,
            review,
            quest=target_quest,
            finalize=finalize,
            review_url=target_url,
        )

        if finalize:
            if confirmed:
                print("Review submitted on Shipd.")
            else:
                raise RuntimeError(
                    "Submit clicked but confirmation not observed — verify on Shipd."
                )
        else:
            print("Form filled (--no-finalize); not clicking final Submit.")

        context.storage_state(path=str(auth_state_path))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Load an agent review JSON bundle and submit it on Shipd "
            "using Playwright form automation."
        ),
    )
    parser.add_argument(
        "review_json",
        type=Path,
        nargs="?",
        default=PENDING_SUBMIT_PATH,
        help=f"Review bundle path (default: {PENDING_SUBMIT_PATH}).",
    )
    parser.add_argument(
        "--quest",
        choices=("olympus", "mars"),
        default=None,
        help="Override quest from JSON (default: use JSON value).",
    )
    parser.add_argument(
        "--review-url",
        default=None,
        help="Override Shipd review page URL from JSON.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window.",
    )
    parser.add_argument(
        "--no-finalize",
        action="store_true",
        help="Fill the form but do not click the final Submit button.",
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=AUTH_STATE_PATH,
        help="Path to Playwright auth state.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run_submit_from_json(
            args.review_json,
            quest=args.quest,
            review_url=args.review_url,
            headless=not args.headed,
            auth_state_path=args.auth_state,
            finalize=not args.no_finalize,
        )
    except (PlaywrightTimeoutError, RuntimeError, ValueError, OSError) as exc:
        print(f"Submit from JSON failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
