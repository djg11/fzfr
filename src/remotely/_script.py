"""remotely._script -- VERSION and script-self-reference constants.

This module has NO imports from other remotely submodules so it can be
safely imported by any module without creating circular dependencies.

Constants exported for use by remote.py and search.py:

    VERSION          -- human-readable version string
    SELF             -- absolute path to the built single-file remotely script
    SCRIPT_BYTES     -- full contents of the built script (read once at startup)
    SCRIPT_HASH      -- 16-char hex SHA-256 prefix of SCRIPT_BYTES
    SCRIPT_BOOTSTRAP -- tiny bootstrap sent to the remote on every preview call
    _BOOTSTRAP_CACHE_MISS -- sentinel exit code meaning the remote cache is cold
"""

import hashlib
import sys
from pathlib import Path


VERSION = "0.9.5"


_SHEBANG = b"#!/usr/bin/env python3"


def _is_built_script(path):
    # type: (Path) -> bool
    """Return True if path is the built single-file remotely script.

    Checks for the shebang line rather than file size -- size thresholds are
    fragile as the codebase grows or shrinks.
    """
    try:
        with path.open("rb") as f:
            return f.read(len(_SHEBANG)) == _SHEBANG
    except OSError:
        return False


def _find_self():
    # type: () -> Optional[str]
    """Locate the built single-file remotely script.

    When running from the built file: returns __file__ (the script itself).
    When running from the src/ package: walks up to the repo root to find
    the built remotely, so SCRIPT_BYTES contains the full script for SSH remote
    preview. Falls back to __file__ if the built file is absent.
    """
    here = Path(__file__).resolve()
    if _is_built_script(here):
        return str(here)
    # Package: look for the built remotely two levels up (src/remotely/ -> repo root)
    built = here.parent.parent.parent / "remotely"
    if _is_built_script(built):
        return str(built)
    # Fallback: running from src/ without a built file.
    # Local search works fine; SSH remote preview will send the wrong script.
    print(
        "remotely: warning: built remotely not found -- run 'make build' for SSH remote preview.",
        file=sys.stderr,
    )
    return str(here) if here.exists() else None


SELF = _find_self()

# PERF:     Read once at import time and reused for every remotely-remote-preview
#           call. Without caching, each cursor movement would read ~60 KB from
#           disk at fzf's typical 50-100 ms preview latency budget.
# SECURITY: Snapshot is taken at process start. Replacing the source file on
#           disk after launch has no effect on the running session.
# LIMITATION: If this script is executed via 'python3 -' (stdin), SELF is None
#             and SCRIPT_BYTES is empty. Remote callbacks then have no script to
#             pipe and will silently produce no preview output.
SCRIPT_BYTES = Path(SELF).read_bytes() if SELF else b""

SCRIPT_HASH = hashlib.sha256(SCRIPT_BYTES).hexdigest()[:16] if SCRIPT_BYTES else ""  # type: str

# Bootstrap script sent to the remote on every preview call (~250 bytes).
#
# Checks /dev/shm/remotely/<hash>.py first (tmpfs, RAM-backed, preferred) then
# ~/.cache/remotely/<hash>.py (persistent disk fallback on macOS and systems
# without /dev/shm). On hit: runs the cached file directly as a path argument
# -- one python3 process. On miss: exits 99 so _upload_remote_script() uploads
# to exactly one of those locations and retries.
#
# PERF: Uses os.path.exists() only -- no file read, no hash computation on
# the hot path. The hash embedded in the filename is the integrity check:
# _upload_remote_script writes atomically (tmp -> rename) so the file at
# that path is always either absent or complete. Reading and hashing 60KB on
# every preview call adds measurable latency on low-latency (LAN) links where
# the SSH RTT is only ~1ms, so the exists() check is deliberately kept cheap.
SCRIPT_BOOTSTRAP = (
    (
        b"import sys,os,subprocess\n"
        b'for p in[f"/dev/shm/remotely/{SCRIPT_HASH}.py",'
        b'os.path.expanduser("~/.cache/remotely/{SCRIPT_HASH}.py")]:\n'
        b"    if os.path.exists(p):\n"
        b"        sys.exit(subprocess.run([sys.executable,p]+sys.argv[1:]).returncode)\n"
        b"sys.exit(99)\n"
    )
    if SCRIPT_HASH
    else b""
)  # type: bytes

# Sentinel exit code: remote cache miss. Must not clash with remotely-preview's
# own exit codes (0, 1, 127).
_BOOTSTRAP_CACHE_MISS = 99
