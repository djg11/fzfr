"""fzfr.workbase — Session working directory setup and security checks."""

import os
import sys
import tempfile
from pathlib import Path


def _get_work_base() -> Path:
    """Find the best place for temporary files (RAM-backed if possible).

    Prefers /dev/shm (Linux tmpfs) for low-latency I/O; falls back to the
    system temp directory on macOS and BSD.
    """
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK):
        return shm / "fzfr"
    return Path(tempfile.gettempdir()) / "fzfr"


WORK_BASE = _get_work_base()


# PERF: /dev/shm is a RAM-backed tmpfs on Linux. Placing the session directory,
#       state file, and preview temp files here avoids disk I/O for files that
#       are created and deleted on every cursor movement.
# SECURITY: /dev/shm has the sticky bit set but is world-writable, so an
#           attacker on the same machine could pre-create a symlink at our
#           intended path to redirect session files to a location they control.
#           We check for a symlink both before and after mkdir to close the
#           TOCTOU window: an attacker who wins the race between the first check
#           and mkdir would cause mkdir to follow the symlink and create a
#           directory at the symlink target — the post-mkdir check catches that.
def _assert_not_symlink(p: Path) -> None:
    if p.is_symlink():
        print(
            f"Error: {p} is a symlink — refusing to use it as the work "
            "directory. Remove the symlink and retry.",
            file=sys.stderr,
        )
        sys.exit(1)


_assert_not_symlink(WORK_BASE)
WORK_BASE.mkdir(parents=True, exist_ok=True)
_assert_not_symlink(WORK_BASE)
