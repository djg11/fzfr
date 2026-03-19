"""fzfr.config — Default configuration and user config loading.

Config is loaded once at import time from ~/.config/fzfr/config (JSON).
Missing keys fall back to _CONFIG_DEFAULTS. The merged result is CONFIG.

AVAILABLE_TOOLS is a frozenset of tool names (fzf, fd, bat, rga, …) that
are present in PATH. It is computed once at import time so per-keystroke
code can check tool availability without forking a subprocess.
"""

import json
import shutil
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "fzfr" / "config"
HISTORY_PATH = Path.home() / ".local" / "share" / "fzfr" / "history"

# DESIGN: Single source of truth for every config key. load_config() only
#         accepts keys present here, so typos in the config file produce a
#         warning rather than silently changing behaviour.
_CONFIG_DEFAULTS: dict = {
    # DESIGN: Default False so users with existing ControlMaster in ~/.ssh/config
    #         are not affected. See module docstring for the conflict warning.
    "ssh_multiplexing": False,
    # How long (seconds) the SSH master socket stays open after the last use.
    # Only applies when ssh_multiplexing is True. Lower values are safer on
    # shared machines; higher values reduce re-authentication prompts (e.g.
    # YubiKey touches) during long sessions. 0 means close immediately on exit.
    "ssh_control_persist": 60,
    # Falls back to $EDITOR, then the nvim/vim/nano/vi chain.
    "editor": "",
    # Either "name" or "content".
    "default_mode": "content",
    # Pass -o StrictHostKeyChecking=yes and a per-session known_hosts file
    # to SSH connections. Prevents MITM, but requires pre-adding hosts or
    # accepting manually. Set to false to defer to system ~/.ssh/config.
    "ssh_strict_host_key_checking": True,
    # Set to true to persist search queries in a history file across sessions.
    # Disabled by default — the history file stores every query typed, which
    # may include sensitive terms (passwords in filenames, internal hostnames,
    # etc.). When enabled, use CTRL-P/CTRL-N to navigate history.
    "search_history": False,
    # Set to true to show hidden files and directories (those starting with .)
    # by default. Can be toggled at runtime with a keybinding.
    "show_hidden": False,
    "exclude_patterns": [],  # List of glob patterns to exclude from search
    # Maximum size in MB for streaming a remote binary file to a local temp
    # file for xdg-open. Files larger than this are refused with an error
    # message. WORK_BASE is on tmpfs (RAM-backed), so large files consume
    # memory. Set to 0 to disable the limit (not recommended).
    "max_stream_mb": 100,
    "keybindings": {
        "toggle_mode": "ctrl-t",
        "toggle_ftype": "ctrl-d",
        "toggle_hidden": "ctrl-h",
        "filter_ext": "ctrl-f",
        "add_exclude": "ctrl-x",
        "refresh_list": "ctrl-r",
        "sort_list": "ctrl-s",
        "copy_path": "ctrl-c",
        "open_file": "enter",
        "preview_half_page_down": "alt-j",
        "preview_half_page_up": "alt-k",
        "history_prev": "ctrl-p",
        "history_next": "ctrl-n",
        "exit": "esc",
    },
    # Output path format shown in the fzf list.
    # "absolute" — full paths (e.g. /home/user/project/src/foo.py)
    # "relative" — paths relative to the search root (e.g. src/foo.py)
    "path_format": "relative",
    # File listing source.
    # "auto" — use git ls-files when inside a git repo, fd everywhere else
    # "fd"   — always use fd (original behaviour)
    # "git"  — always use git ls-files (errors outside a repo)
    # For remote hosts, "auto" behaves like "fd" unless explicitly set to
    # "git" — detecting a remote git repo requires an extra SSH round-trip
    # that is too expensive to run on every keystroke.
    "file_source": "auto",
}


def _merge_config_key(cfg: dict, key: str, default: object, user_value: object) -> None:
    """Validate and merge one user config value into cfg.

    Handles three special cases:
    - "exclude_patterns" must be a list
    - "keybindings" must be a dict; individual keybinding values must be strings
    - all other keys must match the type of their default value

    Invalid values are silently dropped and the default is kept.
    """
    if key == "exclude_patterns":
        if not isinstance(user_value, list):
            print(
                "Warning: config key 'exclude_patterns' has wrong type "
                "(expected list), using default.",
                file=sys.stderr,
            )
            return
        cfg[key] = user_value

    elif key == "keybindings":
        if not isinstance(user_value, dict):
            print(
                "Warning: config key 'keybindings' has wrong type "
                "(expected dict), using default.",
                file=sys.stderr,
            )
            return
        for action, kbd in user_value.items():
            if not isinstance(kbd, str):
                print(
                    f"Warning: keybinding for '{action}' has wrong type "
                    f"(expected string), using default.",
                    file=sys.stderr,
                )
            else:
                cfg[key][action] = kbd

    elif key == "file_source":
        if user_value not in ("auto", "fd", "git"):
            print(
                "Warning: config key 'file_source' must be 'auto', 'fd', or 'git', "
                "using default.",
                file=sys.stderr,
            )
            return
        cfg[key] = user_value

    else:
        # SECURITY: isinstance() handles subclasses correctly (e.g. bool is a
        #           subclass of int, so "ssh_multiplexing": 1 would wrongly pass
        #           a type() == type(False) check but is caught here).
        if not isinstance(user_value, type(default)):
            print(
                f"Warning: config key '{key}' has wrong type "
                f"(expected {type(default).__name__}), using default.",
                file=sys.stderr,
            )
            return
        cfg[key] = user_value


def load_config() -> dict:
    """Load ~/.config/fzfr/config (JSON) and merge with defaults.

    The config file is entirely optional. Missing keys fall back to
    _CONFIG_DEFAULTS. Unknown keys are silently ignored so that old config
    files survive script upgrades that add or remove options. Type mismatches
    produce a warning and keep the default.

    Returns a dict guaranteed to contain every key in _CONFIG_DEFAULTS.
    """
    cfg = dict(_CONFIG_DEFAULTS)
    if not CONFIG_PATH.exists():
        return cfg
    try:
        user = json.loads(CONFIG_PATH.read_text())
        for key, default in _CONFIG_DEFAULTS.items():
            if key in user:
                _merge_config_key(cfg, key, default, user[key])
    except json.JSONDecodeError as exc:
        print(f"Warning: could not parse {CONFIG_PATH}: {exc}", file=sys.stderr)
    return cfg


# PERF: Loaded once at import time so every sub-command invocation (including
#       the short-lived fzf callback processes) shares the same config without
#       re-reading and re-parsing the file.
CONFIG = load_config()

# PERF: shutil.which() probes PATH on every call. Running it once at import
#       time and storing results in a frozenset lets every subsequent lookup
#       be an O(1) set membership test with no subprocess or filesystem I/O.
_ALL_TOOLS = [
    "fzf",
    "fd",
    "git",
    "bat",
    "rga",
    "pdftotext",
    "tmux",
    "file",
    "grep",
    "xargs",
    "ssh",
    "7z",
    "unrar",
    "unzip",
    "tar",
    "bzcat",
    "xzcat",
    "lz4",
    "zstd",
    "cpio",
    "gunzip",
    "zcat",
    "head",
    "tree",
    "eza",
    "exa",
    "ls",
    "xxd",
    "hexdump",
    "xclip",
    "pbcopy",
    "wl-copy",
    "xdg-open",
]
AVAILABLE_TOOLS: frozenset[str] = frozenset(
    t for t in _ALL_TOOLS if shutil.which(t) is not None
)

HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
# SECURITY: History file contains search queries, so it should be owner-only.
#           fzf creates the file if it doesn't exist, so we touch it here to
#           ensure correct permissions from the start.
HISTORY_PATH.touch(mode=0o600, exist_ok=True)
