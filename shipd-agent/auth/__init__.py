# Auth helpers and CLI for Shipd sign-in.

import os as _os
import sys as _sys

# macOS libmalloc prints "MallocStackLogging: can't turn off malloc stack logging
# because it was not enabled" from every subprocess we spawn (git, docker, bash,
# chromium) when these debug vars are inherited from the launching environment but
# were never actually active — pure noise that floods logs/run.log. This package is
# imported before any subprocess or browser launch in every entry path, so drop the
# vars here to keep child processes quiet.
if _sys.platform == "darwin":
    for _var in (
        "MallocStackLogging",
        "MallocStackLoggingNoCompact",
        "MallocStackLoggingDirectory",
    ):
        _os.environ.pop(_var, None)
    del _var
del _os, _sys

from .auth import *  # noqa: F403,E402
