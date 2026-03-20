"""fzfr.config — Defaults, user config loading, and validation."""

import json
import shutil
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "fzfr" / "config"
HISTORY_PATH = Path.home() / ".local" / "share" / "fzfr" / "history"

# All supported overlay box positions — used by menu_position and output_position.
_VALID_POSITIONS = {
    "top-left", "top-center", "top-right",
    "left-center", "center", "right-center",
    "bottom-left", "bottom-center", "bottom-right",
}

_CONFIG_DEFAULTS: dict = {
    "ssh_multiplexing": False,
    "ssh_control_persist": 60,
    "editor": "",
    "default_mode": "content",
    "ssh_strict_host_key_checking": True,
    "search_history": False,
    "show_hidden": False,
    "exclude_patterns": [],
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
    "path_format": "relative",
    "file_source": "auto",
    "custom_actions": {
        "leader": "ctrl-b",
        "menu_position": "bottom-right",
        "output_position": "bottom-left",
        "groups": {},
    },
}


def _validate_position(value: object, key: str, default: str) -> str:
    """Validate a position string; warn and return default if invalid."""
    if not isinstance(value, str) or value not in _VALID_POSITIONS:
        print(
            f"Warning: custom_actions.{key} {value!r} must be one of "
            f"{sorted(_VALID_POSITIONS)} — using default {default!r}.",
            file=sys.stderr,
        )
        return default
    return value


def _validate_custom_actions(value: object) -> "dict | None":
    """Validate the custom_actions config block.

    Returns a cleaned dict on success, or None if the top-level structure is
    invalid. Individual bad groups or actions are skipped with a warning so
    that a single misconfigured action never prevents fzfr from launching.

    Rules enforced:
    - value must be a dict
    - value["leader"] must be a non-empty string
    - value["leader"] must not conflict with fzfr's reserved bindings
    - value["groups"] must be a dict
    - each group key must be exactly one character
    - each group must have "label" (str) and "actions" (dict)
    - each action key must be exactly one character
    - each action must have "cmd" (non-empty str) and "label" (str)
    - each action "output" must be one of "tmux", "preview", "silent"
    """
    _RESERVED = {
        "ctrl-t", "ctrl-d", "ctrl-h", "ctrl-f", "ctrl-x",
        "ctrl-r", "ctrl-s", "ctrl-c", "ctrl-p", "ctrl-n",
        "ctrl-g",  # fzf hardcoded exit — cannot be overridden
        "enter", "esc", "alt-j", "alt-k",
    }
    _VALID_OUTPUTS = {"tmux", "overlay", "silent"}

    if not isinstance(value, dict):
        print(
            "Warning: 'custom_actions' must be a dict, using default.",
            file=sys.stderr,
        )
        return None

    leader = value.get("leader", "ctrl-space")
    if not isinstance(leader, str) or not leader:
        print(
            "Warning: custom_actions.leader must be a non-empty string, using default.",
            file=sys.stderr,
        )
        leader = "ctrl-b"
    if leader in _RESERVED:
        print(
            f"Warning: custom_actions.leader {leader!r} conflicts with a reserved "
            f"fzfr keybinding. Choose a different key.",
            file=sys.stderr,
        )
        leader = "ctrl-b"

    menu_position = _validate_position(
        value.get("menu_position", "bottom-right"), "menu_position", "bottom-right"
    )
    output_position = _validate_position(
        value.get("output_position", "bottom-left"), "output_position", "bottom-left"
    )

    raw_groups = value.get("groups", {})
    if not isinstance(raw_groups, dict):
        print(
            "Warning: custom_actions.groups must be a dict, using empty groups.",
            file=sys.stderr,
        )
        raw_groups = {}

    clean_groups: dict = {}
    for gk, gv in raw_groups.items():
        if not isinstance(gk, str) or len(gk) != 1:
            print(
                f"Warning: custom_actions group key {gk!r} must be a single "
                f"character — skipping group.",
                file=sys.stderr,
            )
            continue
        if not isinstance(gv, dict):
            print(
                f"Warning: custom_actions group {gk!r} must be a dict — skipping.",
                file=sys.stderr,
            )
            continue
        label = gv.get("label", "")
        if not isinstance(label, str):
            print(
                f"Warning: custom_actions group {gk!r} label must be a string — skipping.",
                file=sys.stderr,
            )
            continue
        raw_actions = gv.get("actions", {})
        if not isinstance(raw_actions, dict):
            print(
                f"Warning: custom_actions group {gk!r} actions must be a dict — skipping.",
                file=sys.stderr,
            )
            continue

        clean_actions: dict = {}
        for ak, av in raw_actions.items():
            if not isinstance(ak, str) or len(ak) != 1:
                print(
                    f"Warning: custom_actions group {gk!r} action key {ak!r} must be a "
                    f"single character — skipping action.",
                    file=sys.stderr,
                )
                continue
            if not isinstance(av, dict):
                print(
                    f"Warning: custom_actions group {gk!r} action {ak!r} must be a "
                    f"dict — skipping.",
                    file=sys.stderr,
                )
                continue
            cmd = av.get("cmd", "")
            if not isinstance(cmd, str) or not cmd:
                print(
                    f"Warning: custom_actions group {gk!r} action {ak!r} requires a "
                    f"non-empty 'cmd' string — skipping.",
                    file=sys.stderr,
                )
                continue
            action_label = av.get("label", "")
            if not isinstance(action_label, str):
                action_label = ""
            output = av.get("output", "silent")
            # Accept "preview" as deprecated alias for "overlay"
            if output == "preview":
                output = "overlay"
            if output not in _VALID_OUTPUTS:
                print(
                    f"Warning: custom_actions group {gk!r} action {ak!r} output "
                    f"{output!r} must be one of {sorted(_VALID_OUTPUTS)} — "
                    f"defaulting to 'silent'.",
                    file=sys.stderr,
                )
                output = "silent"
            # Per-action output_position overrides the global default
            action_out_pos = _validate_position(
                av["output_position"],
                f"groups.{gk}.actions.{ak}.output_position",
                output_position,
            ) if "output_position" in av else output_position
            clean_actions[ak] = {
                "cmd": cmd,
                "label": action_label,
                "output": output,
                "output_position": action_out_pos,
            }

        if clean_actions:
            clean_groups[gk] = {"label": label, "actions": clean_actions}

    return {
        "leader": leader,
        "menu_position": menu_position,
        "output_position": output_position,
        "groups": clean_groups,
    }


def _merge_config_key(cfg: dict, key: str, default: object, user_value: object) -> None:
    """Validate and merge one user config value into cfg."""
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

    elif key == "custom_actions":
        cleaned = _validate_custom_actions(user_value)
        if cleaned is not None:
            cfg[key] = cleaned

    else:
        if not isinstance(user_value, type(default)):
            print(
                f"Warning: config key '{key}' has wrong type "
                f"(expected {type(default).__name__}), using default.",
                file=sys.stderr,
            )
            return
        cfg[key] = user_value


def load_config() -> dict:
    """Load ~/.config/fzfr/config (JSON) and merge with defaults."""
    cfg = dict(_CONFIG_DEFAULTS)
    # Deep copy the nested dicts so mutations don't affect the defaults
    cfg["keybindings"] = dict(_CONFIG_DEFAULTS["keybindings"])
    cfg["custom_actions"] = {
        "leader": _CONFIG_DEFAULTS["custom_actions"]["leader"],
        "menu_position": _CONFIG_DEFAULTS["custom_actions"]["menu_position"],
        "output_position": _CONFIG_DEFAULTS["custom_actions"]["output_position"],
        "groups": {},
    }
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


CONFIG = load_config()

_ALL_TOOLS = [
    "fzf", "fd", "git", "bat", "rga", "pdftotext", "tmux", "file",
    "grep", "xargs", "ssh", "7z", "unrar", "unzip", "tar", "bzcat",
    "xzcat", "lz4", "zstd", "cpio", "gunzip", "zcat", "head", "tree",
    "eza", "exa", "ls", "xxd", "hexdump", "xclip", "pbcopy", "wl-copy",
    "xdg-open",
]
AVAILABLE_TOOLS: frozenset[str] = frozenset(
    t for t in _ALL_TOOLS if shutil.which(t) is not None
)

HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
HISTORY_PATH.touch(mode=0o600, exist_ok=True)
