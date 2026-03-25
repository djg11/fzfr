"""remotely.open_cmd -- remotely open headless sub-command.

Opens a file in $EDITOR and exits. No TUI, no state file, no fzf dependency.

For remote files: streams the file to a local temp path in /dev/shm, launches
$EDITOR, watches for modification, and syncs back on save.

For local files: opens directly in $EDITOR.

Usage:
    remotely open [TARGET:]PATH

    TARGET:PATH uses the same format as remotely list output:
        /absolute/local/path          -- local file, no prefix
        ~/relative/path               -- local file, no prefix
        user@host:/remote/path        -- remote file, host prefix
        user@host:~/remote/path       -- remote file, tilde path

Examples:
    remotely open /etc/hosts
    remotely open user@host:/etc/nginx/nginx.conf
    remotely open user@host:~/projects/main.py

    # Round-trip with remotely list:
    remotely list user@host:/etc | fzf | xargs remotely open
"""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import AVAILABLE_TOOLS, CONFIG
from .preview_cmd import _parse_target_path
from .session import SSH_DEFERRED, acquire_socket
from .utils import _is_text_mime, _resolve_remote_path
from .workbase import WORK_BASE


# ---------------------------------------------------------------------------
# Editor resolution
# ---------------------------------------------------------------------------


def _find_editor() -> str:
    """Return the editor to use.

    Priority: config > $EDITOR > nvim > vim > vi.
    """
    if CONFIG.get("editor"):
        return CONFIG["editor"]
    editor = os.environ.get("EDITOR", "")
    if editor:
        return editor
    for candidate in ("nvim", "vim", "vi"):
        if candidate in AVAILABLE_TOOLS:
            return candidate
    return "vi"


# ---------------------------------------------------------------------------
# Open helpers
# ---------------------------------------------------------------------------


def _open_local(path: str, editor: str) -> int:
    """Open a local file in the editor."""
    if not Path(path).exists():
        print(f"remotely open: file not found: {path}", file=sys.stderr)
        return 1
    return subprocess.run([editor] + editor.split()[1:] + [path]).returncode


def _open_remote(host: str, path: str, editor: str) -> int:
    """Stream a remote file locally, open in editor, sync back on save.

    Workflow:
      1. Acquire session socket for host.
      2. Stream remote file into a local temp file in /dev/shm.
      3. Record mtime before launching editor.
      4. Launch editor and wait.
      5. If mtime changed, scp the temp file back to the remote path.
      6. Remove the temp file.

    SECURITY: mkstemp creates the file with mode 0o600 (O_CREAT|O_EXCL).
    The temp file lives in WORK_BASE which is 0o700.
    """
    sock = acquire_socket(host)
    # SSH_DEFERRED means ~/.ssh/config handles multiplexing.
    if sock and sock is not SSH_DEFERRED:
        ssh_opts = [
            "-o",
            "ControlMaster=no",
            "-o",
            f"ControlPath={sock}",
            "-o",
            "ConnectTimeout=5",
        ]
    else:
        ssh_opts = []

    # Check MIME type to refuse binary files early.
    mime_result = subprocess.run(
        ["ssh"]
        + ssh_opts
        + [host, f"file -L --mime-type -b {shlex.quote(path)} 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    mime = mime_result.stdout.strip() if mime_result.returncode == 0 else ""

    if mime and not _is_text_mime(mime):
        print(
            f"remotely open: {path} appears to be a binary file ({mime}).\n"
            "Use remotely open only for text files.",
            file=sys.stderr,
        )
        return 1

    # Stream file to a local temp file.
    suffix = Path(path).suffix or ""
    fd, tmp_path = tempfile.mkstemp(
        prefix="remotely-open-", suffix=suffix, dir=str(WORK_BASE)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            r = subprocess.run(
                ["ssh"] + ssh_opts + [host, f"cat {shlex.quote(path)}"],
                stdout=fh,
            )
        if r.returncode != 0:
            print(f"remotely open: could not read {host}:{path}", file=sys.stderr)
            return 1

        mtime_before = os.stat(tmp_path).st_mtime

        editor_parts = editor.split()
        rc = subprocess.run(editor_parts + [tmp_path]).returncode

        # Sync back if the file was modified.
        mtime_after = os.stat(tmp_path).st_mtime
        if mtime_after != mtime_before:
            scp_opts = (
                ["-o", f"ControlPath={sock}"]
                if sock and sock is not SSH_DEFERRED
                else []
            )
            sync = subprocess.run(["scp"] + scp_opts + [tmp_path, f"{host}:{path}"])
            if sync.returncode != 0:
                print(
                    f"remotely open: warning: could not sync {tmp_path} back to "
                    f"{host}:{path}",
                    file=sys.stderr,
                )

        return rc

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_open_headless(argv: list) -> int:
    """Entry point for the remotely open headless sub-command.

    Routes to local or remote open based on whether the path argument
    carries a host: prefix.
    """
    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__, file=sys.stderr)
        return 0

    host, path = _parse_target_path(argv[0])
    editor = _find_editor()

    if not host:
        return _open_local(path, editor)

    sock = acquire_socket(host)
    ssh_control = sock if sock is not SSH_DEFERRED else ""

    # Resolve ~ to an absolute path on the remote before use.
    if path.startswith("~"):
        path = _resolve_remote_path(host, path, ssh_control)
        if not path:
            print(f"remotely open: could not resolve path on {host}", file=sys.stderr)
            return 1

    return _open_remote(host, path, editor)
