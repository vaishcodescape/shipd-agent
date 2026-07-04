# Make the package root (shipd-agent/) importable so tests can use
# `from review... / workflow... / stats...` regardless of the invocation cwd.

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
