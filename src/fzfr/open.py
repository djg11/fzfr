"""fzfr.open — fzfr-open sub-command: open a selected file or directory.

Dispatch logic:
  - Directory          → open in a new tmux window with fzfr (recursive search)
  - Text file, local   → open in $EDITOR in a new tmux window (or inline)
  - Binary file, local → xdg-open (Linux) / open (macOS)
  - Text file, remote  → ssh -t <host> <editor> <file>
  - Binary file, remote→ stream via SSH into the session directory, then
                         xdg-open (Linux) / open (macOS).
                         The session directory is on tmpfs (/dev/shm) and is
                         removed entirely by _cleanup() on exit. A background
                         sweeper thread in cmd_search also removes individual
                         fzfr-open-* files after 30 seconds.

_dquote() is used (not shlex.quote) for remote editor paths because the
command travels through two shell levels: local tmux → ssh → remote shell.
Double-quoting is safe at both levels; single-quoting breaks at the first.
"""
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import CONFIG, AVAILABLE_TOOLS, HISTORY_PATH
from .state import _load_state
from .backends import LocalBackend, RemoteBackend
from .ssh import _ssh_opts, _ssh_opts_str
from .utils import _is_text_mime
from .search import _self_cmd
from .workbase import WORK_BASE

def _find_editor() -> str:
    """Return the editor to use.

    Priority: config file > $EDITOR > compiled-in fallback chain (nvim, vim,
    vi). Returns 'vi' as a last resort — POSIX-required on every Unix system
    (Linux, macOS, BSD).

    DESIGN: nano is intentionally absent from the fallback chain. It is not
    universally present and adds no reliability guarantee beyond what vi
    already provides. Users who prefer nano should set $EDITOR or the
    'editor' config key.
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


def _in_tmux() -> bool:
    """Return True if we are running inside a tmux session.

    Checked by the presence of $TMUX, which tmux sets automatically. When True,
    text files are opened in a new tmux window instead of replacing the current
    terminal, so fzf remains visible and reusable.
    """
    return bool(os.environ.get("TMUX"))


def _dquote(s: str) -> str:
    """Wrap s in double quotes, escaping the characters special inside them:
    backslash, double-quote, dollar, backtick, !, and }.

    DESIGN: shlex.quote() produces single-quoted strings which cannot be
            nested inside another single-quoted context. This breaks the
            tmux → ssh → remote shell chain used for opening remote files,
            where the outer tmux command is already single-quoted. Double-
            quoted strings pass through that chain safely.

    Used in _open() for remote paths that must survive:
      local shell (tmux) → ssh transport → remote shell
    """
    for ch in ("\\", '"', "$", "`", "!", "}"):
        s = s.replace(ch, "\\" + str(ch))
    return f'"{s}"'


def _xdg_open(path: str) -> None:
    """Open path with the platform file opener and detach immediately.

    Uses xdg-open on Linux, open on macOS. Both are called via Popen so
    the opener detaches and fzf remains interactive — run() would block
    until the application closes.

    DESIGN: sys.platform == "darwin" is the canonical Python check for
    macOS. xdg-open is Linux-specific (part of xdg-utils); it does not
    exist on macOS or BSD. 'open' is a macOS built-in, always present.
    On BSD without a desktop environment neither tool is available — the
    Popen call will fail with FileNotFoundError which is caught and
    reported rather than crashing fzfr.
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


def _open(
    choice: str,
    editor: str,
    state: dict,
    self_path: Path | None,
    backend: LocalBackend | RemoteBackend,
) -> None:
    """Open a selected file or directory using the session backend.

    The backend encapsulates all local/remote divergence: safety checking,
    directory detection, MIME detection, and the actual open actions. This
    function contains no `if remote:` branches — it calls backend methods
    and interprets the results.

    Arguments:
        choice      — path as returned by fzf (may be relative, e.g. "./foo")
        editor      — resolved editor binary (from _find_editor())
        state       — current session state dict (used for mode on dir-open)
        self_path   — frozen script path for launching new fzfr windows
        backend     — LocalBackend or RemoteBackend for this session
    """
    # DESIGN: removeprefix("./") not lstrip("./"): lstrip treats its argument
    #         as a *set* of characters, so lstrip("./") on "..hidden" would
    #         incorrectly strip to "hidden". removeprefix matches the exact
    #         string "./", leaving dotfiles and dot-dot paths untouched.
    choice_clean = choice.removeprefix("./")

    # --- 1. Build the full path --------------------------------------------------
    # Path construction is identical for both backends: join relative paths
    # against base_path, leave absolute paths untouched.
    full_path_str = (
        choice_clean
        if Path(choice_clean).is_absolute()
        else str(Path(backend.base_path) / choice_clean)
    )

    # --- 2. Safety check ---------------------------------------------------------
    if not backend.is_safe_subpath(full_path_str):
        print(
            f"Error: Blocked path outside search root: {full_path_str}",
            file=sys.stderr,
        )
        return

    window_name = Path(choice_clean).name
    mode = state.get("mode", "content")
    is_remote = isinstance(backend, RemoteBackend)

    # --- 3. Directory handling ---------------------------------------------------
    if backend.is_dir(full_path_str):
        if _in_tmux():
            # DESIGN: Use the absolute script path so the new tmux window finds
            #         the script even if the user's PATH differs from the parent.
            if is_remote:
                cmd = f"{_self_cmd(self_path)} {shlex.quote(backend.remote)} {shlex.quote(full_path_str)} {shlex.quote(mode)}"
            else:
                cmd = f"{_self_cmd(self_path)} local {shlex.quote(full_path_str)} {shlex.quote(mode)}"
            subprocess.run(["tmux", "new-window", "-n", window_name, cmd])
            return
        # No tmux: fall through to xdg-open below.

    # --- 4. MIME detection -------------------------------------------------------
    mime = backend.get_mime(full_path_str)
    safe_editor = shlex.quote(editor)

    # --- 5. Open the file --------------------------------------------------------
    if is_remote and _is_text_mime(mime):
        # DESIGN: _dquote() is used here (not shlex.quote) because the path
        #         must survive two shell levels: tmux new-window evaluates its
        #         command string through a local shell, and ssh then passes it
        #         to the remote shell. A double-quoted path passes through both
        #         layers safely; a single-quoted path would break at the first.
        #         See _dquote() for the full explanation.
        safe_path_dq = _dquote(full_path_str)
        ssh_cmd = f"{safe_editor} {safe_path_dq}"
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

    elif is_remote:
        # Binary remote file: stream into the session directory then xdg-open.
        #
        # DESIGN: The session dir (parent of self_path) is private to this
        #         session (mode 0o700) and is removed entirely by _cleanup()
        #         on exit. No separate cleanup logic needed here.
        #
        # SIZE GUARD: Refuse files over the configured limit (max_stream_mb, default 100 MB).
        #             WORK_BASE is tmpfs (RAM-backed), so large files consume memory.
        #
        # SECURITY: mkstemp() creates the file with O_CREAT|O_EXCL (mode 0o600).
        _max_mb = CONFIG.get("max_stream_mb", 100)
        _MAX_OPEN_BYTES = _max_mb * 1024 * 1024 if _max_mb > 0 else float("inf")
        # Use `ls -l` for remote size — POSIX, works on Linux and macOS,
        # does not read file content. Output field 4 (0-indexed) is the size.
        _size_result = subprocess.run(
            ["ssh"] + _ssh_opts(backend.ssh_control)
            + [backend.remote, shlex.join(["ls", "-l", full_path_str])],
            capture_output=True, text=True,
        )
        if _size_result.returncode == 0:
            try:
                _remote_size = int(_size_result.stdout.split()[4])
                if _remote_size > _MAX_OPEN_BYTES:
                    print(
                        f"Error: remote file is too large to stream "
                        f"({_remote_size // 1024 // 1024} MB > {_max_mb} MB limit). "
                        f"Use ssh to access it directly.",
                        file=sys.stderr,
                    )
                    return
            except (ValueError, IndexError):
                pass  # unparseable — proceed and let cat fail naturally

        session_dir = self_path.parent if self_path is not None else WORK_BASE
        suffix = Path(full_path_str).suffix or ""
        fd, temp_local = tempfile.mkstemp(
            prefix="fzfr-open-", suffix=suffix, dir=session_dir
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                subprocess.run(
                    ["ssh"]
                    + _ssh_opts(backend.ssh_control)
                    + [backend.remote, shlex.join(["cat", full_path_str])],
                    stdout=fh,
                )
        except Exception:
            os.unlink(temp_local)
            raise
        _xdg_open(temp_local)

    elif _in_tmux() and _is_text_mime(mime):
        # Local text file in tmux: open in a new window.
        subprocess.run(
            [
                "tmux",
                "new-window",
                "-n",
                window_name,
                f"{safe_editor} {shlex.quote(full_path_str)}",
            ]
        )

    else:
        # Local file (binary, or text without tmux): hand off to the
        # platform file opener. _xdg_open() uses xdg-open on Linux and
        # 'open' on macOS, detaching immediately so fzf stays interactive.
        _xdg_open(full_path_str)


def cmd_open(argv: list[str]) -> int:
    """Entry point for the fzfr-open sub-command.

    Called by fzf when the user presses Enter. Receives the positional
    arguments that build_fzf_invocation() embeds in the --bind string:

        argv[0]  target      — "local" or the ssh host string
        argv[1]  base_path   — absolute base directory used by fd
        argv[2]  remote      — ssh host (same as target for remote, "" for local)
        argv[3]  remote_dir  — unused, kept for argv compatibility with existing fzf bind strings
        argv[4]  ssh_control — ControlPath socket, or "" to use ~/.ssh/config
        argv[5]  state_path  — session state file
        argv[6]  self_path   — path to the script (original or frozen)
        argv[7]  query       — current fzf query ({q}), written to history
        argv[8+] choices     — selected file paths ({+} expands to all of them)

    remote and ssh_control arrive as empty strings for local mode. We strip
    residual shell quotes defensively in case the caller passed literal '' tokens.
    """
    if len(argv) < 9:
        print(
            "Usage: fzfr-open <target> <base_path> <remote> <remote_dir> "
            "<ssh_control> <state_path> <self_path> <query> <choice> [choice ...]",
            file=sys.stderr,
        )
        return 1

    _, base_path, remote, _, ssh_control, state_path, self_path_str = (
        argv[:7]
    )
    query = argv[7]
    choices = argv[8:]
    remote = remote.strip("'\"")
    ssh_control = ssh_control.strip("'\"")
    state_path = state_path.strip("'\"")
    self_path_str = self_path_str.strip("'\"")
    self_path = (
        Path(self_path_str) if self_path_str and self_path_str != "None" else None
    )

    if not choices:
        return 0

    # Write the query to the history file on every Enter press.
    # fzf only writes --history on normal exit (ESC), so queries that lead
    # to an open-and-continue workflow would never be saved without this.
    # DESIGN: Matches fzf's own history write behaviour: the new entry is
    #         prepended and any existing duplicate is removed, so the most
    #         recent query always appears first when navigating with ctrl-p.
    #         The file is capped at --history-size (default 1000) entries.
    if query and CONFIG.get("search_history", False):
        try:
            existing = (
                HISTORY_PATH.read_text().splitlines() if HISTORY_PATH.exists() else []
            )
            deduped = [query] + [e for e in existing if e != query]
            max_entries = 1000  # matches fzf's --history-size default
            HISTORY_PATH.write_text("\n".join(deduped[:max_entries]) + "\n")
        except OSError:
            pass  # non-fatal: history write failure never blocks the open

    state = _load_state(Path(state_path))
    editor = _find_editor()
    # Cache not needed here — cmd_open opens files, not previews.
    if remote:
        be: LocalBackend | RemoteBackend = RemoteBackend(remote, base_path, ssh_control)
    else:
        be = LocalBackend(base_path, ssh_control)
    for choice in choices:
        _open(choice, editor, state, self_path, be)
    return 0
