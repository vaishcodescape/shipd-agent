# Make the package root (shipd-agent/) importable so tests can use
# `from review... / workflow... / stats...` regardless of the invocation cwd.

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

# Never touch the real OS keychain or ~/.config from tests: stored LLM key
# lookups (review/credentials.py) go to an empty, throwaway file store.
_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="shipd-test-config-")
os.environ["SHIPD_CREDENTIAL_STORE"] = "file"
os.environ["SHIPD_CONFIG_DIR"] = _TEST_CONFIG_DIR
atexit.register(shutil.rmtree, _TEST_CONFIG_DIR, ignore_errors=True)
