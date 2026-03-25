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

Examples:
    remotely preview /etc/hosts
    remotely preview user@host:/var/log/app.log
    remotely preview user@host:/var/log/app.log "error"
    remotely preview user@host:~/projects/main.py
"""

import sys
from pathlib import Path

from .preview import cmd_preview
from .remote import cmd_remote_preview
from .session import SSH_DEFERRED, acquire_socket
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
    - Otherwise split on the first : that is followed by / or ~.
      A bare hostname with no colon is treated as local.
    - user@host:/path  -> ("user@host", "/path")
    - user@host:~/p    -> ("user@host", "~/p")
    - /local/path      -> ("", "/local/path")
    """
    if arg.startswith("/") or arg.startswith("~") or arg.startswith("."):
        return "", arg

    for i, ch in enumerate(arg):
        if ch == ":" and i > 0 and i + 1 < len(arg) and arg[i + 1] in ("/", "~"):
            return arg[:i], arg[i + 1:]

    return "", arg


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_preview_headless(argv: list) -> int:
    """Entry point for the remotely preview sub-command.

    Routes to local preview or remote preview based on whether the path
    argument carries a host: prefix.
    """
    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__, file=sys.stderr)
        return 0

    target_path = argv[0]
    query = argv[1] if len(argv) > 1 else ""

    host, path = _parse_target_path(target_path)

    if not host:
        args = [path]
        if query:
            args.append(query)
        return cmd_preview(args)

    # Remote: ensure a session socket exists, then call cmd_remote_preview.
    sock = acquire_socket(host)
    ssh_control = sock if sock is not SSH_DEFERRED else ""

    if path.startswith("~"):
        path = _resolve_remote_path(host, path, ssh_control)
        if not path:
            print(
                f"remotely preview: could not resolve path on {host}", file=sys.stderr
            )
            return 1

    base_path = str(Path(path).parent) if not path.endswith("/") else path
    args = [host, base_path, ssh_control, path]
    if query:
        args.append(query)
    return cmd_remote_preview(args)
