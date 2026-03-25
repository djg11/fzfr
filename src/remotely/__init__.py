"""
remotely - Fuzzy file search for local and remote filesystems.

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
    -> acquire_socket(host)  [session.py]
    -> ssh host fd ...       [list.py -> remote.py]

  remotely preview user@host:/path [query]
    -> acquire_socket(host)
    -> cmd_remote_preview    [preview_cmd.py -> remote.py]

  remotely open user@host:/path
    -> acquire_socket(host)
    -> ssh cat -> $EDITOR -> scp back  [open_cmd.py]

SSH connection multiplexing
---------------------------
By default remotely passes NO extra flags to ssh, deferring entirely to
~/.ssh/config. If that config already has ControlMaster/ControlPath, all
calls reuse the existing master connection.

If you do NOT have multiplexing in your ssh config, enable it here:

  ~/.config/remotely/config:
    {
      "ssh_multiplexing": true
    }

WARNING: do NOT set ssh_multiplexing: true if your ~/.ssh/config already
has ControlMaster/ControlPath. The conflicting sockets will trigger
spurious key prompts (e.g. YubiKey touch) on every cursor move.

Quoting discipline
------------------
Three quoting strategies are used deliberately:

  list-form subprocess  ->  ["cmd", "--flag", path]
                            Zero quoting needed for LOCAL calls.

  shlex.join()          ->  shlex.join(["realpath", "-e", "--", path])
                            REQUIRED for all SSH remote commands.

  shlex.quote()         ->  for individual tokens within remote commands.

Source layout
-------------
  _script.py    VERSION, SELF, SCRIPT_BYTES
  utils.py      subprocess helpers, MIME detection, extension parsing, SSH path
  workbase.py   session working directory (prefers /dev/shm)
  config.py     defaults and user config loading
  ssh.py        SSH option construction
  session.py    host-keyed SSH session manager
  state.py      session state load/save/mutate
  cache.py      preview output cache
  archive.py    archive format detection and listing
  backends.py   LocalBackend, RemoteBackend
  preview.py    file preview rendering
  remote.py     SSH remote search and preview
  list.py       remotely list headless sub-command
  preview_cmd.py  remotely preview headless sub-command
  open_cmd.py   remotely open headless sub-command
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

    if invoked_as in COMMAND_MAP and invoked_as != "remotely":
        fn, args = COMMAND_MAP[invoked_as], sys.argv[1:]
    elif len(sys.argv) > 1 and sys.argv[1] in COMMAND_MAP:
        fn, args = COMMAND_MAP[sys.argv[1]], sys.argv[2:]
    else:
        # Default to `remotely list` when invoked with no recognised sub-command.
        fn, args = cmd_list, sys.argv[1:]

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
