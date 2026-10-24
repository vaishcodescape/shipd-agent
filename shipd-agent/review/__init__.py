# Autonomous review agent (LangGraph pipeline).

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from review.agent import run_review_agent

__all__ = ["run_review_agent"]


def __getattr__(name: str) -> Any:
    # Lazy so light submodules (review.credentials, review.llm_setup) can be
    # imported without loading LangGraph and every provider SDK.
    if name == "run_review_agent":
        from review.agent import run_review_agent

        return run_review_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
