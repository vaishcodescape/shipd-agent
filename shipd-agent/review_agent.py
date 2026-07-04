# Review Agent Entry Point

from review.agent import main, run_review_agent

__all__ = ["main", "run_review_agent"]

if __name__ == "__main__":
    raise SystemExit(main())
