"""remotely.session -- Host-keyed SSH session manager.

A session represents one persistent SSH connection to one host. Sessions are
global to the user -- multiple callers (Television channels, fzf preview
windows, parallel list calls) that reference the same host all share one
ControlMaster socket.

Session directories live under WORK_BASE/sessions/<hash(host)>/:

    /dev/shm/remotely/sessions/a3f1b2c4/
        ssh.sock    -- ControlMaster socket
        ssh.lock    -- flock lock, held while creating the socket

The hash is the first 16 hex chars of SHA-256(host) so the directory name
is safe for all filesystem characters regardless of the host string.

Public API:

    session_dir(host)         -- Path to the session directory (creates it)
    socket_path(host)         -- Path to the ControlMaster socket for host
    acquire_socket(host)      -- Ensure socket exists, return its path
    release_session(host)     -- Close the ControlMaster and remove the dir
    ssh_opts_for(host)        -- SSH option list reusing the session socket
"""

import fcntl
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import CONFIG
from .workbase import WORK_BASE, _assert_not_symlink


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SESSIONS_DIR = WORK_BASE / "sessions"


def _host_hash(host: str) -> str:
    """Return a 16-char hex digest of the host string for use as a dir name."""
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def session_dir(host: str) -> Path:
    """Return the session directory for host, creating it if necessary.

    SECURITY: Directory is created with mode 0o700 so the ControlMaster
    socket inside is not accessible to other local users.
    """
    _assert_not_symlink(_SESSIONS_DIR)
    _SESSIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_not_symlink(_SESSIONS_DIR)

    d = _SESSIONS_DIR / _host_hash(host)
    _assert_not_symlink(d)
    d.mkdir(mode=0o700, exist_ok=True)
    _assert_not_symlink(d)
    return d


def socket_path(host: str) -> Path:
    """Return the ControlMaster socket path for host."""
    return session_dir(host) / "ssh.sock"


def _lock_path(host: str) -> Path:
    return session_dir(host) / "ssh.lock"


# ---------------------------------------------------------------------------
# Socket lifecycle
# ---------------------------------------------------------------------------


def acquire_socket(host: str) -> str:
    """Ensure a live ControlMaster socket exists for host and return its path.

    Uses flock so that parallel callers (e.g. Television's async preview
    threads) wait rather than racing to create the socket. The first caller
    wins the lock and starts the master; subsequent callers find the socket
    already alive and return immediately.

    Returns the socket path as a string, or "" if the connection failed.
    """
    sock = socket_path(host)
    lock = _lock_path(host)

    with open(lock, "w") as lf:
        # DESIGN: LOCK_EX blocks until any other acquire_socket for this host
        # finishes. This is the correct behaviour -- we want serialized socket
        # creation, not parallel failures.
        fcntl.flock(lf, fcntl.LOCK_EX)

        # Fast path: socket already exists and is alive.
        if sock.exists():
            r = subprocess.run(
                ["ssh", "-O", "check", "-S", str(sock), host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                return str(sock)
            # Dead socket -- remove it and fall through to create a new one.
            sock.unlink(missing_ok=True)

        # Slow path: start a new ControlMaster.
        persist = int(CONFIG.get("ssh_control_persist", 60))
        strict = CONFIG.get("ssh_strict_host_key_checking", True)

        opts = [
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPath={sock}",
            "-o",
            f"ControlPersist={persist}",
            "-o",
            "ConnectTimeout=5",
        ]
        if strict:
            known_hosts = session_dir(host) / "known_hosts"
            known_hosts.touch(mode=0o600, exist_ok=True)
            opts += [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
            ]

        r = subprocess.run(
            ["ssh"] + opts + ["-N", "-f", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace").strip()
            print(f"remotely: failed to connect to {host}: {err}", file=sys.stderr)
            return ""

        return str(sock)


def release_session(host: str) -> None:
    """Close the ControlMaster for host and remove the session directory.

    Safe to call even if no session exists for this host.
    """
    sock = socket_path(host)
    if sock.exists():
        subprocess.run(
            ["ssh", "-O", "exit", "-S", str(sock), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    d = session_dir(host)
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)


def ssh_opts_for(host: str) -> list:
    """Return SSH option flags that reuse the session socket for host.

    If no session socket exists yet for this host, returns options that will
    create one on first connection (ControlMaster=auto).

    DESIGN: Callers that need a guaranteed live socket should call
    acquire_socket() first. This function is for callers that are happy to
    let SSH handle master creation lazily (e.g. one-shot commands).
    """
    sock = socket_path(host)
    persist = int(CONFIG.get("ssh_control_persist", 60))
    strict = CONFIG.get("ssh_strict_host_key_checking", True)

    opts = [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={sock}",
        "-o",
        f"ControlPersist={persist}",
        "-o",
        "ConnectTimeout=5",
    ]
    if strict:
        known_hosts = session_dir(host) / "known_hosts"
        known_hosts.touch(mode=0o600, exist_ok=True)
        opts += [
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
        ]
    return opts
