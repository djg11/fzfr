import shlex
import subprocess
import sys
from pathlib import Path

from .config import CONFIG, _CONFIG_DEFAULTS, AVAILABLE_TOOLS
from .state import _load_state, _mutate_state
from .tty import _tty_prompt
from .utils import _parse_extensions

# ── Internal sub-commands ─────────────────────────────────────────────────────
#
# All cmd_internal_* functions are dispatched by _internal-dispatch and called
# as fzf execute / execute-silent / transform targets. They communicate with
# the running fzf session exclusively through:
#   • the session state file  (read/write via _load_state / _mutate_state)
#   • stdout                  (fzf reads it for transform / preview targets)
#   • exit code               (fzf surfaces non-zero exits as errors)
#
# None of them take keyword arguments — argv is always a raw list[str].

def cmd_internal_prompt(argv: list[str]) -> int:
    """Prompt for input on the terminal and update one key in the state file.

    Usage: fzfr _internal-prompt <state_path> <key> <prompt_text>

    LIMITATION: /dev/tty is unavailable in some environments (Docker containers
                without a TTY, certain CI runners). When _tty_prompt returns
                None the state update is skipped silently.
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
    """Prompt for a glob pattern and append it to exclude_patterns in state.

    An empty input resets the list to the config-level defaults.

    Usage: fzfr _internal-exclude <state_path>
    """
    if not argv:
        return 1
    path = Path(argv[0])
    value = _tty_prompt("Exclude pattern (empty to clear): ")
    if value is None:
        return 1

    def _update(s: dict) -> None:
        if not value:
            s["exclude_patterns"] = list(CONFIG.get("exclude_patterns", []))
        else:
            patterns = list(s.get("exclude_patterns", []))
            if value not in patterns:
                patterns.append(value)
            s["exclude_patterns"] = patterns

    return _mutate_state(path, _update)

def _prompt_str(state: dict) -> str:
    """Return the fzf prompt string for the given state."""
    mode     = state.get("mode", "content")
    ftype    = state.get("ftype", "f")
    ext      = state.get("ext", "")
    remote   = state.get("remote", "")
    base_path = state.get("base_path", "")
    hidden   = state.get("show_hidden", False)
    exclude_patterns = state.get("exclude_patterns", [])

    if ftype == "d":
        prompt_icon = "📁 Dir Name"
    elif mode == "name":
        prompt_icon = "📄 File Name"
    else:
        prompt_icon = "🔍 Content"

    if ftype != "d":
        exts = _parse_extensions(ext)
        if exts:
            prompt_icon += f" [{','.join(exts)}]"
    if hidden:
        prompt_icon += " (incl. hidden)"
    if exclude_patterns:
        if len(exclude_patterns) > 2:
            prompt_icon += f" (excl: {exclude_patterns[0]}, ...)"
        else:
            prompt_icon += f" (excl: {', '.join(exclude_patterns)})"

    return f"{remote} [{base_path}] {prompt_icon}: " if remote else f"{prompt_icon}: "

def _header_str(state: dict) -> str:
    """Return the fzf header string for the given state."""
    mode    = state.get("mode", "content")
    ftype   = state.get("ftype", "f")
    hidden  = state.get("show_hidden", False)
    keybindings = CONFIG.get("keybindings", {})

    def _kb(name: str) -> str:
        return keybindings.get(name, _CONFIG_DEFAULTS["keybindings"][name]).upper()

    toggle_key  = _kb("toggle_mode")
    ftype_key   = _kb("toggle_ftype")
    hidden_key  = _kb("toggle_hidden")
    filter_key  = _kb("filter_ext")
    exclude_key = _kb("add_exclude")
    refresh_key = _kb("refresh_list")
    sort_key    = _kb("sort_list")
    copy_key    = _kb("copy_path")
    exit_key    = _kb("exit")

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
    """Print the current prompt string to stdout (used by transform-prompt).

    Usage: fzfr _internal-get-prompt <state_path>
    """
    if not argv:
        return 1
    state = _load_state(Path(argv[0]))
    if not state:
        return 1
    print(_prompt_str(state), end="")
    return 0

def cmd_internal_get_header(argv: list[str]) -> int:
    """Print the current header string to stdout (used by transform-header).

    Usage: fzfr _internal-get-header <state_path>
    """
    if not argv:
        return 1
    state = _load_state(Path(argv[0]))
    if not state:
        return 1
    print(_header_str(state), end="")
    return 0

def cmd_internal_get_search_action(argv: list[str]) -> int:
    """Print 'disable-search' or 'enable-search' based on the current mode.

    Used as a fzf transform() target so fzf applies the correct search-filter
    state after a mode toggle without needing a full transform action.

    Content mode: fzf must not re-filter the result list — the items ARE the
    search results. Name mode: fzf fuzzy-filters the list itself.

    Usage: fzfr _internal-get-search-action <state_path>
    """
    if not argv:
        return 1
    state = _load_state(Path(argv[0]))
    if not state:
        return 1
    print(
        "disable-search" if state.get("mode", "content") == "content" else "enable-search",
        end="",
    )
    return 0

def cmd_internal_toggle_mode(argv: list[str]) -> int:
    """Toggle search mode between 'name' and 'content' in the state file.

    Usage: fzfr _internal-toggle-mode <state_path>
    """
    if not argv:
        return 1
    return _mutate_state(
        Path(argv[0]),
        lambda s: s.update({"mode": "content" if s.get("mode") == "name" else "name"}),
    )

def cmd_internal_toggle_ftype(argv: list[str]) -> int:
    """Toggle ftype between 'f' (files) and 'd' (directories) in the state file.

    DESIGN: Directory mode is always a name search — extension filters are
            meaningless for directories (fd ignores -e with --type d).

            f → d: save current mode and ext as mode_before_dir / ext_before_dir,
                   force mode="name", clear ext="".
            d → f: restore mode and ext from the saved values, clear the saves.

            This makes CTRL-D independent of CTRL-T and CTRL-F: the user can
            have an active extension filter and content mode, press CTRL-D to
            browse directories, then CTRL-D again to return to their previous
            state automatically.

    Usage: fzfr _internal-toggle-ftype <state_path>
    """
    if not argv:
        return 1

    def _toggle(s: dict) -> None:
        if s.get("ftype") == "f":
            s["mode_before_dir"] = s.get("mode", "content")
            s["ext_before_dir"]  = s.get("ext", "")
            s["ftype"] = "d"
            s["mode"]  = "name"
            s["ext"]   = ""
        else:
            s["mode"]  = s.pop("mode_before_dir", "content")
            s["ext"]   = s.pop("ext_before_dir", "")
            s["ftype"] = "f"

    return _mutate_state(Path(argv[0]), _toggle)

def cmd_internal_toggle_hidden(argv: list[str]) -> int:
    """Toggle 'show_hidden' boolean in the state file.

    Usage: fzfr _internal-toggle-hidden <state_path>
    """
    if not argv:
        return 1
    return _mutate_state(
        Path(argv[0]),
        lambda s: s.update({"show_hidden": not s.get("show_hidden", False)}),
    )

def _substitute_placeholders(cmd: str, path: str, paths: list[str], base: str, q: str) -> str:
    """Substitute fzfr placeholders in a custom action cmd string.

    All path values are shell-quoted with shlex.quote() before substitution
    so filenames containing spaces, quotes, or semicolons cannot inject shell
    commands. The cmd string itself is user-controlled (same threat model as
    ~/.bashrc).

    Placeholders:
      {path}   — single highlighted file, shell-quoted
      {paths}  — all TAB-selected files, space-joined, each shell-quoted;
                 falls back to {path} if nothing is multi-selected
      {dir}    — directory containing {path}, shell-quoted
      {base}   — search root BASE_PATH, shell-quoted
      {q}      — current fzf query string, shell-quoted

    Note: {paths} is substituted before {path} so a cmd containing both
    placeholders gets the correct value for each.
    """
    import os as _os
    safe_path  = shlex.quote(path) if path else "''"
    safe_paths = " ".join(shlex.quote(p) for p in paths) if paths else safe_path
    safe_dir   = shlex.quote(_os.path.dirname(path)) if path else "''"
    safe_base  = shlex.quote(base) if base else "''"
    safe_q     = shlex.quote(q) if q else "''"

    result = cmd
    result = result.replace("{paths}", safe_paths)   # before {path}
    result = result.replace("{path}",  safe_path)
    result = result.replace("{dir}",   safe_dir)
    result = result.replace("{base}",  safe_base)
    result = result.replace("{q}",     safe_q)
    return result

def cmd_internal_exec(argv: list[str], overlay_out: "dict | None" = None) -> int:
    """Execute a custom action identified by 'group_key.action_key'.

    Looks up the action in CONFIG["custom_actions"], resolves file paths to
    absolute, substitutes placeholders, and runs the command.

    Usage: fzfr _internal-exec <state_path> <action_id> [path ...]
      state_path  — session state file (provides base_path, query, remote info)
      action_id   — "group_key.action_key" e.g. "f.d"
      path ...    — selected file paths from fzf {+} (one or more)

    Output modes:
      "silent"   — run silently; stderr is surfaced on non-zero exit only
      "tmux"     — open a new tmux window and run the command there
      "overlay"  — run the command and return output via overlay_out for the
                   caller (cmd_internal_action_menu) to display in a box while
                   fzf is still frozen. Falls back to stdout if called directly.

    Remote execution:
      When the session has a remote host, the command is run over the existing
      SSH ControlMaster socket (no new connection). Paths are resolved against
      the remote base_path using PurePosixPath (no local filesystem access).
    """
    if len(argv) < 3:
        print(
            "Usage: fzfr _internal-exec <state_path> <action_id> [path ...]",
            file=sys.stderr,
        )
        return 1

    state_path_str, action_id = argv[0], argv[1]
    selected_paths = argv[2:]

    parts = action_id.split(".", 1)
    if len(parts) != 2:
        print(
            f"[fzfr] invalid action_id {action_id!r} — expected 'group.action'",
            file=sys.stderr,
        )
        return 1
    group_key, action_key = parts

    state       = _load_state(Path(state_path_str))
    base_path   = state.get("base_path", "")
    q           = state.get("last_query", "")
    remote      = state.get("remote", "")        # "user@host" or ""
    ssh_control = state.get("ssh_control", "")   # ControlMaster socket path

    custom_actions = CONFIG.get("custom_actions", {})
    groups = custom_actions.get("groups", {})
    group  = groups.get(group_key)
    if not group:
        print(f"[fzfr] no action group {group_key!r}", file=sys.stderr)
        return 1
    action = group.get("actions", {}).get(action_key)
    if not action:
        print(f"[fzfr] no action {action_key!r} in group {group_key!r}", file=sys.stderr)
        return 1

    # Resolve relative paths to absolute before placeholder substitution so
    # commands like `du` and `stat` work regardless of the working directory.
    # Remote paths are resolved against the remote base_path using
    # PurePosixPath — no local filesystem access.
    def _abs(p: str) -> str:
        if not p:
            return p
        from pathlib import PurePosixPath as _PP, Path as _P
        if remote:
            pp = _PP(p)
            return p if pp.is_absolute() else str(_PP(base_path) / pp) if base_path else p
        pp = _P(p)
        return p if pp.is_absolute() else str((_P(base_path) if base_path else _P.cwd()) / pp)

    primary   = _abs(selected_paths[0]) if selected_paths else ""
    abs_paths = [_abs(p) for p in selected_paths]

    cmd = _substitute_placeholders(
        action["cmd"],
        path=primary,
        paths=abs_paths,
        base=base_path,
        q=q,
    )

    output = action.get("output", "silent")

    # Wrap command for remote execution over the existing ControlMaster socket.
    if remote:
        ssh_base = ["ssh", "-S", ssh_control, remote] if ssh_control else ["ssh", remote]
        def _run(extra: dict):
            return subprocess.run(ssh_base + [cmd], **extra)
    else:
        # shell=True is intentional: cmd is a user-defined shell string that
        # may contain pipes, redirects, or shell operators (e.g. "du -sh {path}
        # | head -5"). All placeholder values ({path} etc.) are shlex.quote()'d
        # by _substitute_placeholders before reaching here.
        def _run(extra: dict):
            return subprocess.run(cmd, shell=True, **extra)  # nosemgrep: fzfr-subprocess-shell-true

    if output == "tmux":
        if "tmux" in AVAILABLE_TOOLS:
            if remote:
                import shlex as _sl
                tmux_cmd = " ".join(_sl.quote(a) for a in ssh_base) + " " + _sl.quote(cmd)
            else:
                tmux_cmd = cmd
            return subprocess.run(["tmux", "new-window", tmux_cmd]).returncode
        else:
            return _run({}).returncode

    elif output in ("overlay", "preview"):
        # Run the command and collect its output. The caller
        # (cmd_internal_action_menu) owns the TTY and displays the result
        # box while fzf is still SIGSTOPed. If called directly (no
        # overlay_out), fall back to printing to stdout.
        r = _run({"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT})
        raw = r.stdout.decode("utf-8", errors="replace").rstrip()
        lines = raw.splitlines() if raw else ["(no output)"]
        if overlay_out is not None:
            overlay_out["lines"]    = lines
            overlay_out["title"]    = action.get("label") or action_key
            overlay_out["position"] = action.get(
                "output_position",
                custom_actions.get("output_position", "bottom-left"),
            )
        else:
            print("\n".join(lines))
        return r.returncode

    else:  # "silent"
        r = _run({"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE})
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace").strip()
            if err:
                print(f"[fzfr] {err}", file=sys.stderr)
        return r.returncode

# ── Terminal box renderer ─────────────────────────────────────────────────────
#
# Draws a Unicode-bordered box directly on the terminal at any of 9 positions,
# used by the which-key menu and the "overlay" output mode. fzf is SIGSTOPed
# for the entire duration so we have exclusive terminal access.
#
# Positions:
#   top-left      top-center      top-right
#   left-center     center      right-center
#   bottom-left  bottom-center  bottom-right
#
# Box width is sized to content: min 22, max 60 inner characters.
# Title is centred in the top border; footer hint in the bottom border.
# _erase_box() uses dimensions saved by the last _draw_box() call.

_BOX_STATE: dict = {"row": 0, "col": 0, "h": 0, "w": 0, "tty_fd": -1}

_TL, _TR, _BL, _BR = "╭", "╮", "╰", "╯"
_H, _V = "─", "│"

def _box_build(lines: list[str], title: str | None, footer: str | None) -> list[str]:
    """Return the list of strings that make up the box, ready to write."""
    inner = max(22, min(60, max(
        len(title or ""),
        len(footer or ""),
        max((len(l) for l in lines), default=0),
    ) + 2))

    def _border(text: str | None, left: str, right: str) -> str:
        """Build a border line with optional centred label."""
        if not text:
            return left + _H * (inner + 2) + right
        gap       = inner - len(text)
        left_pad  = gap // 2
        right_pad = gap - left_pad
        return left + _H + " " + _H * left_pad + text + _H * right_pad + " " + _H + right

    rows = [_border(title, _TL, _TR)]
    for line in lines:
        rows.append(f"{_V}  {line}{' ' * (inner - len(line))}  {_V}")
    rows.append(_border(footer, _BL, _BR))
    return rows

def _box_origin(rows: int, cols: int, box_h: int, box_w: int, position: str) -> tuple[int, int]:
    """Return (start_row, start_col) — 1-indexed terminal coordinates."""
    if position == "top-left":
        return (1, 1)
    elif position == "top-center":
        return (1, max(1, (cols - box_w) // 2))
    elif position == "top-right":
        return (1, max(1, cols - box_w + 1))
    elif position == "left-center":
        return (max(1, (rows - box_h) // 2), 1)
    elif position == "center":
        return (max(1, (rows - box_h) // 2), max(1, (cols - box_w) // 2))
    elif position == "right-center":
        return (max(1, (rows - box_h) // 2), max(1, cols - box_w + 1))
    elif position == "bottom-left":
        return (max(1, rows - box_h + 1), 1)
    elif position == "bottom-center":
        return (max(1, rows - box_h + 1), max(1, (cols - box_w) // 2))
    else:  # "bottom-right" (default / fallback)
        return (max(1, rows - box_h + 1), max(1, cols - box_w + 1))

def _draw_box(
    tty_fd: int,
    lines: list[str],
    position: str = "bottom-right",
    title: str | None = None,
    footer: str | None = None,
) -> None:
    """Draw a bordered box at position and save its geometry for _erase_box."""
    import os
    box   = _box_build(lines, title, footer)
    box_h = len(box)
    box_w = max(len(l) for l in box)
    term_rows, term_cols = _terminal_size(tty_fd)
    start_row, start_col = _box_origin(term_rows, term_cols, box_h, box_w, position)

    _BOX_STATE.update({"row": start_row, "col": start_col,
                        "h": box_h, "w": box_w, "tty_fd": tty_fd})

    # \033[s — save cursor; position each line; \033[u — restore cursor
    buf = "\033[s"
    for i, line in enumerate(box):
        buf += f"\033[{start_row + i};{start_col}H{line}"
    buf += "\033[u"
    os.write(tty_fd, buf.encode("utf-8"))

def _erase_box() -> None:
    """Erase the last box drawn by _draw_box using its saved geometry."""
    import os
    tty_fd = _BOX_STATE["tty_fd"]
    if tty_fd < 0:
        return
    start_row = _BOX_STATE["row"]
    box_h     = _BOX_STATE["h"]
    buf = "\033[s"
    for i in range(box_h):
        buf += f"\033[{start_row + i};1H\033[2K"   # move to row, erase whole line
    buf += "\033[u"
    os.write(tty_fd, buf.encode("utf-8"))
    _BOX_STATE["tty_fd"] = -1

# ── TTY key reading ───────────────────────────────────────────────────────────

def _read_single_key(tty_fd: int) -> str:
    """Read one keypress from an open TTY file descriptor.

    Returns the character as a string, or "" for unrecognised input.
    A bare ESC is returned as "esc"; multi-byte escape sequences (arrow
    keys, function keys) are consumed and discarded — callers that need
    'any key' semantics should use _read_any_key() instead.

    DESIGN: Read one byte. If it is ESC (0x1b), do a non-blocking peek:
    a bare ESC key sends exactly 0x1b with nothing following; an escape
    sequence sends 0x1b immediately followed by more bytes. Discard the
    full sequence and return "" so the menu loop treats it as a no-op.
    """
    import os, fcntl
    b = os.read(tty_fd, 1)
    if not b:
        return ""
    ch = b.decode("utf-8", errors="replace")
    if ch == "\x1b":
        flags = fcntl.fcntl(tty_fd, fcntl.F_GETFL)
        fcntl.fcntl(tty_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            if os.read(tty_fd, 8):
                return ""   # escape sequence — discard
        except BlockingIOError:
            pass            # bare ESC — nothing followed
        finally:
            fcntl.fcntl(tty_fd, fcntl.F_SETFL, flags)
        return "esc"
    return ch

def _read_any_key(tty_fd: int) -> None:
    """Block until any single byte arrives on the TTY.

    Used for 'press any key to dismiss' prompts. Unlike _read_single_key,
    this does not inspect or discard escape sequences — the first byte of
    any keypress (including arrow keys and function keys) is sufficient.
    """
    import os
    try:
        os.read(tty_fd, 1)
    except OSError:
        pass

def _terminal_size(tty_fd: int) -> tuple[int, int]:
    """Return (rows, cols) of the terminal attached to tty_fd.

    Falls back to (24, 80) if the ioctl fails (e.g. in a pipe or test).
    """
    import struct, fcntl, termios as _t
    try:
        packed = fcntl.ioctl(tty_fd, _t.TIOCGWINSZ, b"\x00" * 8)
        rows, cols = struct.unpack("HHHH", packed)[:2]
        return (rows or 24, cols or 80)
    except Exception:
        return (24, 80)

# ── Which-key action menu ─────────────────────────────────────────────────────

def cmd_internal_action_menu(argv: list[str]) -> int:
    """Present the which-key action menu and execute the chosen action.

    Called by fzf via execute-silent when the user presses the leader key.

    DESIGN — SIGSTOP/SIGCONT approach:
      fzf is started with subprocess.Popen; its PID is saved to the state
      file immediately after launch. When the leader key fires, this function
      sends SIGSTOP to fzf as its very first operation — before argv checks,
      config access, or TTY setup. fzf is frozen before it can react.

      With fzf frozen we take exclusive ownership of /dev/tty, hide the
      cursor, draw the which-key box, and read keypresses. On exit we show
      the cursor, restore terminal attrs, SIGCONT fzf, and send SIGWINCH to
      trigger a clean redraw.

      execute-silent (not execute) is used for the leader bind so fzf does
      not issue its own redraw when our subprocess exits.

    Flow:
      1. SIGSTOP fzf   — freeze before anything else
      2. Open /dev/tty, setraw, hide cursor
      3. Draw group menu box at menu_position
      4. Keypress → group key  (q / ESC → cancel)
      5. Draw action menu box at menu_position
      6. Keypress → action key (q / ESC → back to step 3)
      7. Erase box
      8. Run action via cmd_internal_exec
      9. For overlay output: draw result box, wait for any key, erase box
     10. Show cursor, restore terminal, SIGCONT fzf, SIGWINCH fzf

    Usage: fzfr _internal-action-menu <state_path> [path ...]
    """
    import os, signal, termios, tty as _tty

    # ── Step 1: freeze fzf immediately ───────────────────────────────────────
    # Must be the very first operation. execute-silent is asynchronous —
    # fzf continues running until we SIGSTOP it.
    fzf_pid = None
    if argv:
        try:
            fzf_pid = _load_state(Path(argv[0])).get("fzf_pid")
            if fzf_pid:
                os.kill(fzf_pid, signal.SIGSTOP)
        except Exception:
            fzf_pid = None

    if not argv:
        if fzf_pid:
            os.kill(fzf_pid, signal.SIGCONT)
        return 1

    state_path_str = argv[0]
    selected_paths = argv[1:]

    custom_actions = CONFIG.get("custom_actions", {})
    groups = custom_actions.get("groups", {})
    if not groups:
        if fzf_pid:
            os.kill(fzf_pid, signal.SIGCONT)
        return 0

    # ── Step 2: take over the terminal ────────────────────────────────────────
    try:
        tty_fd    = os.open("/dev/tty", os.O_RDWR)
        old_attrs = termios.tcgetattr(tty_fd)
        _tty.setraw(tty_fd)
        os.write(tty_fd, b"\033[?25l")   # hide cursor
    except OSError:
        if fzf_pid:
            os.kill(fzf_pid, signal.SIGCONT)
        return 1

    def _restore() -> None:
        """Show cursor, restore terminal attrs, unfreeze fzf."""
        os.write(tty_fd, b"\033[?25h")   # show cursor
        termios.tcsetattr(tty_fd, termios.TCSADRAIN, old_attrs)
        os.close(tty_fd)
        if fzf_pid:
            os.kill(fzf_pid, signal.SIGCONT)
            os.kill(fzf_pid, signal.SIGWINCH)  # force fzf to redraw cleanly

    menu_pos = custom_actions.get("menu_position", "bottom-right")

    # ── Steps 3–7: which-key navigation ──────────────────────────────────────
    action_id = ""
    try:
        while True:
            # Level 1 — group menu
            group_items = [
                f"[{gk}] {gv['label']}" for gk, gv in sorted(groups.items())
            ] + ["[q] cancel"]
            _draw_box(tty_fd, group_items, menu_pos, title="actions")

            gk = _read_single_key(tty_fd)
            if not gk or gk in ("q", "esc"):
                _erase_box()
                break
            if gk not in groups:
                continue

            group   = groups[gk]
            actions = group.get("actions", {})
            if not actions:
                continue

            # Level 2 — action menu
            while True:
                action_items = [
                    f"[{ak}] {av['label']}" for ak, av in sorted(actions.items())
                ] + ["[q] back"]
                _draw_box(tty_fd, action_items, menu_pos, title=group.get("label", gk))

                ak = _read_single_key(tty_fd)
                if not ak or ak in ("q", "esc"):
                    break
                if ak not in actions:
                    continue

                _erase_box()
                action_id = f"{gk}.{ak}"
                break

            if action_id:
                break

    except Exception:
        pass

    if not action_id:
        _restore()
        return 0

    # ── Steps 8–9: run action, show overlay if needed ────────────────────────
    overlay_out: dict = {}
    rc = cmd_internal_exec(
        [state_path_str, action_id] + selected_paths,
        overlay_out=overlay_out,
    )

    if overlay_out.get("lines"):
        try:
            _draw_box(
                tty_fd,
                overlay_out["lines"],
                overlay_out.get("position", "bottom-left"),
                title=overlay_out.get("title", ""),
                footer="any key to dismiss",
            )
            _read_any_key(tty_fd)
            _erase_box()
        except Exception:
            pass

    # ── Step 10: hand control back to fzf ────────────────────────────────────
    _restore()
    return rc
