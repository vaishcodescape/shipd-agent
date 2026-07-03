"""Compatibility entry point — delegates to workflow.orchestrator."""

from workflow.orchestrator import main

if __name__ == "__main__":
    raise SystemExit(main())
