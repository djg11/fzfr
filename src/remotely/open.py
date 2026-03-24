"""remotely.open -- remotely-open sub-command: open a selected file or directory.

Dispatch logic:
  - Directory          -> open in a new tmux window with remotely (recursive search)
  - Text file, local   -> open in $EDITOR in a new tmux window (or inline)
  - Binary file, local -> xdg-open (Linux) / open (macOS)
  - Text file, remote  -> ssh -t <host> <editor> <file>
  - Binary file, remote-> stream via SSH into the session directory, then
                         xdg-open (Linux) / open (macOS).

_dquote() is used (not shlex.quote) for remote editor paths because the
command travels through two shell levels: local tmux -> ssh -> remote shell.
Double-quoting is safe at both levels; single-quoting breaks at the first.
"""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .backends import LocalBackend, RemoteBackend
from .config import AVAILABLE_TOOLS, CONFIG, HISTORY_PATH
from .search import _self_cmd
from .ssh import _ssh_opts, _ssh_opts_str
from .state import _load_state
from .utils import _is_text_mime, _removeprefix, _resolve_absolute_path, _shlex_join
from .workbase import WORK_BASE


def _find_editor():
    # type: () -> str
    """Return the editor to use.

    Priority: config > $EDITOR > compiled-in fallback chain (nvim, vim, vi).
    Returns 'vi' as a last resort -- POSIX-required on every Unix system.
    """
    if CONFIG.get("editor"):
        return CONFIG["editor"]
    editor = os.environ.get("EDITOR", "")
    if editor:
        return editor
    for c in ("nvim", "vim", "vi"):
        if c in AVAILABLE_TOOLS:
            return c
    return "vi"


def _in_tmux():
    # type: () -> bool
    """Return True if running inside a tmux session ($TMUX is set)."""
    return bool(os.environ.get("TMUX"))


def _dquote(s):
    # type: (str) -> str
    """Wrap s in double quotes, escaping characters special inside them.

    DESIGN: shlex.quote() produces single-quoted strings which cannot be
            nested inside another single-quoted context. This breaks the
            tmux -> ssh -> remote shell chain. Double-quoted strings pass
            through that chain safely.
    """
    for ch in ("\\", '"', "$", "`", "!", "}"):
        s = s.replace(ch, "\\" + str(ch))
    return f'"{s}"'


def _xdg_open(path):
    # type: (str) -> None
    """Open path with the platform file opener and detach immediately.

    Uses xdg-open on Linux, open on macOS. Popen is used (not run) so
    the opener detaches and fzf remains interactive.
    """
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(
            [opener, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(
            f"Error: no file opener available ({opener!r} not found). "
            f"Open the file manually: {path}",
            file=sys.stderr,
        )


def _strip_quotes(s):
    # type: (str) -> str
    """Strip residual shell quotes from an argv token (e.g. literal '' tokens)."""
    return s.strip("'\"")


def _open(choice, editor, state, self_path, backend):
    # type: (str, str, dict, object, Union[LocalBackend, RemoteBackend]) -> None
    """Open a selected file or directory using the session backend."""
    choice_clean = _removeprefix(choice, "./")
    is_remote = isinstance(backend, RemoteBackend)
    full_path_str = _resolve_absolute_path(choice_clean, backend.base_path, is_remote)

    if not backend.is_safe_subpath(full_path_str):
        print(
            f"Error: Blocked path outside search root: {full_path_str}",
            file=sys.stderr,
        )
        return

    window_name = Path(choice_clean).name
    mode = state.get("mode", "content")
    safe_editor = shlex.quote(editor)

    # -- Directory --
    if backend.is_dir(full_path_str):
        if _in_tmux():
            if is_remote:
                cmd = f"{_self_cmd(self_path)} {shlex.quote(backend.remote)} {shlex.quote(full_path_str)} {shlex.quote(mode)}"
            else:
                cmd = f"{_self_cmd(self_path)} local {shlex.quote(full_path_str)} {shlex.quote(mode)}"
            subprocess.run(["tmux", "new-window", "-n", window_name, cmd])
            return

    mime = backend.get_mime(full_path_str)

    # -- Remote text --
    if is_remote and _is_text_mime(mime):
        ssh_cmd = f"{safe_editor} {_dquote(full_path_str)}"
        if _in_tmux():
            opts_str = _ssh_opts_str(backend.ssh_control)
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-n",
                    window_name,
                    f"ssh {opts_str} {shlex.quote(backend.remote)} -t {shlex.quote(ssh_cmd)}",
                ]
            )
        else:
            subprocess.run(
                ["ssh"]
                + _ssh_opts(backend.ssh_control)
                + [backend.remote, "-t", ssh_cmd]
            )
        return

    # -- Remote binary --
    if is_remote:
        _open_remote_binary(full_path_str, backend, self_path, window_name)
        return

    # -- Local text in tmux --
    if _in_tmux() and _is_text_mime(mime):
        subprocess.run(
            [
                "tmux",
                "new-window",
                "-n",
                window_name,
                f"{safe_editor} {shlex.quote(full_path_str)}",
            ]
        )
        return

    # -- Local binary / text without tmux --
    _xdg_open(full_path_str)


def _open_remote_binary(full_path_str, backend, self_path, window_name):
    # type: (str, RemoteBackend, object, str) -> None
    """Stream a remote binary file locally and open with xdg-open.

    SIZE GUARD: Refuses files over max_stream_mb (default 100 MB) since
    WORK_BASE is tmpfs (RAM-backed).

    SECURITY: mkstemp() creates the file with O_CREAT|O_EXCL (mode 0o600).
    The session dir (parent of self_path) is private (mode 0o700) and is
    removed entirely by _cleanup() on exit.
    """
    _max_mb = CONFIG.get("max_stream_mb", 100)
    _max_bytes = _max_mb * 1024 * 1024 if _max_mb > 0 else float("inf")

    size_result = subprocess.run(
        ["ssh"]
        + _ssh_opts(backend.ssh_control)
        + [backend.remote, _shlex_join(["ls", "-l", full_path_str])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if size_result.returncode == 0:
        try:
            remote_size = int(size_result.stdout.split()[4])
            if remote_size > _max_bytes:
                print(
                    "Error: remote file is too large to stream "
                    f"({remote_size // 1024 // 1024} MB > {_max_mb} MB limit). "
                    "Use ssh to access it directly.",
                    file=sys.stderr,
                )
                return
        except (ValueError, IndexError):
            pass  # unparseable -- proceed and let cat fail naturally

    session_dir = self_path.parent if self_path is not None else WORK_BASE
    suffix = Path(full_path_str).suffix or ""
    fd, temp_local = tempfile.mkstemp(
        prefix="remotely-open-", suffix=suffix, dir=session_dir
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            subprocess.run(
                ["ssh"]
                + _ssh_opts(backend.ssh_control)
                + [backend.remote, _shlex_join(["cat", full_path_str])],
                stdout=fh,
            )
    except Exception:
        os.unlink(temp_local)
        raise
    _xdg_open(temp_local)


def cmd_open(argv):
    # type: (list) -> int
    """Entry point for the remotely-open sub-command.

    Called by fzf when the user presses Enter.

    argv[0]  target      -- "local" or the ssh host string
    argv[1]  base_path   -- absolute base directory used by fd
    argv[2]  remote      -- ssh host (same as target for remote, "" for local)
    argv[3]  remote_dir  -- unused, kept for argv compatibility
    argv[4]  ssh_control -- ControlPath socket, or "" to use ~/.ssh/config
    argv[5]  state_path  -- session state file
    argv[6]  self_path   -- path to the script (original or frozen)
    argv[7]  query       -- current fzf query ({q}), written to history
    argv[8+] choices     -- selected file paths ({+} expands to all of them)
    """
    if len(argv) < 9:
        print(
            "Usage: remotely-open <target> <base_path> <remote> <remote_dir> "
            "<ssh_control> <state_path> <self_path> <query> <choice> [choice ...]",
            file=sys.stderr,
        )
        return 1

    _, base_path, remote, _, ssh_control, state_path, self_path_str = argv[:7]
    query = argv[7]
    choices = argv[8:]

    remote, ssh_control, state_path, self_path_str = (
        _strip_quotes(remote),
        _strip_quotes(ssh_control),
        _strip_quotes(state_path),
        _strip_quotes(self_path_str),
    )
    self_path = (
        Path(self_path_str) if self_path_str and self_path_str != "None" else None
    )

    if not choices:
        return 0

    if query and CONFIG.get("search_history", False):
        try:
            existing = (
                HISTORY_PATH.read_text().splitlines() if HISTORY_PATH.exists() else []
            )
            deduped = [query] + [e for e in existing if e != query]
            HISTORY_PATH.write_text("\n".join(deduped[:1000]) + "\n")
        except OSError:
            pass

    state = _load_state(Path(state_path))
    editor = _find_editor()
    be = (
        RemoteBackend(remote, base_path, ssh_control)
        if remote
        else LocalBackend(base_path, ssh_control)
    )  # type: Union[LocalBackend, RemoteBackend]
    for choice in choices:
        _open(choice, editor, state, self_path, be)
    return 0
