"""fzfr.ssh — SSH option construction for multiplexed connections.

Provides _ssh_opts() and _ssh_opts_str() which return the ControlMaster flags
needed when config["ssh_multiplexing"] is True, or an empty list/string when
fzfr defers to the user's ~/.ssh/config.
"""
import shlex
from pathlib import Path

from .config import CONFIG


def _ssh_opts(ssh_control: str) -> list[str]:
    """Return SSH multiplexing flags, or [] to let ~/.ssh/config decide.

    Empty ssh_control (the default) → no flags injected. ssh falls through
    to whatever ControlMaster/ControlPath is in ~/.ssh/config. This is the
    right behaviour for users who already have multiplexing configured and
    don't want a second master connection (e.g. YubiKey users).

    Non-empty ssh_control → inject ControlMaster=auto + the given socket
    path so fzfr manages its own dedicated multiplexed connection.
    Activated only when config["ssh_multiplexing"] is True.
    """
    if not ssh_control:
        return []

    persist = int(CONFIG.get("ssh_control_persist", 60))
    opts = [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ssh_control}",
        "-o", f"ControlPersist={persist}",
    ]

    if CONFIG.get("ssh_strict_host_key_checking", True):
        known_hosts_path = Path(ssh_control).parent / "known_hosts"
        # SECURITY: Touch with 0o600 before the first SSH call. Without this
        #           the file inherits the user's umask (often 0o644), making
        #           host keys world-readable.
        known_hosts_path.touch(mode=0o600, exist_ok=True)
        opts += [
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts_path}",
        ]

    return opts


def _ssh_opts_str(ssh_control: str) -> str:
    """Shell-quoted string form of _ssh_opts(), for embedding in shell commands."""
    return " ".join(shlex.quote(o) for o in _ssh_opts(ssh_control))
