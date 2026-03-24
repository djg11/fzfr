"""remotely.session -- Host-keyed SSH session manager.

Two modes depending on config["ssh_multiplexing"]:

DEFERRED MODE (default, ssh_multiplexing = false):
    The user's ~/.ssh/config already handles ControlMaster/ControlPath.
    remotely passes NO extra -o flags to ssh -- it just calls ssh normally
    and the user's config multiplexes automatically. acquire_socket()
    returns the sentinel value SSH_DEFERRED so callers know to use plain
    ssh with no ControlPath override.

MANAGED MODE (ssh_multiplexing = true):
    remotely manages its own ControlMaster socket per host under
    WORK_BASE/sessions/<hash(host)>/ssh.sock. Use this only when your
    ~/.ssh/config does NOT already have ControlMaster -- two masters on
    the same host conflict and cause spurious auth prompts.

    In managed mode acquire_socket() starts the master with ssh -N -M -f
    so authentication prompts (password, YubiKey, host key confirmation)
    reach the terminal. After auth succeeds ssh backgrounds itself and
    ControlPersist handles cleanup.

Session directories (managed mode only):

    /dev/shm/remotely/sessions/a3f1b2c4/
        ssh.sock    -- ControlMaster socket
        ssh.lock    -- flock, held while creating the master
        known_hosts -- per-session known_hosts (0o600)

Public API:
    SSH_DEFERRED        -- sentinel: use plain ssh, no ControlPath override
    acquire_socket(host)  -- return socket path or SSH_DEFERRED
    release_session(host) -- close managed socket and remove session dir
    ssh_opts_for(host)    -- SSH option list for callers using the socket
"""

import fcntl
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import CONFIG
from .workbase import WORK_BASE, _assert_not_symlink


# Sentinel returned by acquire_socket() when ssh_multiplexing is false.
# Callers should pass NO extra -o flags to ssh -- ~/.ssh/config handles it.
SSH_DEFERRED = ""

_SESSIONS_DIR = WORK_BASE / "sessions"
_SOCKET_READY_TIMEOUT = 10


def _host_hash(host: str) -> str:
    """Return a 16-char hex digest of the host string for use as a dir name."""
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def session_dir(host: str) -> Path:
    """Return the session directory for host, creating it (mode 0o700) if needed."""
    _assert_not_symlink(_SESSIONS_DIR)
    _SESSIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_not_symlink(_SESSIONS_DIR)
    d = _SESSIONS_DIR / _host_hash(host)
    _assert_not_symlink(d)
    d.mkdir(mode=0o700, exist_ok=True)
    _assert_not_symlink(d)
    return d


def socket_path(host: str) -> Path:
    """Return the managed ControlMaster socket path for host."""
    return session_dir(host) / "ssh.sock"


def _lock_path(host: str) -> Path:
    return session_dir(host) / "ssh.lock"


# ---------------------------------------------------------------------------
# Socket liveness
# ---------------------------------------------------------------------------


def _socket_alive(host: str) -> bool:
    """Return True if the managed socket exists and the master responds."""
    sock = socket_path(host)
    if not sock.exists():
        return False
    r = subprocess.run(
        ["ssh", "-O", "check", "-o", f"ControlPath={sock}", host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Managed master startup
# ---------------------------------------------------------------------------


def _start_master(host: str) -> bool:
    """Start an SSH ControlMaster for host (managed mode).

    Runs ssh -N -M -f so that authentication prompts reach the terminal.
    SSH backgrounds itself after auth succeeds; ControlPersist handles
    cleanup when the connection is idle.

    Returns True if the socket appeared, False on auth failure or timeout.
    """
    sock = socket_path(host)
    persist = int(CONFIG.get("ssh_control_persist", 60))

    # SECURITY: Use our per-session known_hosts (0o600) so host key
    # verification is not silently skipped.
    known_hosts = sock.parent / "known_hosts"
    known_hosts.touch(mode=0o600, exist_ok=True)

    opts = [
        "-N",
        "-M",
        "-f",
        "-o",
        "ControlMaster=yes",
        "-o",
        f"ControlPath={sock}",
        "-o",
        f"ControlPersist={persist}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]

    r = subprocess.run(["ssh"] + opts + [host])
    if r.returncode != 0:
        print(
            f"remotely: SSH connection to {host} failed (rc={r.returncode})",
            file=sys.stderr,
        )
        return False

    deadline = time.monotonic() + _SOCKET_READY_TIMEOUT
    while time.monotonic() < deadline:
        if sock.exists():
            return True
        time.sleep(0.05)

    print(
        f"remotely: timed out waiting for socket after connecting to {host}",
        file=sys.stderr,
    )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def acquire_socket(host: str) -> str:
    """Return a ControlMaster socket path for host, or SSH_DEFERRED.

    DEFERRED MODE (ssh_multiplexing = false, the default):
        Returns SSH_DEFERRED immediately. Callers must use plain ssh with
        no extra -o flags -- ~/.ssh/config handles multiplexing.

    MANAGED MODE (ssh_multiplexing = true):
        Creates or reuses a managed socket under WORK_BASE/sessions/.
        Uses flock so parallel callers wait rather than racing to start
        the master.
        Returns the socket path on success, or SSH_DEFERRED on failure
        (so callers can fall back to a plain ssh attempt).
    """
    if not CONFIG.get("ssh_multiplexing", False):
        return SSH_DEFERRED

    lock = _lock_path(host)
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)

        if _socket_alive(host):
            return str(socket_path(host))

        try:
            socket_path(host).unlink()
        except (FileNotFoundError, OSError):
            pass

        if not _start_master(host):
            return SSH_DEFERRED

        return str(socket_path(host))


def release_session(host: str) -> None:
    """Close the managed ControlMaster for host and remove the session dir.

    No-op in deferred mode or if no session exists.
    """
    if not CONFIG.get("ssh_multiplexing", False):
        return
    sock = socket_path(host)
    if sock.exists():
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={sock}", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    d = session_dir(host)
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)


def ssh_opts_for(host: str) -> list:
    """Return SSH option flags appropriate for the current multiplexing mode.

    DEFERRED MODE: returns [] so ~/.ssh/config options are used unchanged.
    MANAGED MODE: returns ControlPath flags pointing at the managed socket.
    """
    if not CONFIG.get("ssh_multiplexing", False):
        return []

    sock = socket_path(host)
    persist = int(CONFIG.get("ssh_control_persist", 60))
    known_hosts = session_dir(host) / "known_hosts"
    known_hosts.touch(mode=0o600, exist_ok=True)

    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={sock}",
        "-o",
        f"ControlPersist={persist}",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]
