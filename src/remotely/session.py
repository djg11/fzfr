"""remotely.session -- Anchor-PID session manager.

DESIGN: In the headless API (remotely list / preview / open), there is no
long-running remotely process that can own cleanup. Instead, each short-lived
remotely invocation discovers the PID of its interactive parent shell and uses
that as the session anchor.

Session directory layout:
    /dev/shm/remotely/u<uid>/s<anchor_pid>/
        reaper.pid              -- PID of the background reaper process
        reaper.py               -- reaper script (written once, then execv'd)
        ssh-<hash>.sock         -- ControlMaster socket (managed mode)
        ssh-<hash>.lock         -- flock preventing parallel master creation
        ssh-<hash>.hosts        -- per-host known_hosts file (0o600)
        ssh-<hash>.host         -- hostname string (read by reaper on cleanup)
        preview/                -- file-backed preview cache (LRU)
        stream/                 -- streamed binary files (remotely open)

Anchor PID:
    Every subprocess spawned by fzf (--preview, --bind execute) has fzf as its
    direct parent.  os.getppid() from within any such subprocess therefore
    returns fzf's PID -- the same value for every remotely invocation in one
    fzf session.  This is used as the session anchor.

    remotely list is invoked by the shell (not fzf), so its os.getppid() is
    the shell PID.  Because remotely list starts the session, its anchor PID
    is also valid -- the reaper monitors the shell, which outlives fzf anyway.

Reaper:
    The first remotely call in a session writes a small Python script to
    session_dir/reaper.py and double-forks a detached python3 process to run
    it.  The reaper polls os.kill(anchor_pid, 0) every 2 s.  When the anchor
    exits, the reaper closes all ControlMaster sockets and removes the session
    directory.

    Writing to a file (rather than passing source via -c) avoids all quoting
    and escaping issues when the source is exec'd via os.execv.

Public API:
    SSH_DEFERRED            -- sentinel: use plain ssh, no ControlPath
    get_anchor_pid()        -- return the interactive parent PID
    get_session_dir()       -- return (creating if needed) the session dir
    ensure_reaper()         -- start the reaper if not already running
    acquire_socket(host)    -- return socket path or SSH_DEFERRED
    release_session(host)   -- close a managed socket for one host
    ssh_opts_for(host)      -- SSH option list for the current mode
    gc_stale_sessions()     -- remove sessions whose anchor is gone
"""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from .config import CONFIG
from .workbase import WORK_BASE, _assert_not_symlink


# Sentinel returned by acquire_socket() in deferred mode.
SSH_DEFERRED = ""

_SOCKET_READY_TIMEOUT = 10

# Timeout for the Python-side subprocess.run() backstop on SSH calls.
# Must be larger than the SSH-side ConnectTimeout so SSH's own timeout
# fires first and produces a clean error message.
_SSH_CHECK_TIMEOUT = 5  # for ssh -O check (local socket probe, no TCP)
_SSH_CONNECT_TIMEOUT = 10  # ConnectTimeout passed to ssh -o
_SSH_RUN_TIMEOUT = 15  # subprocess.run() backstop for _start_master


# ---------------------------------------------------------------------------
# Anchor PID
# ---------------------------------------------------------------------------


def get_anchor_pid() -> int:
    """Return the PID of the anchor process for this session.

    By default, this is the direct parent PID. When called from fzf, this is
    fzf's PID. When called from the shell, it is the shell's PID.

    Can be overridden via the REMOTELY_SESSION_PID environment variable to
    force multiple processes (e.g. a wrapper script and its fzf child) into
    the same session.
    """
    env_pid = os.environ.get("REMOTELY_SESSION_PID")
    if env_pid:
        try:
            return int(env_pid)
        except ValueError:
            pass
    return os.getppid()


# ---------------------------------------------------------------------------
# Session directory
# ---------------------------------------------------------------------------


def get_session_dir(anchor_pid: Optional[int] = None) -> Path:
    """Return (and create) the session directory for anchor_pid.

    Path: WORK_BASE/u<uid>/s<anchor_pid>/
    Created with mode 0o700.  Symlink safety checks at every level.
    """
    if anchor_pid is None:
        anchor_pid = get_anchor_pid()

    uid = os.getuid()
    uid_dir = WORK_BASE / ("u" + str(uid))
    _assert_not_symlink(uid_dir)
    uid_dir.mkdir(mode=0o700, exist_ok=True)
    _assert_not_symlink(uid_dir)

    sess_dir = uid_dir / ("s" + str(anchor_pid))
    _assert_not_symlink(sess_dir)
    sess_dir.mkdir(mode=0o700, exist_ok=True)
    _assert_not_symlink(sess_dir)

    return sess_dir


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------

# The reaper script is written to session_dir/reaper.py and executed via
# os.execv so there are no quoting or escaping issues.
# It receives two arguments: the anchor PID and the session directory path.
# DESIGN: The build process of the monolith removes the import package line
#         if a multi-line string is been used. Prevent this, by joining all lines.
_REAPER_SCRIPT = "\n".join(
    [
        "import os, sys, time, shutil, subprocess, glob",
        "",
        "anchor_pid = int(sys.argv[1])",
        "sess_dir   = sys.argv[2]",
        "",
        "def _close_sockets():",
        "    for sock in glob.glob(os.path.join(sess_dir, 'ssh-*.sock')):",
        "        host_file = sock[:-5] + '.host'",
        "        host = ''",
        "        try:",
        "            with open(host_file) as f:",
        "                host = f.read().strip()",
        "        except OSError:",
        "            pass",
        "        if host:",
        "            try:",
        "                subprocess.run(",
        "                    ['ssh', '-O', 'exit', '-o', 'ControlPath=' + sock, host],",
        "                    stdout=subprocess.DEVNULL,",
        "                    stderr=subprocess.DEVNULL,",
        "                    timeout=5,",
        "                )",
        "            except Exception:",
        "                pass",
        "",
        "while True:",
        "    time.sleep(2)",
        "    try:",
        "        os.kill(anchor_pid, 0)",
        "    except (ProcessLookupError, PermissionError, OSError):",
        "        _close_sockets()",
        "        shutil.rmtree(sess_dir, ignore_errors=True)",
        "        break",
        "",
    ]
)


def ensure_reaper(sess_dir: Path) -> None:
    """Spawn the reaper for sess_dir if not already running.

    The reaper PID is stored in sess_dir/reaper.pid.  If the file exists
    and the PID is alive, this is a no-op (one stat + one kill check).

    The reaper script is written to sess_dir/reaper.py once and then
    executed by a double-forked detached python3 process.
    """
    pid_file = sess_dir / "reaper.pid"

    # Fast path: reaper already running.
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            os.kill(existing_pid, 0)
            return
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # stale -- fall through and spawn a new one

    anchor_pid = get_anchor_pid()
    reaper_script = sess_dir / "reaper.py"

    # Write the reaper script if not already present.
    if not reaper_script.exists():
        reaper_script.write_text(_REAPER_SCRIPT)
        reaper_script.chmod(0o700)

    # Double-fork: detach from the current process group so the reaper
    # is not killed when fzf's subprocess group is torn down.
    try:
        child_pid = os.fork()
    except OSError:
        return  # fork failed -- non-fatal, session dir will be GC'd later

    if child_pid != 0:
        # Parent: wait for the first child to exit (it exits immediately).
        try:
            os.waitpid(child_pid, 0)
        except ChildProcessError:
            pass
        return

    # --- First child ---
    try:
        os.setsid()
    except OSError:
        pass

    try:
        grandchild_pid = os.fork()
    except OSError:
        os._exit(0)

    if grandchild_pid != 0:
        # First child exits immediately so init(1) adopts the grandchild.
        os._exit(0)

    # --- Grandchild (the actual reaper) ---
    # Redirect stdio away from the terminal.
    try:
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull_fd, fd)
            except OSError:
                pass
        if devnull_fd > 2:
            os.close(devnull_fd)
    except OSError:
        pass

    # Write our PID so ensure_reaper() can detect us on future calls.
    try:
        pid_file.write_text(str(os.getpid()))
    except OSError:
        pass

    # Replace this process with the reaper script.
    try:
        os.execv(
            sys.executable,
            [sys.executable, str(reaper_script), str(anchor_pid), str(sess_dir)],
        )
    except OSError:
        pass

    os._exit(1)


# ---------------------------------------------------------------------------
# Stale session GC
# ---------------------------------------------------------------------------


def gc_stale_sessions() -> None:
    """Remove session directories whose anchor process no longer exists.

    Called by 'remotely gc' and can be called opportunistically at startup.
    """
    uid = os.getuid()
    uid_dir = WORK_BASE / ("u" + str(uid))
    if not uid_dir.is_dir():
        return
    try:
        entries = list(uid_dir.iterdir())
    except OSError:
        return
    for sess_dir in entries:
        if not sess_dir.is_dir() or not sess_dir.name.startswith("s"):
            continue
        try:
            anchor_pid = int(sess_dir.name[1:])
            os.kill(anchor_pid, 0)
            # Process still alive -- leave it alone.
        except (ValueError, ProcessLookupError, PermissionError):
            # Process is gone -- close sockets and remove.
            for sock in sess_dir.glob("ssh-*.sock"):
                host_file = Path(str(sock)[:-5] + ".host")
                host = ""
                try:
                    host = host_file.read_text().strip()
                except OSError:
                    pass
                if host:
                    try:
                        subprocess.run(
                            [
                                "ssh",
                                "-O",
                                "exit",
                                "-o",
                                "ControlPath=" + str(sock),
                                host,
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                        )
                    except subprocess.TimeoutExpired:
                        pass
            shutil.rmtree(str(sess_dir), ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# SSH socket management
# ---------------------------------------------------------------------------


def _host_hash(host: str) -> str:
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def _socket_path(sess_dir: Path, host: str) -> Path:
    return sess_dir / ("ssh-" + _host_hash(host) + ".sock")


def _lock_path(sess_dir: Path, host: str) -> Path:
    return sess_dir / ("ssh-" + _host_hash(host) + ".lock")


def _known_hosts_path(sess_dir: Path, host: str) -> Path:
    return sess_dir / ("ssh-" + _host_hash(host) + ".hosts")


def _host_file_path(sess_dir: Path, host: str) -> Path:
    """Stores the hostname; read by the reaper to issue ssh -O exit."""
    return sess_dir / ("ssh-" + _host_hash(host) + ".host")


def _socket_alive(sock: Path, host: str) -> bool:
    """Return True if the ControlMaster socket is present and responsive.

    ssh -O check only probes the local socket file -- no TCP connection is
    opened -- so ConnectTimeout is irrelevant here. A Python-side timeout=
    guards against a hung ssh process in pathological cases (e.g. the master
    process is in an uninterruptible sleep).
    """
    if not sock.exists():
        return False
    try:
        r = subprocess.run(
            ["ssh", "-O", "check", "-o", "ControlPath=" + str(sock), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SSH_CHECK_TIMEOUT,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _close_stale_master(sock: Path, host: str) -> None:
    """Attempt a graceful ssh -O exit before unlinking a dead socket.

    If the ControlMaster process is still alive but not responding to
    check, sending exit gives it a chance to shut down cleanly. Errors
    and timeouts are ignored -- we unlink the socket regardless.
    """
    try:
        subprocess.run(
            ["ssh", "-O", "exit", "-o", "ControlPath=" + str(sock), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SSH_CHECK_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        sock.unlink()
    except (FileNotFoundError, OSError):
        pass


def _start_master(sess_dir: Path, host: str) -> bool:
    """Start an SSH ControlMaster for host in managed mode."""
    sock = _socket_path(sess_dir, host)
    persist = int(CONFIG.get("ssh_control_persist", 60))

    known_hosts = _known_hosts_path(sess_dir, host)
    known_hosts.touch(mode=0o600, exist_ok=True)

    # Record hostname so the reaper can close the socket.
    _host_file_path(sess_dir, host).write_text(host)

    opts = [
        "-N",
        "-M",
        "-f",
        "-o",
        "ControlMaster=yes",
        "-o",
        "ControlPath=" + str(sock),
        "-o",
        "ControlPersist=" + str(persist),
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(known_hosts),
    ]

    try:
        r = subprocess.run(
            ["ssh"] + opts + [host],
            timeout=_SSH_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(
            "remotely: SSH connection to " + host + " timed out",
            file=sys.stderr,
        )
        return False

    if r.returncode != 0:
        print(
            "remotely: SSH connection to "
            + host
            + " failed (rc="
            + str(r.returncode)
            + ")",
            file=sys.stderr,
        )
        return False

    deadline = time.monotonic() + _SOCKET_READY_TIMEOUT
    while time.monotonic() < deadline:
        if sock.exists():
            return True
        time.sleep(0.05)

    print(
        "remotely: timed out waiting for socket after connecting to " + host,
        file=sys.stderr,
    )
    return False


def acquire_socket(host: str) -> str:
    """Return a ControlMaster socket path for host, or SSH_DEFERRED.

    DEFERRED MODE (ssh_multiplexing = false, the default):
        Returns SSH_DEFERRED. ~/.ssh/config handles multiplexing.

    MANAGED MODE (ssh_multiplexing = true):
        Creates or reuses a per-session ControlMaster socket.
        Uses flock so parallel preview callbacks wait rather than race.
        Detects stale sockets (host rebooted, network change) and
        recreates the connection automatically.
    """
    if not CONFIG.get("ssh_multiplexing", False):
        return SSH_DEFERRED

    sess_dir = get_session_dir()
    ensure_reaper(sess_dir)

    lock = _lock_path(sess_dir, host)
    with open(str(lock), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)

        sock = _socket_path(sess_dir, host)
        if _socket_alive(sock, host):
            return str(sock)

        # Socket is dead or missing. Close the stale master gracefully
        # before recreating so two masters don't conflict.
        _close_stale_master(sock, host)

        if not _start_master(sess_dir, host):
            return SSH_DEFERRED

        return str(sock)


def release_session(host: str) -> None:
    """Close the managed ControlMaster for host. No-op in deferred mode."""
    if not CONFIG.get("ssh_multiplexing", False):
        return
    try:
        sess_dir = get_session_dir()
        sock = _socket_path(sess_dir, host)
        if sock.exists():
            try:
                subprocess.run(
                    ["ssh", "-O", "exit", "-o", "ControlPath=" + str(sock), host],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_SSH_CHECK_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                pass
    except OSError:
        pass


def ssh_opts_for(host: str) -> List[str]:
    """Return SSH option flags for the current multiplexing mode."""
    if not CONFIG.get("ssh_multiplexing", False):
        return []

    sess_dir = get_session_dir()
    sock = _socket_path(sess_dir, host)
    persist = int(CONFIG.get("ssh_control_persist", 60))
    known_hosts = _known_hosts_path(sess_dir, host)
    known_hosts.touch(mode=0o600, exist_ok=True)

    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath=" + str(sock),
        "-o",
        "ControlPersist=" + str(persist),
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(known_hosts),
    ]


# ---------------------------------------------------------------------------
# Legacy alias
# ---------------------------------------------------------------------------


def session_dir(host: str) -> Path:
    """Deprecated: use get_session_dir() instead."""
    return get_session_dir()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_session_dir(argv: List[str]) -> int:
    """Entry point for the remotely session-dir sub-command.

    Prints the path to the current session directory to stdout and exits.
    Used by wrappers to store session-scoped state (e.g. search mode).
    """
    try:
        print(get_session_dir())
        return 0
    except OSError as e:
        print(f"remotely session-dir: {e}", file=sys.stderr)
        return 1
