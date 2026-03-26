"""remotely.open_cmd -- remotely open headless sub-command.

Opens a file in $EDITOR (text) or the system file opener (binary) and exits.
remotely open ALWAYS returns immediately -- it never blocks waiting for an
editor to close.

For remote TEXT files:
    Streams the file to a local temp file in the session directory, then
    opens the editor in a NEW TERMINAL WINDOW so the calling terminal is
    never blocked. A sync-back command runs inside that window after the
    editor exits, pushing changes back to the remote host via scp.

For remote BINARY files (PDF, images, etc.):
    Streams the file to <session_dir>/stream/<hash><ext> once and launches
    xdg-open (Linux) or open (macOS) detached.

For local files:
    Opens the editor in a new terminal window detached from the caller.

New window strategy (tried in order):
    1. tmux        -- $TMUX set and tmux in PATH -> tmux new-window
    2. kitty       -- $KITTY_LISTEN_ON set      -> kitty @ launch
    3. wezterm     -- $WEZTERM_UNIX_SOCKET set  -> wezterm cli spawn
    4. Terminal emulators in PATH (alacritty, foot, kitty, xterm, ...)
    5. No window available                      -> error with instructions

OOM guard:
    Before streaming, queries the remote file size via stat. Refuses files
    larger than max_stream_mb (config, default 100 MB) or files that would
    leave less than 64 MB free in WORK_BASE.

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
    remotely open user@host:/home/user/report.pdf
"""

import hashlib
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from .config import AVAILABLE_TOOLS, CONFIG
from .preview_cmd import _parse_target_path
from .session import SSH_DEFERRED, acquire_socket, ensure_reaper, get_session_dir
from .ssh import _ssh_opts
from .utils import _is_text_mime, _resolve_remote_path
from .workbase import WORK_BASE


# ---------------------------------------------------------------------------
# Editor resolution
# ---------------------------------------------------------------------------


def _find_editor() -> str:
    """Return the text editor to use.

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
# System file opener (binary files)
# ---------------------------------------------------------------------------


def _xdg_open_detached(path: str) -> None:
    """Open path with the platform file opener, detached from this process."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(
            [opener, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(
            "remotely open: no file opener found (" + opener + "). "
            "Open manually: " + path,
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# New-window launcher
# ---------------------------------------------------------------------------


def _in_tmux() -> bool:
    """Return True when running inside a tmux session with tmux in PATH."""
    return bool(os.environ.get("TMUX")) and shutil.which("tmux") is not None


def _in_kitty() -> bool:
    """Return True when running inside kitty with a control socket available."""
    return bool(os.environ.get("KITTY_LISTEN_ON")) and shutil.which("kitty") is not None


def _in_wezterm() -> bool:
    """Return True when running inside wezterm with a unix socket available."""
    return (
        bool(os.environ.get("WEZTERM_UNIX_SOCKET"))
        and shutil.which("wezterm") is not None
    )


def _find_gui_terminal() -> Optional[str]:
    """Return the name of an available GUI terminal emulator, or None."""
    for t in (
        "alacritty",
        "foot",
        "kitty",
        "wezterm",
        "xterm",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "tilix",
        "urxvt",
        "rxvt",
        "st",
    ):
        if shutil.which(t):
            return t
    return None


def _launch_in_new_window(shell_cmd: str, window_name: str) -> int:
    """Launch shell_cmd in a new terminal window and return immediately.

    shell_cmd is passed verbatim to 'sh -c'. It may chain a sync-back
    command with ';' so it runs after the editor exits inside the new window.

    Returns 0 if a window was successfully launched, 1 if no terminal is
    available.
    """
    # -- tmux: best option, works in headless/SSH environments --
    if _in_tmux():
        r = subprocess.run(
            ["tmux", "new-window", "-n", window_name, shell_cmd],
        )
        return r.returncode

    # -- kitty: socket API opens a new window/tab --
    if _in_kitty():
        listen = os.environ.get("KITTY_LISTEN_ON", "")
        r = subprocess.run(
            [
                "kitty",
                "@",
                "--to",
                listen,
                "launch",
                "--type=window",
                "--title",
                window_name,
                "sh",
                "-c",
                shell_cmd,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            return 0
        # Fall through to GUI terminal.

    # -- wezterm: CLI spawns a new pane --
    if _in_wezterm():
        r = subprocess.run(
            ["wezterm", "cli", "spawn", "--", "sh", "-c", shell_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            return 0
        # Fall through to GUI terminal.

    # -- GUI terminal emulators (require a display) --
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if has_display:
        term = _find_gui_terminal()
        if term:
            if term in ("gnome-terminal", "tilix"):
                cmd = [term, "--", "sh", "-c", shell_cmd]
            elif term in ("konsole", "xfce4-terminal"):
                cmd = [term, "-e", "sh", "-c", shell_cmd]
            else:
                cmd = [term, "-e", "sh", "-c", shell_cmd]
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return 0
            except (FileNotFoundError, OSError):
                pass

    # -- No usable terminal found --
    print(
        "remotely open: cannot open a new terminal window.\n"
        "  Tip: run inside tmux for best results:\n"
        "    tmux new-session\n"
        "  Then retry: remotely open ...",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# MIME detection
# ---------------------------------------------------------------------------


def _local_mime(path: str) -> str:
    """Return MIME type for a local file, or empty string on failure."""
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime
    try:
        r = subprocess.run(
            ["file", "-L", "--mime-type", "-b", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def _remote_mime(host: str, path: str, ssh_opts: List[str]) -> str:
    """Return MIME type for a remote file, or empty string on failure."""
    r = subprocess.run(
        ["ssh"]
        + ssh_opts
        + [host, "file -L --mime-type -b " + shlex.quote(path) + " 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


# ---------------------------------------------------------------------------
# OOM guard
# ---------------------------------------------------------------------------


def _remote_file_size(host: str, path: str, ssh_opts: List[str]) -> Optional[int]:
    """Return the size in bytes of a remote file, or None on failure."""
    r = subprocess.run(
        ["ssh"] + ssh_opts + [host, "stat -c %s " + shlex.quote(path) + " 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if r.returncode == 0:
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    # macOS / BSD fallback
    r = subprocess.run(
        ["ssh"] + ssh_opts + [host, "stat -f %z " + shlex.quote(path) + " 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if r.returncode == 0:
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    return None


def _remote_file_mtime(host: str, path: str, ssh_opts: List[str]) -> Optional[str]:
    """Return the mtime (seconds since epoch) of a remote file, or None."""
    r = subprocess.run(
        ["ssh"] + ssh_opts + [host, "stat -c %Y " + shlex.quote(path) + " 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # macOS / BSD fallback
    r = subprocess.run(
        ["ssh"] + ssh_opts + [host, "stat -f %m " + shlex.quote(path) + " 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _shm_free_bytes() -> int:
    """Return free bytes in the WORK_BASE filesystem."""
    try:
        sv = os.statvfs(str(WORK_BASE))
        return sv.f_bavail * sv.f_frsize
    except OSError:
        return 0


_OOM_HEADROOM = 64 * 1024 * 1024  # refuse stream if < 64 MB would remain


def _check_oom(remote_size: int) -> Optional[str]:
    """Return an error string if streaming remote_size bytes would be unsafe."""
    max_mb = CONFIG.get("max_stream_mb", 100)
    if max_mb > 0 and remote_size > max_mb * 1024 * 1024:
        return (
            "remote file is " + str(remote_size // 1024 // 1024) + " MB "
            "(limit " + str(max_mb) + " MB, set max_stream_mb=0 to disable)"
        )
    free = _shm_free_bytes()
    if free > 0 and remote_size + _OOM_HEADROOM > free:
        return (
            "not enough free space in "
            + str(WORK_BASE)
            + " ("
            + str(free // 1024 // 1024)
            + " MB free, need "
            + str((remote_size + _OOM_HEADROOM) // 1024 // 1024)
            + " MB)"
        )
    return None


# ---------------------------------------------------------------------------
# Stream-cache helpers (binary files)
# ---------------------------------------------------------------------------


def _stream_cache_path(sess_dir: Path, host: str, remote_path: str) -> Path:
    """Return a stable cache path for a streamed binary file."""
    h = hashlib.blake2b((host + ":" + remote_path).encode(), digest_size=16).hexdigest()
    stream_dir = sess_dir / "stream"
    stream_dir.mkdir(mode=0o700, exist_ok=True)
    suffix = Path(remote_path).suffix or ""
    return stream_dir / (h + suffix)


def _stream_mtime_path(cached_file: Path) -> Path:
    """Sidecar file storing the remote mtime at time of last stream."""
    return cached_file.parent / (cached_file.name + ".mtime")


def _get_cached_mtime(cached_file: Path) -> Optional[str]:
    try:
        return _stream_mtime_path(cached_file).read_text().strip()
    except OSError:
        return None


def _set_cached_mtime(cached_file: Path, mtime: str) -> None:
    try:
        _stream_mtime_path(cached_file).write_text(mtime)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# scp helper
# ---------------------------------------------------------------------------


def _scp_opts_from_ssh_opts(ssh_opts: List[str]) -> List[str]:
    """Extract a ControlPath option for scp from an ssh option list."""
    for opt in ssh_opts:
        if opt.startswith("ControlPath="):
            return ["-o", opt]
    return []


# ---------------------------------------------------------------------------
# Core open helpers
# ---------------------------------------------------------------------------


def _open_local(path: str, editor: str) -> int:
    """Open a local file in a new terminal window. Never blocks the caller."""
    if not Path(path).exists():
        print("remotely open: file not found: " + path, file=sys.stderr)
        return 1

    mime = _local_mime(path)
    if mime and not _is_text_mime(mime):
        _xdg_open_detached(path)
        return 0

    editor_cmd = " ".join(shlex.quote(p) for p in editor.split())
    shell_cmd = editor_cmd + " " + shlex.quote(path)
    window_name = Path(path).name or "remotely"
    return _launch_in_new_window(shell_cmd, window_name)


def _open_remote(host: str, path: str, editor: str, ssh_opts: List[str]) -> int:
    """Open a remote file: stream locally, edit in new window, sync back on close."""
    mime = _remote_mime(host, path, ssh_opts)
    is_text = not mime or _is_text_mime(mime)

    if is_text:
        return _stream_and_edit(host, path, editor, ssh_opts)
    return _stream_and_open_binary(host, path, ssh_opts, mime)


def _stream_and_edit(host: str, path: str, editor: str, ssh_opts: List[str]) -> int:
    """Stream remote file locally, open in new window, scp back when editor closes.

    DESIGN: The sync-back command is embedded in the shell command that runs
    inside the new terminal window:

        editor /tmp/remotely-open-xxx.py ; scp /tmp/... host:/remote/path ; rm -f /tmp/...

    This approach:
    - Requires no background watcher process or PID tracking.
    - Works identically across tmux, kitty, wezterm, and GUI terminals.
    - Syncs back exactly once, immediately after the editor exits.
    - Cleans up the temp file inside the window after the sync.
    - remotely open itself exits as soon as the new window is launched.

    SECURITY: All values are shlex.quote'd. host and path come from the
    user's own command line, not from remote input.
    """
    suffix = Path(path).suffix or ""
    fd, tmp_path = tempfile.mkstemp(
        prefix="remotely-open-", suffix=suffix, dir=str(WORK_BASE)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            r = subprocess.run(
                ["ssh"] + ssh_opts + [host, "cat " + shlex.quote(path)],
                stdout=fh,
            )
        if r.returncode != 0:
            print(
                "remotely open: could not read " + host + ":" + path,
                file=sys.stderr,
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return 1
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    scp_opts = _scp_opts_from_ssh_opts(ssh_opts)

    scp_cmd = " ".join(
        ["scp"]
        + [shlex.quote(o) for o in scp_opts]
        + [shlex.quote(tmp_path), shlex.quote(host + ":" + path)]
    )
    editor_cmd = " ".join(shlex.quote(p) for p in editor.split())
    rm_cmd = "rm -f " + shlex.quote(tmp_path)

    # Chain: edit -> sync back -> clean up temp file.
    shell_cmd = (
        editor_cmd + " " + shlex.quote(tmp_path) + " ; " + scp_cmd + " ; " + rm_cmd
    )

    window_name = Path(path).name or "remotely"
    rc = _launch_in_new_window(shell_cmd, window_name)
    if rc != 0:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return rc


def _stream_and_open_binary(
    host: str, path: str, ssh_opts: List[str], mime: str
) -> int:
    """Stream a remote binary to the session stream cache and xdg-open it.

    Reuses the cached copy if the remote mtime is unchanged.
    Applies OOM guard before streaming.
    """
    try:
        sess_dir = get_session_dir()
        ensure_reaper(sess_dir)
    except OSError as exc:
        print(
            "remotely open: could not create session dir: " + str(exc), file=sys.stderr
        )
        return 1

    cached = _stream_cache_path(sess_dir, host, path)
    current_mtime = _remote_file_mtime(host, path, ssh_opts)

    if cached.exists() and current_mtime is not None:
        if _get_cached_mtime(cached) == current_mtime:
            print(
                "remotely open: using cached stream for " + Path(path).name,
                file=sys.stderr,
            )
            _xdg_open_detached(str(cached))
            return 0

    remote_size = _remote_file_size(host, path, ssh_opts)
    if remote_size is not None:
        err = _check_oom(remote_size)
        if err:
            print("remotely open: " + err, file=sys.stderr)
            return 1

    print(
        "remotely open: streaming " + Path(path).name + " (" + mime + ") ...",
        file=sys.stderr,
    )

    stream_dir = cached.parent
    stream_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        fd, tmp_stream = tempfile.mkstemp(
            prefix=".remotely-stream-tmp-", dir=str(stream_dir)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                r = subprocess.run(
                    ["ssh"] + ssh_opts + [host, "cat " + shlex.quote(path)],
                    stdout=fh,
                )
            if r.returncode != 0:
                os.unlink(tmp_stream)
                print(
                    "remotely open: could not stream " + host + ":" + path,
                    file=sys.stderr,
                )
                return 1
            os.replace(tmp_stream, str(cached))
        except Exception:
            try:
                os.unlink(tmp_stream)
            except OSError:
                pass
            raise
    except OSError as exc:
        print("remotely open: stream failed: " + str(exc), file=sys.stderr)
        return 1

    if current_mtime:
        _set_cached_mtime(cached, current_mtime)

    _xdg_open_detached(str(cached))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_open_headless(argv: list) -> int:
    """Entry point for the remotely open headless sub-command."""
    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__, file=sys.stderr)
        return 0

    host, path = _parse_target_path(argv[0])
    editor = _find_editor()

    if not host:
        return _open_local(path, editor)

    sock = acquire_socket(host)
    ssh_control = sock if sock is not SSH_DEFERRED else ""

    if path.startswith("~"):
        path = _resolve_remote_path(host, path, ssh_control)
        if not path:
            print(
                "remotely open: could not resolve path on " + host,
                file=sys.stderr,
            )
            return 1

    ssh_opts = _ssh_opts(ssh_control)
    return _open_remote(host, path, editor, ssh_opts)
