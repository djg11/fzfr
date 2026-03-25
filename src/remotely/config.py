"""remotely.config — Defaults, user config loading, and validation."""

import json
import shutil
import sys
from pathlib import Path
from typing import FrozenSet


CONFIG_PATH = Path.home() / ".config" / "remotely" / "config"

# All supported overlay box positions.
_VALID_POSITIONS = {
    "top-left",
    "top-center",
    "top-right",
    "left-center",
    "center",
    "right-center",
    "bottom-left",
    "bottom-center",
    "bottom-right",
}

_CONFIG_DEFAULTS: dict = {
    "ssh_multiplexing": False,
    "ssh_control_persist": 60,
    "editor": "",
    "default_mode": "content",
    "ssh_strict_host_key_checking": True,
    "show_hidden": False,
    "exclude_patterns": [],
    "max_stream_mb": 100,
    "path_format": "relative",
    "file_source": "auto",
}

_VALID_OUTPUTS = {"tmux", "overlay", "silent"}


def _validate_position(value: object, key: str, default: str) -> str:
    """Validate a position string; warn and return default if invalid."""
    if not isinstance(value, str) or value not in _VALID_POSITIONS:
        print(
            f"Warning: {key} {value!r} must be one of "
            f"{sorted(_VALID_POSITIONS)} -- using default {default!r}.",
            file=sys.stderr,
        )
        return default
    return value


def _validate_action(
    gk: str, ak: str, av: object, global_output_position: str
) -> "dict | None":
    """Validate one custom action entry. Returns a clean dict or None to skip."""
    if not isinstance(ak, str) or len(ak) != 1:
        print(
            f"Warning: custom_actions group {gk!r} action key {ak!r} must be a "
            f"single character -- skipping action.",
            file=sys.stderr,
        )
        return None
    if not isinstance(av, dict):
        print(
            f"Warning: custom_actions group {gk!r} action {ak!r} must be a dict -- skipping.",
            file=sys.stderr,
        )
        return None
    cmd = av.get("cmd", "")
    if not isinstance(cmd, str) or not cmd:
        print(
            f"Warning: custom_actions group {gk!r} action {ak!r} requires a "
            f"non-empty 'cmd' string -- skipping.",
            file=sys.stderr,
        )
        return None

    action_label = av.get("label", "")
    if not isinstance(action_label, str):
        action_label = ""

    output = av.get("output", "silent")
    if output == "preview":
        output = "overlay"  # deprecated alias
    if output not in _VALID_OUTPUTS:
        print(
            f"Warning: custom_actions group {gk!r} action {ak!r} output "
            f"{output!r} must be one of {sorted(_VALID_OUTPUTS)} -- defaulting to 'silent'.",
            file=sys.stderr,
        )
        output = "silent"

    action_out_pos = (
        _validate_position(
            av["output_position"],
            f"custom_actions.groups.{gk}.actions.{ak}.output_position",
            global_output_position,
        )
        if "output_position" in av
        else global_output_position
    )

    return {
        "cmd": cmd,
        "label": action_label,
        "output": output,
        "output_position": action_out_pos,
    }


def _validate_group(gk: str, gv: object, global_output_position: str) -> "dict | None":
    """Validate one custom action group. Returns a clean dict or None to skip."""
    if not isinstance(gk, str) or len(gk) != 1:
        print(
            f"Warning: custom_actions group key {gk!r} must be a single "
            f"character -- skipping group.",
            file=sys.stderr,
        )
        return None
    if not isinstance(gv, dict):
        print(
            f"Warning: custom_actions group {gk!r} must be a dict -- skipping.",
            file=sys.stderr,
        )
        return None

    label = gv.get("label", "")
    if not isinstance(label, str):
        print(
            f"Warning: custom_actions group {gk!r} label must be a string -- skipping.",
            file=sys.stderr,
        )
        return None

    raw_actions = gv.get("actions", {})
    if not isinstance(raw_actions, dict):
        print(
            f"Warning: custom_actions group {gk!r} actions must be a dict -- skipping.",
            file=sys.stderr,
        )
        return None

    clean_actions: dict = {}
    for ak, av in raw_actions.items():
        action = _validate_action(gk, ak, av, global_output_position)
        if action is not None:
            clean_actions[ak] = action

    if not clean_actions:
        return None

    return {"label": label, "actions": clean_actions}


def _validate_custom_actions(value: object) -> "dict | None":
    """Validate the custom_actions config block.

    Returns a cleaned dict on success, or None if the top-level structure is
    invalid. Individual bad groups or actions are skipped with a warning.
    """
    if not isinstance(value, dict):
        print(
            "Warning: 'custom_actions' must be a dict, using default.", file=sys.stderr
        )
        return None

    leader = value.get("leader", "ctrl-b")
    if not isinstance(leader, str) or not leader:
        print(
            "Warning: custom_actions.leader must be a non-empty string, using default.",
            file=sys.stderr,
        )
        leader = "ctrl-b"

    menu_position = _validate_position(
        value.get("menu_position", "bottom-right"),
        "custom_actions.menu_position",
        "bottom-right",
    )
    output_position = _validate_position(
        value.get("output_position", "bottom-left"),
        "custom_actions.output_position",
        "bottom-left",
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
        group = _validate_group(gk, gv, output_position)
        if group is not None:
            clean_groups[gk] = group

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
                "Warning: config key 'exclude_patterns' has wrong type (expected list), using default.",
                file=sys.stderr,
            )
            return
        cfg[key] = user_value

    elif key == "file_source":
        if user_value not in ("auto", "fd", "git"):
            print(
                "Warning: config key 'file_source' must be 'auto', 'fd', or 'git', using default.",
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
    """Load ~/.config/remotely/config (JSON) and merge with defaults."""
    cfg = dict(_CONFIG_DEFAULTS)
    cfg["exclude_patterns"] = list(_CONFIG_DEFAULTS["exclude_patterns"])
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
    "xdg-open",
]
AVAILABLE_TOOLS: FrozenSet[str] = frozenset(
    t for t in _ALL_TOOLS if shutil.which(t) is not None
)
