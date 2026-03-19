"""fzfr._script — VERSION and script-self-reference constants.

This module has NO imports from other fzfr submodules so it can be
safely imported by any module without creating circular dependencies.
"""

import hashlib
from pathlib import Path


VERSION = "0.9.2"


_SHEBANG = b"#!/usr/bin/env python3"


def _is_built_script(path: Path) -> bool:
    """Return True if path is the built single-file fzfr script.

    Checks for the shebang line rather than file size — size thresholds are
    fragile as the codebase grows or shrinks.
    """
    try:
        with path.open("rb") as f:
            return f.read(len(_SHEBANG)) == _SHEBANG
    except OSError:
        return False


def _find_self() -> str | None:
    """Locate the built single-file fzfr script.

    When running from the built file: returns __file__ (the script itself).
    When running from the src/ package: walks up to the repo root to find
    the built fzfr, so SCRIPT_BYTES contains the full script for SSH remote
    preview. Falls back to __file__ if the built file is absent.
    """
    here = Path(__file__).resolve()
    if _is_built_script(here):
        return str(here)
    # Package: look for the built fzfr two levels up (src/fzfr/ -> repo root)
    built = here.parent.parent.parent / "fzfr"
    if _is_built_script(built):
        return str(built)
    # Fallback: running from src/ without a built file.
    # Local search works fine; SSH remote preview will send the wrong script.
    import sys as _sys
    print(
        "fzfr: warning: built fzfr not found — run 'make build' for SSH remote preview.",
        file=_sys.stderr,
    )
    return str(here) if here.exists() else None


SELF = _find_self()

# PERF:     Read once at import time and reused for every fzfr-remote-preview
#           call. Without caching, each cursor movement would read ~60 KB from
#           disk at fzf's typical 50-100 ms preview latency budget.
# SECURITY: Snapshot is taken at process start. Replacing the source file on
#           disk after launch has no effect on the running session.
# LIMITATION: If this script is executed via 'python3 -' (stdin), SELF is None
#             and SCRIPT_BYTES is empty. Remote callbacks then have no script to
#             pipe and will silently produce no preview output.
SCRIPT_BYTES = Path(SELF).read_bytes() if SELF else b""

SCRIPT_HASH: str = hashlib.sha256(SCRIPT_BYTES).hexdigest()[:16] if SCRIPT_BYTES else ""

# PERF: Tiny bootstrap script sent to the remote instead of the full ~60 KB
#       script on every preview call. Cache hit → exec cached copy; miss → 99.
SCRIPT_BOOTSTRAP: bytes = f"""import sys,os,subprocess
p=os.path.expanduser("~/.cache/fzfr/{SCRIPT_HASH}.py")
if os.path.exists(p):
    r=subprocess.run([sys.executable,p]+sys.argv[1:])
    sys.exit(r.returncode)
sys.exit(99)
""".encode() if SCRIPT_HASH else b""

# Sentinel exit code: remote cache miss. Must not clash with fzfr-preview's
# own exit codes (0, 1, 127).
_BOOTSTRAP_CACHE_MISS = 99
