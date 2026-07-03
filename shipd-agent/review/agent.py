# Shipd autonomous review agent — public API and CLI.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from review.config import ReviewConfig, get_review_config
from review.graph import run_review_graph


def run_review_agent(
    repo_path: Path | str,
    quest: str,
    review_url: str,
    page: Any = None,
    *,
    config: ReviewConfig | None = None,
) -> dict:
    """Run the LangGraph review pipeline and return a submit-ready review dict."""
    path = Path(repo_path)
    config = config or get_review_config()

    return run_review_graph(
        repo_path=str(path),
        quest=quest,
        review_url=review_url,
        config=config,
        page=page,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Shipd autonomous review agent on a cloned submission.",
    )
    parser.add_argument(
        "repo_path",
        type=Path,
        help="Path to the cloned submission directory.",
    )
    parser.add_argument(
        "--quest",
        choices=("olympus", "mars"),
        default="olympus",
        help="Quest mode (default: olympus).",
    )
    parser.add_argument(
        "--review-url",
        default="",
        help="Shipd review page URL (optional for standalone CLI).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run Phase 0 and context only; skip LLM API calls.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write review JSON to this file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = get_review_config(dry_run_override=args.dry_run or None)

    result = run_review_agent(
        repo_path=args.repo_path,
        quest=args.quest,
        review_url=args.review_url,
        config=config,
    )

    payload = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote review to {args.output}", file=sys.stderr)
    else:
        print(payload)

    if not result.get("decision"):
        print("Error: review result missing 'decision'.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
