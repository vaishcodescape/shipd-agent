"""Compatibility entry point — delegates to workflow.time_logs."""

from workflow.time_logs import main

if __name__ == "__main__":
    raise SystemExit(main())
