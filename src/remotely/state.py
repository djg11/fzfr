"""remotely.state — Session state file load/save/mutate."""

import json
from collections.abc import Callable
from pathlib import Path

from .workbase import WORK_BASE


def _save_state(path: Path, state: dict) -> None:
    """Atomically save the session state to a JSON file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _load_state(path: Path) -> dict:
    """Load the session state from a JSON file.

    SECURITY: Validates that the path is inside WORK_BASE before reading.
              State paths arrive as argv elements from fzf bind callbacks;
              rejecting paths outside our session directory prevents an
              attacker from pointing remotely at an arbitrary file on disk.
    """
    try:
        path.resolve().relative_to(WORK_BASE.resolve())
    except (FileNotFoundError, ValueError):
        return {}

    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _mutate_state(path: Path, fn: "Callable[[dict], None]") -> int:
    """Load the state file, apply fn(state) in-place, then save atomically.

    All three internal state commands (set, toggle, prompt) share the same
    load → mutate → save pattern. Centralising it here removes the repeated
    boilerplate and ensures every mutation goes through the atomic _save_state
    path rather than a direct write.

    Returns 0 on success, 1 if the state file is missing or fn raises.
    """
    state = _load_state(path)
    if not state:
        return 1
    try:
        fn(state)
    except Exception:
        return 1
    _save_state(path, state)
    return 0
