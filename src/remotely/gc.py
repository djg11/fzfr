"""remotely.gc -- remotely gc sub-command: clean up orphaned session directories.

The reaper background process handles normal cleanup when the anchor shell
exits cleanly.  remotely gc handles the edge cases:
  - The machine was rebooted while a session was active (WORK_BASE is in
    /dev/shm which is volatile, but the session dirs survive a reboot if
    WORK_BASE falls back to /tmp).
  - The reaper itself crashed.
  - WORK_BASE filled up due to large binary stream files.

Usage:
    remotely gc [--dry-run]

    --dry-run   Print what would be removed without removing anything.
"""

import os
import sys

from .session import gc_stale_sessions
from .workbase import WORK_BASE


def cmd_gc(argv: list) -> int:
    """Entry point for the remotely gc sub-command."""
    dry_run = "--dry-run" in argv

    if dry_run:
        # Show what would be removed without touching anything.

        uid = os.getuid()
        uid_dir = WORK_BASE / f"u{uid}"
        if not uid_dir.is_dir():
            print("remotely gc: nothing to clean up.")
            return 0

        found = False
        for sess_dir in uid_dir.iterdir():
            if not sess_dir.is_dir() or not sess_dir.name.startswith("s"):
                continue
            try:
                anchor_pid = int(sess_dir.name[1:])
                os.kill(anchor_pid, 0)
                # Process still alive -- skip.
            except (ValueError, ProcessLookupError, PermissionError):
                print(f"  would remove: {sess_dir}")
                found = True

        if not found:
            print("remotely gc: nothing to clean up.")
        return 0

    gc_stale_sessions()
    print("remotely gc: done.", file=sys.stderr)
    return 0
