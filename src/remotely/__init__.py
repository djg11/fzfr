"""
remotely - Fuzzy file search for local and remote filesystems.

Architecture overview
---------------------
Single-file, multi-command tool modelled after busybox: one script contains
every sub-command. It can be invoked in two ways:

  1. Sub-command mode:   remotely remotely-preview <file>
  2. Symlink mode:       remotely-preview <file>
                         (symlink named "remotely-preview" pointing at this file)

The main command (remotely) launches fzf with --bind and --preview strings that
call back into this same script. All callbacks embed the absolute path of this
file (SELF) so they work regardless of what is in PATH when fzf spawns a
sub-shell.

Call graph (local search)
--------------------------
  remotely
    └─ fzf  (--preview, --bind, --transform each call back into:)
         ├─ remotely _internal-get-prompt   <state>           (transform-prompt)
         ├─ remotely _internal-get-header   <state>           (transform-header)
         ├─ remotely _internal-get-search-action <state>      (transform)
         ├─ remotely _internal-toggle-mode  <state>           (CTRL-T)
         ├─ remotely _internal-toggle-ftype <state>           (CTRL-D)
         ├─ remotely _internal-toggle-hidden <state>          (CTRL-H)
         ├─ remotely _internal-prompt       <state> ext ...   (CTRL-F)
         ├─ remotely _internal-exclude      <state>           (CTRL-X)
         ├─ remotely _internal-dispatch     <state> preview {} {q}
         │    └─ remotely-preview {} [q]
         ├─ remotely _internal-dispatch     <state> reload {q}
         │    └─ rga ... or fd | grep ...
         └─ remotely remotely-open local <base> '' '' {}          (Enter)

Call graph (remote search)
---------------------------
  remotely user@host /path
    └─ fzf
         ├─ remotely _internal-dispatch <state> preview {} {q}
         │    └─ remotely-remote-preview host /path <ssh_ctl> {} {q}
         │         └─ ssh host "python3 - remotely-preview <file> [q]"
         ├─ remotely _internal-dispatch <state> reload {q}
         │    └─ remotely-remote-reload host /path <ssh_ctl> {q}
         │         └─ ssh host "cd /path && rga ... || fd | xargs grep ..."
         └─ remotely remotely-open host /path host <WORK_BASE>/... <ssh_ctl> {}
              └─ ssh host -t "nvim <file>"   (text)
                 or: ssh host "cat <file>" → local temp → xdg-open   (binary)

SSH connection multiplexing
---------------------------
By default, remotely passes NO extra flags to ssh, deferring entirely to
~/.ssh/config. If that config already has ControlMaster/ControlPath, all fzf
callbacks reuse the existing master connection — no key prompts, no latency.

If you do NOT have multiplexing in your ssh config, enable it here:

  ~/.config/remotely/config:
    {
      "ssh_multiplexing": true
    }

remotely will then create a per-session socket in WORK_BASE and tear it down on
exit.

WARNING: do NOT set ssh_multiplexing = true if your ~/.ssh/config already
has ControlMaster/ControlPath. The two sockets would conflict and remotely would
open a new master connection instead of reusing the existing one -- triggering
spurious key prompts (e.g. YubiKey touch) on every cursor move.

Quoting discipline
------------------
Three quoting strategies are used deliberately:

  list-form subprocess  ->  ["cmd", "--flag", path]
                            Zero quoting needed for LOCAL calls. The OS passes
                            each element directly as an argv token -- no shell
                            sees it. Used for all local subprocess calls (fd,
                            bat, fzf, tmux, xdg-open, etc.).

  shlex.join()          ->  shlex.join(["realpath", "-e", "--", path])
                            Produces a properly shell-quoted string from a list.
                            REQUIRED for all SSH remote commands -- SSH
                            concatenates all arguments after the hostname into a
                            single string which the remote shell word-splits.
                            Also used for fzf --bind strings that fzf evaluates
                            via a shell, and for SSH commands with shell
                            operators (pipes, &&, redirects).

  _dquote()             ->  double-quoted: "path with spaces"
                            Safe for TWO shell levels. Used only for the editor
                            command in _open() when it travels through:
                              local shell (tmux new-window) -> ssh -> remote shell
                            Single-quoted paths break at the first level because
                            a single quote cannot appear inside a single-quoted
                            string.

fzf placeholder quoting
-----------------------
fzf expands {} (selected item) and {q} (current query) into shell-escaped
tokens automatically. These placeholders must therefore be left UNQUOTED in
the --preview and --bind strings we pass to fzf. Quoting them (e.g. '{q}')
would cause double-escaping and break filenames with spaces or special chars.

Source layout
-------------
The distributable remotely script is built from src/remotely/ by
scripts/build_single_file.py. Each module is self-contained with correct
imports so linters work per-file. The build script strips intra-package
imports and deduplicates stdlib imports into one block at the top.

  _script.py   VERSION, SELF, SCRIPT_BYTES -- no intra-package imports
  utils.py     subprocess helpers, MIME detection, extension parsing
  workbase.py  session working directory (prefers /dev/shm)
  config.py    defaults and user config loading
  tty.py       /dev/tty prompt helper
  ssh.py       SSH option construction
  session.py   host-keyed SSH session manager (socket lifecycle + locking)
  state.py     session state load/save/mutate
  cache.py     preview output cache
  archive.py   archive format detection and listing
  backends.py  Backend protocol, LocalBackend, RemoteBackend
  preview.py   file preview rendering
  internal.py  fzf callback sub-commands (_internal-*)
  dispatch.py  _internal-dispatch router
  open.py      file open logic (editor, xdg-open, remote streaming)
  copy.py      clipboard copy sub-command
  remote.py    SSH remote search and preview
  list.py      remotely list headless sub-command
  search.py    main fzf UI entry point, session lifecycle
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
from .copy import cmd_copy
from .dispatch import cmd_dispatch
from .internal import (
    cmd_internal_action_menu,
    cmd_internal_exclude,
    cmd_internal_exec,
    cmd_internal_get_header,
    cmd_internal_get_prompt,
    cmd_internal_get_search_action,
    cmd_internal_prompt,
    cmd_internal_toggle_ftype,
    cmd_internal_toggle_hidden,
    cmd_internal_toggle_mode,
)
from .list import cmd_list
from .open import cmd_open
from .preview import cmd_preview
from .preview_cmd import cmd_preview_headless
from .remote import cmd_remote_preview, cmd_remote_reload
from .search import cmd_search


# Re-exported from ._script -- used by remote.py, search.py, and the built flat file.
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
    # -- Preview / open (internal fzf callbacks, unchanged) --
    "remotely-preview": cmd_preview,
    "remotely-open": cmd_open,
    # -- Remote sub-commands (called by fzf callbacks and headless API) --
    "remotely-remote-reload": cmd_remote_reload,
    "remotely-remote-preview": cmd_remote_preview,
    # -- Clipboard --
    "remotely-copy": cmd_copy,
    # -- Main fzf TUI (legacy) --
    "remotely": cmd_search,
    # -- Internal callbacks invoked by fzf bindings --
    "_internal-get-prompt": cmd_internal_get_prompt,
    "_internal-get-header": cmd_internal_get_header,
    "_internal-get-search-action": cmd_internal_get_search_action,
    "_internal-toggle-mode": cmd_internal_toggle_mode,
    "_internal-toggle-ftype": cmd_internal_toggle_ftype,
    "_internal-toggle-hidden": cmd_internal_toggle_hidden,
    "_internal-prompt": cmd_internal_prompt,
    "_internal-exclude": cmd_internal_exclude,
    "_internal-dispatch": cmd_dispatch,
    "_internal-exec": cmd_internal_exec,
    "_internal-action-menu": cmd_internal_action_menu,
}


def _set_process_name(name: str) -> None:
    """Set the process name visible in ps/top via prctl(PR_SET_NAME).

    Linux only -- silently ignored on other platforms. Makes the remote
    agent appear as 'python3 remotely' rather than 'python3 -' or
    'python3 /path/to/script.py', reducing visual noise for sysadmins.
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
        fn, args = cmd_search, sys.argv[1:]

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
