"""remotely.session -- Host-keyed SSH session manager.

A session represents one persistent SSH connection to one host. Sessions are
global to the user -- multiple callers (Television channels, fzf preview
windows, parallel list calls) that reference the same host all share one
ControlMaster socket.

Each session is a daemon process (one per host) that owns the SSH master.
When the daemon exits the master exits with it and the socket disappears --
no orphaned connections, no silent lingering credentials.

Session directories live under WORK_BASE/sessions/<hash(host)>/:

    /dev/shm/remotely/sessions/a3f1b2c4/
        ssh.sock    -- ControlMaster socket (gone when daemon exits)
        ssh.lock    -- flock lock, held while creating the daemon
        daemon.pid  -- PID of the owning daemon process
        known_hosts -- per-session known_hosts file

The hash is the first 16 hex chars of SHA-256(host) so the directory name
is safe for all filesystem characters regardless of the host string.

Daemon lifecycle:
    1. First caller acquires flock on ssh.lock.
    2. If no live daemon exists, start one via _start_daemon().
       The daemon runs ssh -N -M as a child and blocks until that child
       exits or the daemon receives SIGTERM.
    3. Caller releases flock and uses the socket normally.
    4. On SIGTERM/SIGINT the daemon kills the SSH master and removes
       the session directory.
    5. Subsequent callers reuse the socket -- flock + PID check confirm
       the daemon is still alive before returning.

Public API:
    session_dir(host)       -- Path to the session directory (creates it)
    socket_path(host)       -- Path to the ControlMaster socket for host
    acquire_socket(host)    -- Ensure a live daemon+socket exists, return path
    release_session(host)   -- Kill the daemon and remove the session dir
    ssh_opts_for(host)      -- SSH option list reusing the session socket
"""

import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import CONFIG
from .workbase import WORK_BASE, _assert_not_symlink


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SESSIONS_DIR = WORK_BASE / "sessions"

# Seconds to wait for the SSH master to create its socket after the daemon
# forks. On a fast LAN this is typically <1 s; 10 s covers slow WAN links.
_SOCKET_READY_TIMEOUT = 10


def _host_hash(host: str) -> str:
    """Return a 16-char hex digest of the host string for use as a dir name."""
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def session_dir(host: str) -> Path:
    """Return the session directory for host, creating it if necessary.

    SECURITY: Created with mode 0o700 so the ControlMaster socket and
    daemon.pid inside are not accessible to other local users.
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


def _pid_path(host: str) -> Path:
    return session_dir(host) / "daemon.pid"


# ---------------------------------------------------------------------------
# Daemon liveness check
# ---------------------------------------------------------------------------


def _daemon_alive(host: str) -> bool:
    """Return True if a live daemon process owns the socket for this host.

    Reads daemon.pid and sends signal 0 to verify the process exists.
    Also checks the socket file -- a stale PID without a socket means the
    daemon crashed without cleaning up and should not be trusted.
    """
    pid_file = _pid_path(host)
    sock = socket_path(host)
    if not pid_file.exists() or not sock.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # signal 0: check existence only, sends nothing
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


# ---------------------------------------------------------------------------
# Daemon process
# ---------------------------------------------------------------------------


def _build_ssh_master_cmd(host: str, sock: Path) -> list:
    """Build the ssh -N -M argv list for the master process."""
    persist = int(CONFIG.get("ssh_control_persist", 60))
    strict = CONFIG.get("ssh_strict_host_key_checking", True)

    opts = [
        "-N",  # no remote command
        "-M",  # become ControlMaster
        "-o",
        "ControlMaster=yes",
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
    ]
    if strict:
        known_hosts = sock.parent / "known_hosts"
        known_hosts.touch(mode=0o600, exist_ok=True)
        opts += [
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
        ]
    return ["ssh"] + opts + [host]


def _run_daemon(host: str) -> None:
    """Daemon entry point -- runs in the grandchild process after double-fork.

    Starts the SSH master as a subprocess, writes our PID to daemon.pid,
    then blocks until the master exits or we receive SIGTERM/SIGINT.

    DESIGN: This function runs after os.fork() x2. It must never return --
    it either exits via os._exit() or is killed. All state is local.
    """
    sock = socket_path(host)
    pid_file = _pid_path(host)
    master_proc = None

    def _cleanup(signum=None, frame=None):
        if master_proc is not None:
            try:
                master_proc.terminate()
                master_proc.wait(timeout=3)
            except Exception:
                try:
                    master_proc.kill()
                except Exception:
                    pass
        try:
            sock.unlink(missing_ok=True)
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        cmd = _build_ssh_master_cmd(host, sock)
        master_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Write PID after the master starts so callers can verify liveness.
        pid_file.write_text(str(os.getpid()))

        # Block until the master dies naturally (remote closed, idle timeout).
        master_proc.wait()
    except Exception:
        pass
    finally:
        _cleanup()


def _start_daemon(host: str) -> bool:
    """Fork a daemon process for host and wait for its socket to appear.

    Uses a double-fork so the daemon is reparented to init and does not
    become a zombie when the caller exits.

    Returns True if the socket became available within _SOCKET_READY_TIMEOUT,
    False if the connection failed or timed out.
    """
    sock = socket_path(host)

    pid = os.fork()
    if pid == 0:
        # First child: start a new session, then fork again.
        os.setsid()
        pid2 = os.fork()
        if pid2 == 0:
            # Second child (grandchild): actual daemon -- never returns.
            _run_daemon(host)
            os._exit(0)
        else:
            # First child exits immediately so the grandchild is reparented.
            os._exit(0)
    else:
        # Original process: reap the first child immediately.
        os.waitpid(pid, 0)

    # Wait for the socket to appear -- SSH creates it once the connection
    # is fully established and the master is ready to accept multiplexed calls.
    deadline = time.monotonic() + _SOCKET_READY_TIMEOUT
    while time.monotonic() < deadline:
        if sock.exists() and _daemon_alive(host):
            return True
        time.sleep(0.1)

    print(
        f"remotely: timed out waiting for SSH connection to {host}",
        file=sys.stderr,
    )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def acquire_socket(host: str) -> str:
    """Ensure a live daemon and ControlMaster socket exist for host.

    Uses flock so parallel callers (Television preview threads, fzf preview
    windows) wait rather than racing to start the daemon. The first caller
    wins the lock and starts the daemon; all others find it already alive.

    Returns the socket path as a string, or "" on failure.
    """
    lock = _lock_path(host)

    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)

        if _daemon_alive(host):
            return str(socket_path(host))

        # Stale state -- clean up before starting fresh.
        socket_path(host).unlink(missing_ok=True)
        _pid_path(host).unlink(missing_ok=True)

        if not _start_daemon(host):
            return ""

        return str(socket_path(host))


def release_session(host: str) -> None:
    """Kill the daemon for host and remove the session directory.

    Safe to call even if no session exists for this host.
    """
    pid_file = _pid_path(host)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            pass

    d = session_dir(host)
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)


def ssh_opts_for(host: str) -> list:
    """Return SSH option flags that reuse the session socket for host.

    Callers that need a guaranteed live socket should call acquire_socket()
    first. This function is for callers that are happy to let SSH handle
    master creation lazily (e.g. one-shot commands where a new master is
    acceptable if none exists yet).
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
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
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
