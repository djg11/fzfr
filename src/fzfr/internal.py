from pathlib import Path

from .config import CONFIG, _CONFIG_DEFAULTS
from .state import _load_state, _mutate_state
from .tty import _tty_prompt
from .utils import _parse_extensions

def cmd_internal_prompt(argv: list[str]) -> int:
    """Internal: prompt for input on the terminal and update the state file.

    Usage: fzfr _internal-prompt <path> <key> <prompt_text>

    LIMITATION: /dev/tty is unavailable in some environments (Docker containers
                without a TTY, certain CI runners, nested fzf sessions). When
                _tty_prompt returns None, the state update is skipped silently.
    """
    if len(argv) < 3:
        return 1
    path, key, prompt_text = Path(argv[0]), argv[1], argv[2]
    # Extension filter is meaningless in directory mode — silently no-op.
    if key == "ext":
        state = _load_state(path)
        if state.get("ftype") == "d":
            return 0
    value = _tty_prompt(prompt_text)
    if value is None:
        return 1
    return _mutate_state(path, lambda s: s.update({key: value}))


def cmd_internal_exclude(argv: list[str]) -> int:
    """Internal: prompt for a glob pattern and append it to exclude_patterns.

    Usage: fzfr _internal-exclude <state_path>

    Unlike _internal-prompt which replaces a value, this appends to the list.
    An empty input clears all runtime-added patterns back to the config default.
    """
    if not argv:
        return 1
    path = Path(argv[0])
    value = _tty_prompt("Exclude pattern (empty to clear): ")
    if value is None:
        return 1

    def _update(s: dict) -> None:
        if not value:
            # Reset to the original config-level patterns.
            s["exclude_patterns"] = list(CONFIG.get("exclude_patterns", []))
        else:
            patterns = list(s.get("exclude_patterns", []))
            if value not in patterns:
                patterns.append(value)
            s["exclude_patterns"] = patterns

    return _mutate_state(path, _update)


def _prompt_str(state: dict) -> str:
    """Return the fzf prompt string for the given state."""
    mode = state.get("mode", "content")
    ftype = state.get("ftype", "f")
    ext = state.get("ext", "")
    remote = state.get("remote", "")
    base_path = state.get("base_path", "")
    hidden = state.get("show_hidden", False)

    exclude_patterns = state.get("exclude_patterns", [])

    if ftype == "d":
        prompt_icon = "📁 Dir Name"
    elif mode == "name":
        prompt_icon = "📄 File Name"
    else:
        prompt_icon = "🔍 Content"
    # Extension filter is only meaningful for file searches, not dir mode.
    if ftype != "d":
        exts = _parse_extensions(ext)
        if exts:
            prompt_icon += f" [{','.join(exts)}]"
    if hidden:
        prompt_icon += " (incl. hidden)"
    if exclude_patterns:
        # Show a summary of excluded patterns if there are many.
        if len(exclude_patterns) > 2:
            prompt_icon += f" (excl: {exclude_patterns[0]}, ...)"
        else:
            prompt_icon += f" (excl: {', '.join(exclude_patterns)})"

    return f"{remote} [{base_path}] {prompt_icon}: " if remote else f"{prompt_icon}: "


def _header_str(state: dict) -> str:
    """Return the fzf header string for the given state."""
    mode = state.get("mode", "content")
    ftype = state.get("ftype", "f")
    hidden = state.get("show_hidden", False)
    keybindings = CONFIG.get("keybindings", {})

    def _kb(name: str) -> str:
        return keybindings.get(name, _CONFIG_DEFAULTS["keybindings"][name]).upper()

    exit_key = _kb("exit")
    toggle_key = _kb("toggle_mode")
    ftype_key = _kb("toggle_ftype")
    hidden_key = _kb("toggle_hidden")
    filter_key = _kb("filter_ext")
    exclude_key = _kb("add_exclude")
    refresh_key = _kb("refresh_list")
    sort_key = _kb("sort_list")
    copy_key = _kb("copy_path")

    # CTRL-T label: in dir mode the search is always by name, so show what
    # CTRL-T would do if the user were back in file mode.
    if ftype == "d":
        toggle_label = "Dir Name (name only)"
    else:
        toggle_label = "Content" if mode == "name" else "File Name"
    toggle_type_label = "Dirs" if ftype == "f" else "Files"
    hidden_label = "Hide" if hidden else "Show"
    filter_hint = "" if ftype == "d" else f" | {filter_key}: Filter | {exclude_key}: Exclude"
    return (
        f"{toggle_key}: {toggle_label} | {ftype_key}: {toggle_type_label}"
        f" | {hidden_key}: {hidden_label} Hidden"
        f"{filter_hint}"
        f" | {refresh_key}: Refresh | {sort_key}: Sort | {copy_key}: Copy"
        f" | Enter: Open | {exit_key}: Exit"
    )


def cmd_internal_get_prompt(argv: list[str]) -> int:
    """Internal: print the current prompt based on state (used by transform-prompt).

    Usage: fzfr _internal-get-prompt <path>
    """
    if not argv:
        return 1
    state = _load_state(Path(argv[0]))
    if not state:
        return 1
    print(_prompt_str(state), end="")
    return 0


def cmd_internal_get_header(argv: list[str]) -> int:
    """Internal: print the current header based on state (used by transform-header).

    Usage: fzfr _internal-get-header <path>
    """
    if not argv:
        return 1
    state = _load_state(Path(argv[0]))
    if not state:
        return 1
    print(_header_str(state), end="")
    return 0


def cmd_internal_get_search_action(argv: list[str]) -> int:
    """Internal: print 'disable-search' or 'enable-search' based on current mode.

    Used as the target of a fzf transform() action so fzf applies the correct
    search-filter state after a mode toggle, without needing the full transform
    action (which requires fzf >= 0.45).

    Usage: fzfr _internal-get-search-action <state_path>
    """
    if not argv:
        return 1
    state = _load_state(Path(argv[0]))
    if not state:
        return 1
    # Content mode: fzf must not re-filter the search results with its own
    # fuzzy engine — the item list IS the result. Name mode: fzf fuzzy-filters.
    print(
        (
            "disable-search"
            if state.get("mode", "content") == "content"
            else "enable-search"
        ),
        end="",
    )
    return 0


def cmd_internal_toggle_mode(argv: list[str]) -> int:
    """Internal: toggle mode between 'name' and 'content' in the state file.

    Thin wrapper around _mutate_state used as the execute-silent target for
    CTRL-T. Kept separate from _internal-toggle so the binding string is
    shorter and the operation is self-documenting.

    Usage: fzfr _internal-toggle-mode <state_path>
    """
    if not argv:
        return 1
    path = Path(argv[0])
    return _mutate_state(
        path,
        lambda s: s.update({"mode": "content" if s.get("mode") == "name" else "name"}),
    )


def cmd_internal_toggle_ftype(argv: list[str]) -> int:
    """Internal: toggle ftype between 'f' (files) and 'd' (dirs).

    DESIGN: Directory mode is always a name search and extension filters are
            meaningless for directories (fd ignores -e with --type d). So:
              - Entering dir mode (f→d): save the current mode and ext filter
                as mode_before_dir / ext_before_dir, force mode="name", and
                clear ext to "".
              - Leaving dir mode (d→f): restore mode and ext from the saved
                values, clear mode_before_dir and ext_before_dir.

            This means CTRL-D is independent of CTRL-T and CTRL-F: the user
            can have an active extension filter and content mode, press CTRL-D
            to browse directories by name, then press CTRL-D again to return
            to their previous state automatically.

    Usage: fzfr _internal-toggle-ftype <state_path>
    """
    if not argv:
        return 1
    path = Path(argv[0])

    def _toggle(s: dict) -> None:
        if s.get("ftype") == "f":
            # Entering dir mode: save current mode and ext filter, force name
            # search, and clear the extension filter — fd ignores -e with
            # --type d and would silently return no results if ext is set.
            s["mode_before_dir"] = s.get("mode", "content")
            s["ext_before_dir"] = s.get("ext", "")
            s["ftype"] = "d"
            s["mode"] = "name"
            s["ext"] = ""
        else:
            # Leaving dir mode: restore previous mode and ext filter.
            s["mode"] = s.pop("mode_before_dir", "content")
            s["ext"] = s.pop("ext_before_dir", "")
            s["ftype"] = "f"

    return _mutate_state(path, _toggle)


def cmd_internal_toggle_hidden(argv: list[str]) -> int:
    """Internal: toggle 'show_hidden' boolean in the state file.

    Usage: fzfr _internal-toggle-hidden <state_path>
    """
    if not argv:
        return 1
    path = Path(argv[0])
    return _mutate_state(
        path, lambda s: s.update({"show_hidden": not s.get("show_hidden", False)})
    )


