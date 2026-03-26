"""
remotely - Zero-install SSH file transport for fuzzy-finders (fzf, Television, etc.).

Architecture overview
---------------------
Single-file, multi-command tool modelled after busybox: one script contains
every sub-command. It can be invoked in two ways:

  1. Sub-command mode:   remotely list user@host:/path
  2. Symlink mode:       remotely-preview <file>
                         (symlink named "remotely-preview" pointing at this file)

Call graph (headless API)
--------------------------
  remotely list user@host:/path
    -> get_session_dir() / ensure_reaper()  [session.py]
    -> acquire_socket(host)                 [session.py]
    -> ssh host fd ...                      [list.py -> remote.py]

  remotely preview user@host:/path [query]
    -> get_session_dir() / ensure_reaper()
    -> acquire_socket(host)
    -> cache check (session_dir/preview/)   [cache.py]
    -> _cmd_remote_preview_capture          [preview_cmd.py -> remote.py]
    -> cache store

  remotely open user@host:/path
    -> get_session_dir() / ensure_reaper()
    -> acquire_socket(host)
    -> text:   ssh cat -> $EDITOR (tmux new-window -d if inside tmux)
               -> tmux wait-for -> scp back       [open_cmd.py]
    -> binary: stat + OOM check -> stream to cache
               -> xdg-open detached               [open_cmd.py]

  remotely gc
    -> gc_stale_sessions()                        [session.py]

Session lifecycle
-----------------
Each remotely invocation calls get_session_dir(anchor_pid=os.getppid()).
All invocations spawned by the same fzf process share the same anchor PID
and therefore the same session directory.

ensure_reaper() spawns a detached background process (double-fork) that
polls os.kill(anchor_pid, 0) every 2 seconds.  When the shell exits the
reaper removes the session directory and closes any ControlMaster sockets.

SSH connection multiplexing
---------------------------
By default remotely passes NO extra flags to ssh (SSH_DEFERRED), deferring
entirely to ~/.ssh/config.  Enable managed multiplexing with:

  ~/.config/remotely/config:
    { "ssh_multiplexing": true }

WARNING: do NOT set ssh_multiplexing: true if ~/.ssh/config already has
ControlMaster.  Conflicting sockets trigger spurious auth prompts.

Quoting discipline
------------------
  list-form subprocess  ->  ["cmd", "--flag", path]    local calls
  shlex.join()          ->  for SSH remote commands
  shlex.quote()         ->  for individual tokens in remote commands

Source layout
-------------
  _script.py     VERSION, SELF, SCRIPT_BYTES
  utils.py       subprocess helpers, MIME detection, extension parsing, SSH path
  workbase.py    session working directory (prefers /dev/shm)
  config.py      defaults and user config loading
  ssh.py         SSH option construction
  session.py     anchor-PID session manager, reaper, SSH sockets
  state.py       session state load/save/mutate
  cache.py       per-session file-backed LRU preview cache
  archive.py     archive format detection and listing
  backends.py    LocalBackend, RemoteBackend
  preview.py     file preview rendering
  remote.py      SSH remote search and preview
  list.py        remotely list headless sub-command
  preview_cmd.py remotely preview headless sub-command
  open_cmd.py    remotely open headless sub-command (text + binary)
  gc.py          remotely gc sub-command
"""

import ctypes
import sys
from pathlib import Path

from ._script import (
    _BOOTSTRAP_CACHE_MISS,
    SCRIPT_BOOTSTRAP,
    SCRIPT_BYTES,
    SCRIPT_HASH,
    SELF,
    VERSION,
)
from .gc import cmd_gc
from .list import cmd_list
from .open_cmd import cmd_open_headless
from .preview import cmd_preview
from .preview_cmd import cmd_preview_headless
from .remote import cmd_remote_preview, cmd_remote_reload


# Re-exported from ._script
__all__ = [
    "VERSION",
    "SELF",
    "SCRIPT_BYTES",
    "SCRIPT_HASH",
    "SCRIPT_BOOTSTRAP",
    "_BOOTSTRAP_CACHE_MISS",
]

COMMAND_MAP = {
    # -- Headless transport API --
    "remotely-list": cmd_list,
    "list": cmd_list,
    "preview": cmd_preview_headless,
    "open": cmd_open_headless,
    # -- Preview (called by headless API and directly by name) --
    "remotely-preview": cmd_preview,
    # -- Remote sub-commands (called by headless API) --
    "remotely-remote-reload": cmd_remote_reload,
    "remotely-remote-preview": cmd_remote_preview,
    # -- Maintenance --
    "gc": cmd_gc,
}


def _set_process_name(name: str) -> None:
    """Set the process name visible in ps/top via prctl(PR_SET_NAME).

    Linux only -- silently ignored on other platforms.
    """
    try:
        ctypes.CDLL(None).prctl(15, name.encode()[:15] + b"\x00", 0, 0, 0)
    except Exception:
        pass  # non-Linux or ctypes unavailable -- harmless


def main():
    """Resolve which sub-command to run and execute it."""
    _set_process_name("python3 remotely")
    invoked_as = Path(sys.argv[0]).name

    # -- Version flag: handle before sub-command dispatch --------------------
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print("remotely " + VERSION)
        sys.exit(0)

    if invoked_as in COMMAND_MAP and invoked_as != "remotely":
        fn, args = COMMAND_MAP[invoked_as], sys.argv[1:]
    elif len(sys.argv) > 1 and sys.argv[1] in COMMAND_MAP:
        fn, args = COMMAND_MAP[sys.argv[1]], sys.argv[2:]
    else:
        fn, args = cmd_list, sys.argv[1:]

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
