"""remotely.state -- Session state file load, save, and mutation.

Session state is a small JSON dict stored at session_dir/state.json. It holds
the current mode, extension filter, path format, and other per-session settings
that fzf callbacks need to reconstruct context across subprocess boundaries.

All writes go through _save_state() which uses an atomic rename (write to
.tmp then replace) so a concurrent reader never sees a partial file.

All reads go through _load_state() which validates that the path is inside
WORK_BASE before deserialising. State paths arrive as argv elements from fzf
bind callbacks and could be attacker-controlled; boundary enforcement here
prevents directory traversal to arbitrary files on disk.
"""

import json
from collections.abc import Callable
from pathlib import Path

from .workbase import WORK_BASE


def _save_state(path: Path, state: dict) -> None:
    """Atomically write state to path as JSON.

    Writes to ``path.with_suffix(".tmp")`` first, then renames to path so
    concurrent readers never observe an incomplete file.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _load_state(path: Path) -> dict:
    """Read and deserialise the JSON state file at path.

    SECURITY: Resolves path and verifies it is inside WORK_BASE before
    reading. State paths come from fzf argv and could be attacker-supplied;
    this check prevents them from pointing at arbitrary files on disk.

    Returns an empty dict if the file is missing, malformed, or outside
    WORK_BASE -- callers treat an empty dict as "no state".
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
    """Load the state file, apply fn(state) in place, then save atomically.

    All three internal state-update commands (set, toggle, prompt) share the
    same load -> mutate -> save pattern. Centralising it here ensures every
    mutation goes through the atomic _save_state() path and removes repeated
    boilerplate from each command handler.

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
