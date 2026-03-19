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
    opts = []
    if ssh_control:
        persist = int(CONFIG.get("ssh_control_persist", 60))
        opts += [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={ssh_control}",
            "-o",
            f"ControlPersist={persist}",
        ]
        # SECURITY: Enforce host key checking and use a temporary known_hosts
        #           file to prevent MITM attacks and avoid modifying the user's
        #           global ~/.ssh/known_hosts for transient connections.
        if CONFIG.get("ssh_strict_host_key_checking", True):
            known_hosts_path = Path(ssh_control).parent / "known_hosts"
            # SECURITY: Touch the known_hosts file with 0o600 before the first
            #           SSH call. Without this, the file inherits the user's
            #           umask (often 0o644), making host keys world-readable.
            #           touch() is a no-op if the file already exists.
            known_hosts_path.touch(mode=0o600, exist_ok=True)
            opts += [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts_path}",
            ]
    return opts


def _ssh_opts_str(ssh_control: str) -> str:
    """Shell-quoted string form of _ssh_opts(), for embedding in shell commands."""
    return " ".join(shlex.quote(o) for o in _ssh_opts(ssh_control))
