"""remotely._script -- VERSION constant and script self-reference.

This module is imported by every other remotely module, so it must have
NO imports from other remotely submodules to avoid circular dependencies.

Exported constants
------------------
VERSION          -- human-readable version string, e.g. "0.9.5"
SELF             -- absolute path to the built single-file remotely script,
                   or None when running without a built file
SCRIPT_BYTES     -- full contents of the built script (read once at startup)
SCRIPT_HASH      -- 16-char hex SHA-256 prefix of SCRIPT_BYTES; used as the
                   remote cache filename stem
SCRIPT_BOOTSTRAP -- ~250-byte Python script sent to the remote host on every
                   preview call to check the local cache before uploading
_BOOTSTRAP_CACHE_MISS -- sentinel exit code (99) meaning "remote cache miss"
"""

import hashlib
import sys
from pathlib import Path


VERSION = "0.9.5"

_SHEBANG = b"#!/usr/bin/env python3"


def _is_built_script(path):
    # type: (Path) -> bool
    """Return True if path is the built single-file remotely script.

    Identifies the built file by its shebang line rather than by size or
    name -- size thresholds break as the codebase grows, and the filename
    varies between the installed ``remotely`` binary and ``remotely.py``
    used during PyPI packaging.
    """
    try:
        with path.open("rb") as f:
            return f.read(len(_SHEBANG)) == _SHEBANG
    except OSError:
        return False


def _find_self():
    # type: () -> Optional[str]
    """Locate the built single-file remotely script and return its path.

    Search order:
      1. ``__file__`` itself -- when running from the built monolith.
      2. Two levels up from ``__file__`` (``src/remotely/`` -> repo root) --
         when running from the ``src/`` package during development.
      3. Falls back to ``__file__`` with a warning if no built file is found.
         Local search still works; SSH remote preview will transfer the wrong
         (package) file and likely fail on the remote.

    Returns None only if no file exists at all (e.g. running via stdin).
    """
    here = Path(__file__).resolve()
    if _is_built_script(here):
        return str(here)

    # Development layout: src/remotely/_script.py -> repo root / remotely
    built = here.parent.parent.parent / "remotely"
    if _is_built_script(built):
        return str(built)

    print(
        "remotely: warning: built script not found -- "
        "run 'make build' to enable SSH remote preview.",
        file=sys.stderr,
    )
    return str(here) if here.exists() else None


SELF = _find_self()

# PERF: Read once at import time and reused for every SSH remote preview call.
#       Without caching each cursor movement would re-read ~100 KB from disk.
# SECURITY: Snapshot is taken at process start. Replacing the source file on
#           disk mid-session has no effect on the running process.
SCRIPT_BYTES = Path(SELF).read_bytes() if SELF else b""

# 16-char hex prefix of the SHA-256 of the script. Used as the remote cache
# filename stem so the remote host can identify a cached copy without reading
# or hashing the file contents (the name IS the integrity check).
SCRIPT_HASH = hashlib.sha256(SCRIPT_BYTES).hexdigest()[:16] if SCRIPT_BYTES else ""  # type: str


def _build_bootstrap(script_hash):
    # type: (str) -> bytes
    """Return the bootstrap script with script_hash substituted in.

    DESIGN: The bootstrap is piped to the remote python3 via SSH stdin and
    executed as literal source code. The hash must therefore be a string
    literal baked into the script -- it cannot reference a Python variable
    that only exists on the local side.

    Uses bytes.replace() on a template rather than an f-string so the
    result is always a plain, self-contained Python script.
    """
    if not script_hash:
        return b""

    template = (
        b"import sys,os,subprocess\n"
        b"_h='__HASH__'\n"
        b"for p in['/dev/shm/remotely/'+_h+'.py',"
        b"os.path.expanduser('~/.cache/remotely/'+_h+'.py')]:\n"
        b"    if os.path.exists(p):\n"
        b"        sys.exit(subprocess.run([sys.executable,p]+sys.argv[1:]).returncode)\n"
        b"sys.exit(99)\n"
    )
    return template.replace(b"__HASH__", script_hash.encode("ascii"))


# Bootstrap sent to the remote on every preview call (~250 bytes).
#
# Checks /dev/shm/remotely/<hash>.py (tmpfs, RAM-backed) then
# ~/.cache/remotely/<hash>.py (disk fallback for macOS / no-/dev/shm systems).
#   Hit  -> run the cached script directly; exit with its return code.
#   Miss -> exit 99 so _upload_remote_script() uploads and retries.
#
# PERF: Only os.path.exists() calls on the hot path -- no file reads, no
#       hashing. The hash in the filename is the integrity check: uploads are
#       atomic (tmp -> rename) so the file at that path is always complete.
SCRIPT_BOOTSTRAP = _build_bootstrap(SCRIPT_HASH)  # type: bytes

# Exit code 99 signals "remote cache miss". Must not collide with remotely's
# own exit codes (0 = success, 1 = error, 127 = command not found).
_BOOTSTRAP_CACHE_MISS = 99
