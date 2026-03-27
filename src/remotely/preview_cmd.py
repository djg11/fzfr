"""remotely.preview_cmd -- remotely preview headless sub-command.

Renders a single file to stdout and exits. No TUI, no state file.

Usage:
    remotely preview [TARGET:]PATH [QUERY]

    TARGET:PATH is either:
        /absolute/local/path          -- local file, no prefix
        ~/relative/path               -- local file, no prefix
        user@host:/remote/path        -- remote file, host prefix
        user@host:~/remote/path       -- remote file, tilde path

    The host:/path format is exactly what remotely list emits, so the UI
    can pass the selected line from remotely list directly to remotely preview
    without any string manipulation:

        remotely list user@host:/var/log \
            | fzf --preview 'remotely preview {}'

    QUERY is an optional search string passed to the preview renderer for
    syntax-highlighted match context (rga / grep).

    Results are cached in <session_dir>/preview/ keyed on host, path, mtime,
    and query.  Navigating back to a previously previewed file replays from
    cache with no SSH round-trip or subprocess spawn.

Examples:
    remotely preview /etc/hosts
    remotely preview user@host:/var/log/app.log
    remotely preview user@host:/var/log/app.log "error"
    remotely preview user@host:~/projects/main.py
"""

import subprocess
import sys
from pathlib import Path

from .cache import (
    get,
    local_cache_key,
    local_mtime,
    put,
    remote_cache_key,
    remote_mtime,
)
from .preview import cmd_preview
from .remote import _cmd_remote_preview_capture
from .session import SSH_DEFERRED, acquire_socket, ensure_reaper, get_session_dir
from .ssh import _ssh_opts
from .utils import _resolve_remote_path


# ---------------------------------------------------------------------------
# host:/path parsing
# ---------------------------------------------------------------------------


def _parse_target_path(arg: str) -> "tuple[str, str]":
    """Split a TARGET:PATH argument into (host, path).

    Returns ("", arg) for local paths (no host prefix).
    Returns (host, path) for remote paths.

    Rules:
    - If arg starts with / or ~ or . it is always local.
    - Otherwise split on the first : if followed by / or ~ or .
    """
    if arg.startswith("/") or arg.startswith("~") or arg.startswith("."):
        return "", arg

    for i, ch in enumerate(arg):
        if ch == ":" and i > 0 and i + 1 < len(arg) and arg[i + 1] in ("/", "~", "."):
            return arg[:i], arg[i + 1 :]

    return "", arg


# ---------------------------------------------------------------------------
# Cached local preview
# ---------------------------------------------------------------------------


def _preview_local_cached(path: str, query: str) -> int:
    """Run the local preview renderer with a session-scoped cache check.

    Cache key: "local:<path>:<mtime_ns>:<query>"
    On hit: write cached bytes to stdout and return 0.
    On miss: capture subprocess output, store in cache, write to stdout.
    """
    mtime = local_mtime(path)
    if mtime is not None:
        key = local_cache_key(path, mtime, query)
        hit = get(key)
        if hit is not None:
            sys.stdout.buffer.write(hit)
            sys.stdout.buffer.flush()
            return 0

        # Cache miss -- capture so we can store the result.
        args = [path]
        if query:
            args.append(query)
        r = subprocess.run(
            [sys.executable, sys.argv[0], "remotely-preview"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sys.stdout.buffer.write(r.stdout)
        sys.stdout.buffer.flush()
        if r.returncode == 0 and r.stdout:
            put(key, r.stdout)
        return r.returncode

    # stat failed; let cmd_preview handle the error message directly.
    args = [path]
    if query:
        args.append(query)
    return cmd_preview(args)


# ---------------------------------------------------------------------------
# Cached remote preview
# ---------------------------------------------------------------------------


def _preview_remote_cached(host: str, path: str, query: str, ssh_control: str) -> int:
    """Run the remote preview with a session-scoped cache check.

    Cache key: "remote:<host>:<path>:<mtime_epoch>:<query>"
    On hit: write cached bytes to stdout and return 0.
    On miss: capture via _cmd_remote_preview_capture, store, write to stdout.
    """
    ssh_prefix = ["ssh"] + _ssh_opts(ssh_control) + [host]
    mtime = remote_mtime(ssh_prefix, path)

    if mtime is not None:
        key = remote_cache_key(host, path, mtime, query)
        hit = get(key)
        if hit is not None:
            sys.stdout.buffer.write(hit)
            sys.stdout.buffer.flush()
            return 0

    # Cache miss -- capture output so we can store it.
    base_path = str(Path(path).parent) if not path.endswith("/") else path
    args = [host, base_path, ssh_control, path]
    if query:
        args.append(query)

    rc, data = _cmd_remote_preview_capture(args)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    if rc == 0 and mtime is not None and data:
        key = remote_cache_key(host, path, mtime, query)
        put(key, data)
    return rc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_preview_headless(argv: list) -> int:
    """Entry point for the remotely preview sub-command."""
    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__, file=sys.stderr)
        return 0

    target_path = argv[0]
    query = argv[1] if len(argv) > 1 else ""

    host, path = _parse_target_path(target_path)

    # Ensure the session dir and reaper exist so the cache is available.
    try:
        sess_dir = get_session_dir()
        ensure_reaper(sess_dir)
    except OSError:
        pass

    if not host:
        return _preview_local_cached(path, query)

    sock = acquire_socket(host)
    ssh_control = sock if sock is not SSH_DEFERRED else ""

    if path.startswith("~"):
        path = _resolve_remote_path(host, path, ssh_control)
        if not path:
            print(
                "remotely preview: could not resolve path on " + host,
                file=sys.stderr,
            )
            return 1

    return _preview_remote_cached(host, path, query, ssh_control)
