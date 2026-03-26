"""remotely.ssh -- SSH option construction for multiplexed connections.

Provides _ssh_opts() and _ssh_opts_str(), which return the ControlMaster
flags to inject when config["ssh_multiplexing"] is True, or empty
list / string when remotely defers entirely to the user's ~/.ssh/config.

When to use managed multiplexing
---------------------------------
Set ``"ssh_multiplexing": true`` only when ~/.ssh/config does NOT already
configure ControlMaster. Two masters on the same host conflict and trigger
spurious authentication prompts (YubiKey users are especially affected).
The default (false) defers to the user's own SSH configuration, which is
always the safer choice.
"""

import shlex
from pathlib import Path
from typing import List

from .config import CONFIG


def _ssh_opts(ssh_control: str) -> List[str]:
    """Return SSH multiplexing option flags for the given socket path.

    When ssh_control is empty (the default / deferred mode) no flags are
    injected, so ssh falls through to whatever ControlMaster / ControlPath
    is configured in ~/.ssh/config. This is correct for users who already
    have multiplexing configured and do not want a second master.

    When ssh_control is non-empty (managed mode, config ssh_multiplexing=true)
    the returned flags instruct ssh to reuse the given socket, creating a new
    master connection if none exists yet (ControlMaster=auto).

    SECURITY: When StrictHostKeyChecking is enabled (the default), a
    per-session known_hosts file is created beside the socket with mode
    0o600 so host keys are never world-readable. The file is touched before
    the first SSH call; without this the file inherits the user's umask
    (often 0o644).
    """
    if not ssh_control:
        return []

    persist = int(CONFIG.get("ssh_control_persist", 60))
    opts = [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={ssh_control}",
        "-o",
        f"ControlPersist={persist}",
    ]

    if CONFIG.get("ssh_strict_host_key_checking", True):
        known_hosts = Path(ssh_control).parent / "known_hosts"
        # SECURITY: Touch with 0o600 before the first SSH call so the file
        #           is created with restrictive permissions rather than
        #           inheriting the user's umask.
        known_hosts.touch(mode=0o600, exist_ok=True)
        opts += [
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
        ]

    return opts


def _ssh_opts_str(ssh_control: str) -> str:
    """Return _ssh_opts() as a shell-quoted string for embedding in shell commands."""
    return " ".join(shlex.quote(o) for o in _ssh_opts(ssh_control))
