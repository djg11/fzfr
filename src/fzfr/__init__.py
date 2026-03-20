"""
fzfr - Fuzzy file search for local and remote filesystems.

Architecture overview
---------------------
Single-file, multi-command tool modelled after busybox: one script contains
every sub-command. It can be invoked in two ways:

  1. Sub-command mode:   fzfr fzfr-preview <file>
  2. Symlink mode:       fzfr-preview <file>
                         (symlink named "fzfr-preview" pointing at this file)

The main command (fzfr) launches fzf with --bind and --preview strings that
call back into this same script. All callbacks embed the absolute path of this
file (SELF) so they work regardless of what is in PATH when fzf spawns a
sub-shell.

Call graph (local search)
--------------------------
  fzfr
    └─ fzf  (--preview, --bind, --transform each call back into:)
         ├─ fzfr _internal-get-prompt   <state>           (transform-prompt)
         ├─ fzfr _internal-get-header   <state>           (transform-header)
         ├─ fzfr _internal-get-search-action <state>      (transform)
         ├─ fzfr _internal-toggle-mode  <state>           (CTRL-T)
         ├─ fzfr _internal-toggle-ftype <state>           (CTRL-D)
         ├─ fzfr _internal-toggle-hidden <state>          (CTRL-H)
         ├─ fzfr _internal-prompt       <state> ext ...   (CTRL-F)
         ├─ fzfr _internal-exclude      <state>           (CTRL-X)
         ├─ fzfr _internal-dispatch     <state> preview {} {q}
         │    └─ fzfr-preview {} [q]
         ├─ fzfr _internal-dispatch     <state> reload {q}
         │    └─ rga ... or fd | grep ...
         └─ fzfr fzfr-open local <base> '' '' {}          (Enter)

Call graph (remote search)
---------------------------
  fzfr user@host /path
    └─ fzf
         ├─ fzfr _internal-dispatch <state> preview {} {q}
         │    └─ fzfr-remote-preview host /path <ssh_ctl> {} {q}
         │         └─ ssh host "python3 - fzfr-preview <file> [q]"
         ├─ fzfr _internal-dispatch <state> reload {q}
         │    └─ fzfr-remote-reload host /path <ssh_ctl> {q}
         │         └─ ssh host "cd /path && rga ... || fd | xargs grep ..."
         └─ fzfr fzfr-open host /path host <WORK_BASE>/... <ssh_ctl> {}
              └─ ssh host -t "nvim <file>"   (text)
                 or: ssh host "cat <file>" → local temp → xdg-open   (binary)

SSH connection multiplexing
---------------------------
By default, fzfr passes NO extra flags to ssh, deferring entirely to
~/.ssh/config. If that config already has ControlMaster/ControlPath, all fzf
callbacks reuse the existing master connection — no key prompts, no latency.

If you do NOT have multiplexing in your ssh config, enable it here:

  ~/.config/fzfr/config:
    {
      "ssh_multiplexing": true
    }

fzfr will then create a per-session socket in WORK_BASE and tear it down on
exit.

WARNING: do NOT set ssh_multiplexing = true if your ~/.ssh/config already
has ControlMaster/ControlPath. The two sockets would conflict and fzfr would
open a new master connection instead of reusing the existing one — triggering
spurious key prompts (e.g. YubiKey touch) on every cursor move.

Quoting discipline
------------------
Three quoting strategies are used deliberately:

  list-form subprocess  →  ["cmd", "--flag", path]
                            Zero quoting needed for LOCAL calls. The OS passes
                            each element directly as an argv token — no shell
                            sees it. Used for all local subprocess calls (fd,
                            bat, fzf, tmux, xdg-open, etc.).

  shlex.join()          →  shlex.join(["realpath", "-e", "--", path])
                            Produces a properly shell-quoted string from a list.
                            REQUIRED for all SSH remote commands — SSH
                            concatenates all arguments after the hostname into a
                            single string which the remote shell word-splits.
                            Also used for fzf --bind strings that fzf evaluates
                            via a shell, and for SSH commands with shell
                            operators (pipes, &&, redirects).

  _dquote()             →  double-quoted: "path with spaces"
                            Safe for TWO shell levels. Used only for the editor
                            command in _open() when it travels through:
                              local shell (tmux new-window) → ssh → remote shell
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
The distributable fzfr script is built from src/fzfr/ by
scripts/build_single_file.py. Each module is self-contained with correct
imports so linters work per-file. The build script strips intra-package
imports and deduplicates stdlib imports into one block at the top.

  _script.py   VERSION, SELF, SCRIPT_BYTES — no intra-package imports
  utils.py     subprocess helpers, MIME detection, extension parsing
  workbase.py  session working directory (prefers /dev/shm)
  config.py    defaults and user config loading
  tty.py       /dev/tty prompt helper
  ssh.py       SSH option construction
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
  search.py    main fzf UI entry point, session lifecycle
"""

import ctypes
import sys
from pathlib import Path

from ._script import (
    VERSION, SELF, SCRIPT_BYTES, SCRIPT_HASH, SCRIPT_BOOTSTRAP, _BOOTSTRAP_CACHE_MISS,
)
from .preview import cmd_preview
from .open import cmd_open
from .copy import cmd_copy
from .remote import cmd_remote_reload, cmd_remote_preview
from .search import cmd_search
from .dispatch import cmd_dispatch
from .internal import (
      cmd_internal_get_prompt, cmd_internal_get_header,
      cmd_internal_get_search_action, cmd_internal_toggle_mode,
      cmd_internal_toggle_ftype, cmd_internal_toggle_hidden,
      cmd_internal_prompt, cmd_internal_exclude,
      cmd_internal_exec, cmd_internal_action_menu
)

# Re-exported from ._script — used by remote.py, search.py, and the built flat file.
__all__ = [
    "VERSION", "SELF", "SCRIPT_BYTES", "SCRIPT_HASH", "SCRIPT_BOOTSTRAP",
    "_BOOTSTRAP_CACHE_MISS",
]

COMMAND_MAP = {
    "fzfr-preview": cmd_preview,
    "fzfr-open": cmd_open,
    "fzfr-remote-reload": cmd_remote_reload,
    "fzfr-remote-preview": cmd_remote_preview,
    "fzfr-copy": cmd_copy,
    "fzfr": cmd_search,
    # Internal callbacks invoked by fzf bindings:
    "_internal-get-prompt": cmd_internal_get_prompt,  # transform-prompt
    "_internal-get-header": cmd_internal_get_header,  # transform-header
    "_internal-get-search-action": cmd_internal_get_search_action,  # transform (disable/enable-search)
    "_internal-toggle-mode": cmd_internal_toggle_mode,  # execute-silent CTRL-T
    "_internal-toggle-ftype": cmd_internal_toggle_ftype,  # execute-silent CTRL-D
    "_internal-toggle-hidden": cmd_internal_toggle_hidden,  # execute-silent CTRL-H
    "_internal-prompt": cmd_internal_prompt,  # execute CTRL-F (reads /dev/tty)
    "_internal-exclude": cmd_internal_exclude,  # execute CTRL-X (reads /dev/tty)
    "_internal-dispatch": cmd_dispatch,  # preview + reload
    "_internal-exec": cmd_internal_exec,
    "_internal-action-menu": cmd_internal_action_menu,
}


def _set_process_name(name: str) -> None:
    """Set the process name visible in ps/top via prctl(PR_SET_NAME).

    Linux only — silently ignored on other platforms. Makes the remote
    agent appear as 'python3 fzfr' rather than 'python3 -' or
    'python3 /path/to/script.py', reducing visual noise for sysadmins.
    """
    try:
        ctypes.CDLL(None).prctl(15, name.encode()[:15] + b"\x00", 0, 0, 0)
    except Exception:
        pass  # non-Linux or ctypes unavailable -- harmless


def main():
    """Resolve which sub-command to run and execute it."""
    _set_process_name("python3 fzfr")
    invoked_as = Path(sys.argv[0]).name

    if invoked_as in COMMAND_MAP and invoked_as != "fzfr":
        fn, args = COMMAND_MAP[invoked_as], sys.argv[1:]
    elif len(sys.argv) > 1 and sys.argv[1] in COMMAND_MAP:
        fn, args = COMMAND_MAP[sys.argv[1]], sys.argv[2:]
    else:
        fn, args = cmd_search, sys.argv[1:]

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
