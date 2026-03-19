from pathlib import Path

from .state import _load_state
from .backends import backend_from_state

def cmd_dispatch(argv: list[str]) -> int:
    """Internal: dispatch preview and reload actions using the session backend.

    Reconstructs the appropriate backend (LocalBackend or RemoteBackend) from
    the persisted state file on every call. All local/remote branching lives
    inside the backend implementation; this function contains none of it.

    Usage: _internal-dispatch <state_path> <command> [args...]
    """
    if len(argv) < 2:
        return 1

    state_path, command = Path(argv[0]), argv[1]
    command_args = argv[2:]

    state = _load_state(state_path)
    if not state:
        return 1

    backend = backend_from_state(state)
    mode = state.get("mode", "content")
    ftype = state.get("ftype", "f")
    ext = state.get("ext", "")
    hidden = state.get("show_hidden", False)
    exclude_patterns = state.get("exclude_patterns", [])
    path_format = state.get("path_format", "absolute")

    if command == "preview":
        filename = command_args[0] if command_args else ""
        query = command_args[1] if len(command_args) > 1 else ""
        # DESIGN: In relative path_format mode, fzf passes a path like
        #         "./src/foo.py" which is relative to base_path, not to the
        #         cwd of the preview sub-shell. Resolve it to an absolute path
        #         here so all preview tools (bat, file, pdftotext, etc.) find
        #         the file regardless of what directory fzf spawned from.
        if filename and not Path(filename).is_absolute():
            base_path = state.get("base_path", "")
            if base_path:
                filename = str(Path(base_path) / filename.removeprefix("./"))
        return backend.preview(filename, query, mode)

    if command == "reload":
        query = command_args[0] if command_args else ""
        return backend.reload(
            query, ftype, ext, mode,
            hidden=hidden,
            exclude_patterns=exclude_patterns,
            path_format=path_format,
        )

    return 1  # Unknown command

