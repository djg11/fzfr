"""fzfr.copy -- fzfr-copy sub-command: copy a selected file path to the clipboard.

Also provides _resolve_remote_path() for expanding tilde and relative paths
on a remote host without touching the local filesystem.
"""

import subprocess
import sys
from pathlib import Path

from .backends import LocalBackend
from .config import AVAILABLE_TOOLS
from .ssh import _ssh_opts


def cmd_copy(argv: list[str]) -> int:
    """Entry point for the fzfr-copy sub-command.

    Copies the selected file path to the local clipboard.

    Usage: fzfr-copy <target> <base_path> <remote> <ssh_control> <choice>
    """
    if len(argv) < 5:
        print(
            "Usage: fzfr-copy <target> <base_path> <remote> <ssh_control> <choice>",
            file=sys.stderr,
        )
        return 1

    _, base_path, remote, ssh_control, choice = argv[:5]
    remote = remote.strip("'\"")
    ssh_control = ssh_control.strip("'\"")

    choice_clean = choice.removeprefix("./")
    path_to_copy = (
        choice_clean
        if Path(choice_clean).is_absolute()
        else str(Path(base_path) / choice_clean)
    )

    if not remote:
        backend = LocalBackend(base_path, ssh_control)
        if not backend.is_safe_subpath(path_to_copy):
            print(
                f"Error: Blocked path outside search root: {path_to_copy}",
                file=sys.stderr,
            )
            return 1

    if "xclip" in AVAILABLE_TOOLS:
        copy_cmd = ["xclip", "-selection", "clipboard"]
    elif "pbcopy" in AVAILABLE_TOOLS:
        copy_cmd = ["pbcopy"]
    elif "wl-copy" in AVAILABLE_TOOLS:
        copy_cmd = ["wl-copy"]
    else:
        print(
            "Error: No clipboard tool found (xclip, pbcopy, or wl-copy needed).",
            file=sys.stderr,
        )
        return 1

    try:
        subprocess.run(copy_cmd, input=path_to_copy.encode("utf-8"), check=True)
        print(f"Copied: {path_to_copy}")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"Error copying to clipboard: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("Error: Clipboard tool not found.", file=sys.stderr)
        return 1


def _resolve_remote_path(remote: str, raw: str, ssh_control: str) -> str:
    """Expand a remote path to its absolute form by querying the remote host.

    Handles three cases that cannot be resolved locally:
      - Empty or ".": ask the remote shell for its cwd via pwd.
      - "~" or "~/...": expand via python3 -c on the remote (no shell injection).
      - Anything else: return as-is.

    SECURITY: Tilde expansion uses python3 stdin rather than shell expansion
    to avoid injection from a crafted remote path.

    DESIGN: Both SSH branches sys.exit() on failure. Without this, a network
    or auth failure would return an empty string and fzf would silently search
    causing fzfr to silently search from the remote filesystem root (/).
    """
    if not raw or raw == ".":
        r = subprocess.run(
            ["ssh"] + _ssh_opts(ssh_control) + [remote, "pwd"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(
                f"Error: SSH failed to resolve path for {remote} (rc={r.returncode})",
                file=sys.stderr,
            )
            sys.exit(1)
        return r.stdout.strip()

    if raw == "~" or raw.startswith("~"):
        r = subprocess.run(
            ["ssh"]
            + _ssh_opts(ssh_control)
            + [
                remote,
                "python3 -c 'import os,sys; print(os.path.expanduser(sys.stdin.read().strip()))'",
            ],
            input=raw.encode("utf-8"),
            capture_output=True,
        )
        if r.returncode != 0:
            print(
                f"Error: SSH failed to expand tilde for {remote} (rc={r.returncode})",
                file=sys.stderr,
            )
            sys.exit(1)
        return r.stdout.decode("utf-8", errors="replace").strip()

    return raw
