"""remotely.workbase -- Session working directory selection and symlink safety.

WORK_BASE is the root under which all per-session directories live. The module
selects the best available location at import time and creates it immediately.

Location preference
-------------------
1. /dev/shm/remotely   Linux tmpfs -- RAM-backed, low-latency I/O. Ideal for
                        files that are created and deleted on every cursor move.
2. <tempdir>/remotely  System temp directory -- used on macOS and any system
                        without a writable /dev/shm.

Symlink safety
--------------
/dev/shm is world-writable with the sticky bit set. An attacker on the same
machine could pre-create a symlink at our intended path to redirect session
files to a location they control. We guard against this with a double-check:

  1. Assert the path is NOT a symlink before calling mkdir().
  2. Assert again AFTER mkdir() to detect the TOCTOU race where an attacker
     wins the window between our first check and the mkdir call (mkdir would
     follow the symlink and create a real directory at the target; the
     post-mkdir check catches that the original path is still a symlink).
"""

import os
import sys
import tempfile
from pathlib import Path


def _get_work_base() -> Path:
    """Return the best available path for temporary session files.

    Prefers /dev/shm (Linux tmpfs) for low-latency I/O; falls back to the
    system temp directory on macOS and any system without a writable /dev/shm.
    """
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK):
        return shm / "remotely"
    return Path(tempfile.gettempdir()) / "remotely"


def _assert_not_symlink(path: Path) -> None:
    """Exit with an error if path is a symbolic link.

    Called before and after mkdir() on every directory we create under
    WORK_BASE to guard against symlink-redirect attacks on world-writable
    filesystems like /dev/shm.
    """
    if path.is_symlink():
        print(
            f"Error: {path} is a symlink -- refusing to use it as a session "
            "directory. Remove the symlink and retry.",
            file=sys.stderr,
        )
        sys.exit(1)


WORK_BASE = _get_work_base()

_assert_not_symlink(WORK_BASE)
WORK_BASE.mkdir(parents=True, exist_ok=True)
_assert_not_symlink(WORK_BASE)
