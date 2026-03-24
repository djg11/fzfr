"""remotely.internal -- fzf callback sub-commands (_internal-*)."""

import fcntl
import os
import shlex
import signal
import struct
import subprocess
import sys
import termios
import tty
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from .config import _CONFIG_DEFAULTS, AVAILABLE_TOOLS, CONFIG
from .state import _load_state, _mutate_state
from .tty import _tty_prompt
from .utils import _parse_extensions, _resolve_absolute_path


# -- Internal sub-commands --
#
# All cmd_internal_* functions are dispatched by _internal-dispatch and called
# as fzf execute / execute-silent / transform targets. They communicate with
# the running fzf session exclusively through:
#   - the session state file  (read/write via _load_state / _mutate_state)
#   - stdout                  (fzf reads it for transform / preview targets)
#   - exit code               (fzf surfaces non-zero exits as errors)
#
# None of them take keyword arguments -- argv is always a raw list.


def cmd_internal_prompt(argv):
    # type: (List[str]) -> int
    """Prompt for input on the terminal and update one key in the state file.

    Usage: remotely _internal-prompt <state_path> <key> <prompt_text>
    """
    if len(argv) < 3:
        return 1
    path, key, prompt_text = Path(argv[0]), argv[1], argv[2]
    if key == "ext":
        state = _load_state(path)
        if state.get("ftype") == "d":
            return 0
    value = _tty_prompt(prompt_text)
    if value is None:
        return 1
    return _mutate_state(path, lambda s: s.update({key: value}))


def cmd_internal_exclude(argv):
    # type: (List[str]) -> int
    """Prompt for a glob pattern and append it to exclude_patterns in state.

    An empty input resets the list to the config-level defaults.

    Usage: remotely _internal-exclude <state_path>
    """
    if not argv:
        return 1
    path = Path(argv[0])
    value = _tty_prompt("Exclude pattern (empty to clear): ")
    if value is None:
        return 1

    def _update(s):
        # type: (dict) -> None
        if not value:
            s["exclude_patterns"] = list(CONFIG.get("exclude_patterns", []))
        else:
            patterns = list(s.get("exclude_patterns", []))
            if value not in patterns:
                patterns.append(value)
            s["exclude_patterns"] = patterns

    return _mutate_state(path, _update)


def _prompt_str(state):
    # type: (dict) -> str
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

    if ftype != "d":
        exts = _parse_extensions(ext)
        if exts:
            prompt_icon += " [{}]".format(",".join(exts))
    if hidden:
        prompt_icon += " (incl. hidden)"
    if exclude_patterns:
        if len(exclude_patterns) > 2:
            prompt_icon += " (excl: {}, ...)".format(exclude_patterns[0])
        else:
            prompt_icon += " (excl: {})".format(", ".join(exclude_patterns))

    return "{} [{}] {}: ".format(remote, base_path, prompt_icon) if remote else "{}: ".format(prompt_icon)


def _header_str(state):
    # type: (dict) -> str
    """Return the fzf header string for the given state."""
    mode = state.get("mode", "content")
    ftype = state.get("ftype", "f")
    hidden = state.get("show_hidden", False)
    keybindings = CONFIG.get("keybindings", {})

    def _kb(name):
        # type: (str) -> str
        return keybindings.get(name, _CONFIG_DEFAULTS["keybindings"][name]).upper()

    toggle_key = _kb("toggle_mode")
    ftype_key = _kb("toggle_ftype")
    hidden_key = _kb("toggle_hidden")
    filter_key = _kb("filter_ext")
    exclude_key = _kb("add_exclude")
    refresh_key = _kb("refresh_list")
    sort_key = _kb("sort_list")
    copy_key = _kb("copy_path")
    exit_key = _kb("exit")

    toggle_label = (
        "Dir Name (name only)"
        if ftype == "d"
        else ("Content" if mode == "name" else "File Name")
    )
    toggle_type_label = "Dirs" if ftype == "f" else "Files"
    hidden_label = "Hide" if hidden else "Show"
    filter_hint = (
        "" if ftype == "d" else " | {}: Filter | {}: Exclude".format(filter_key, exclude_key)
    )

    return (
        "{}: {} | {}: {}"
        " | {}: {} Hidden"
        "{}"
        " | {}: Refresh | {}: Sort | {}: Copy"
        " | Enter: Open | {}: Exit"
    ).format(
        toggle_key, toggle_label,
        ftype_key, toggle_type_label,
        hidden_key, hidden_label,
        filter_hint,
        refresh_key, sort_key, copy_key,
        exit_key,
    )


def cmd_internal_get(argv):
    # type: (List[str]) -> int
    """Print state-derived strings to stdout for fzf.

    Usage: remotely _internal-get <state_path> <prompt|header|search-action>
    """
    if len(argv) < 2:
        return 1
    path, op = Path(argv[0]), argv[1]
    state = _load_state(path)
    if not state:
        return 1

    if op == "prompt":
        print(_prompt_str(state), end="")
    elif op == "header":
        print(_header_str(state), end="")
    elif op == "search-action":
        print(
            "disable-search" if state.get("mode") == "content" else "enable-search",
            end="",
        )
    return 0


def cmd_internal_toggle(argv):
    # type: (List[str]) -> int
    """Toggle a boolean or switch between two states in the state file.

    Usage: remotely _internal-toggle <state_path> <mode|ftype|hidden>
    """
    if len(argv) < 2:
        return 1
    path, op = Path(argv[0]), argv[1]

    def _toggle(s):
        # type: (dict) -> None
        if op == "mode":
            s["mode"] = "content" if s.get("mode") == "name" else "name"
        elif op == "hidden":
            s["show_hidden"] = not s.get("show_hidden", False)
        elif op == "ftype":
            if s.get("ftype") == "f":
                s["mode_before_dir"] = s.get("mode", "content")
                s["ext_before_dir"] = s.get("ext", "")
                s["ftype"] = "d"
                s["mode"] = "name"
                s["ext"] = ""
            else:
                s["mode"] = s.pop("mode_before_dir", "content")
                s["ext"] = s.pop("ext_before_dir", "")
                s["ftype"] = "f"

    return _mutate_state(path, _toggle)


def _substitute_placeholders(cmd, path, paths, base, q):
    # type: (str, str, List[str], str, str) -> str
    """Substitute remotely placeholders in a custom action cmd string.

    Placeholders:
      {path}   -- single highlighted file, shell-quoted
      {paths}  -- all TAB-selected files, space-joined, each shell-quoted
      {dir}    -- directory containing {path}, shell-quoted
      {base}   -- search root BASE_PATH, shell-quoted
      {q}      -- current fzf query string, shell-quoted
    """
    safe_path = shlex.quote(path) if path else "''"
    subs = {
        "{paths}": " ".join(shlex.quote(p) for p in paths) if paths else safe_path,
        "{path}": safe_path,
        "{dir}": shlex.quote(os.path.dirname(path)) if path else "''",
        "{base}": shlex.quote(base) if base else "''",
        "{q}": shlex.quote(q) if q else "''",
    }
    res = cmd
    for k in ["{paths}", "{path}", "{dir}", "{base}", "{q}"]:
        res = res.replace(k, subs[k])
    return res


def cmd_internal_exec(argv, overlay_out=None):
    # type: (List[str], Optional[dict]) -> int
    """Execute a custom action identified by 'group_key.action_key'."""
    if len(argv) < 3:
        return 1

    state_path_str, action_id, selected_paths = argv[0], argv[1], argv[2:]
    parts = action_id.split(".", 1)
    if len(parts) != 2:
        return 1
    group_key, action_key = parts

    state = _load_state(Path(state_path_str))
    base_path, remote, ssh_control = (
        state.get("base_path", ""),
        state.get("remote", ""),
        state.get("ssh_control", ""),
    )

    custom_actions = CONFIG.get("custom_actions", {})
    group = custom_actions.get("groups", {}).get(group_key)
    action = group.get("actions", {}).get(action_key) if group else None
    if not action:
        return 1

    primary = _resolve_absolute_path(selected_paths[0], base_path, bool(remote)) if selected_paths else ""
    abs_paths = [_resolve_absolute_path(p, base_path, bool(remote)) for p in selected_paths]

    cmd = _substitute_placeholders(
        action["cmd"],
        path=primary,
        paths=abs_paths,
        base=base_path,
        q=state.get("last_query", ""),
    )
    output = action.get("output", "silent")

    ssh_base = (
        (["ssh", "-S", ssh_control, remote] if ssh_control else ["ssh", remote])
        if remote
        else []
    )

    if output == "tmux":
        if "tmux" in AVAILABLE_TOOLS:
            tmux_cmd = (
                " ".join(shlex.quote(a) for a in ssh_base) + " " + shlex.quote(cmd)
                if remote
                else cmd
            )
            return subprocess.run(["tmux", "new-window", tmux_cmd]).returncode
        output = "silent"

    run_kwargs = {}
    if output in ("overlay", "preview"):
        run_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    else:
        run_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}

    if remote:
        r = subprocess.run(ssh_base + [cmd], **run_kwargs)
    else:
        r = subprocess.run(cmd, shell=True, **run_kwargs)

    if output in ("overlay", "preview"):
        raw = r.stdout.decode("utf-8", errors="replace").rstrip()
        lines = raw.splitlines() if raw else ["(no output)"]
        if overlay_out is not None:
            overlay_out.update(
                {
                    "lines": lines,
                    "title": action.get("label") or action_key,
                    "position": action.get(
                        "output_position", custom_actions.get("output_position", "bottom-left")
                    ),
                }
            )
        else:
            print("\n".join(lines))
    elif r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        if err:
            print(f"[remotely] {err}", file=sys.stderr)

    return r.returncode


# -- Terminal box renderer --


class _BoxGeometry(NamedTuple):
    tty_fd: int
    start_row: int
    start_col: int
    box_h: int
    box_w: int


_TL, _TR, _BL, _BR = "\u256d", "\u256e", "\u2570", "\u256f"
_H, _V = "\u2500", "\u2502"


def _box_build(lines, title, footer):
    # type: (List[str], Optional[str], Optional[str]) -> List[str]
    """Return the list of strings that make up the box, ready to write."""
    inner = max(
        22,
        min(
            60,
            max(
                len(title or ""),
                len(footer or ""),
                max((len(ln) for ln in lines), default=0),
            )
            + 2,
        ),
    )

    def _border(text, left, right):
        # type: (Optional[str], str, str) -> str
        if not text:
            return left + _H * (inner + 2) + right
        gap = inner - len(text)
        left_pad = gap // 2
        right_pad = gap - left_pad
        return (
            left + _H + " " + _H * left_pad + text + _H * right_pad + " " + _H + right
        )

    rows = [_border(title, _TL, _TR)]
    for line in lines:
        rows.append("{}  {}{}  {}".format(_V, line, " " * (inner - len(line)), _V))
    rows.append(_border(footer, _BL, _BR))
    return rows


def _box_origin(rows, cols, box_h, box_w, position):
    # type: (int, int, int, int, str) -> Tuple[int, int]
    """Return (start_row, start_col) -- 1-indexed terminal coordinates."""
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


def _draw_box(tty_fd, lines, position="bottom-right", title=None, footer=None):
    # type: (int, List[str], str, Optional[str], Optional[str]) -> _BoxGeometry
    """Draw a bordered box at position and return its geometry for _erase_box."""
    box = _box_build(lines, title, footer)
    box_h = len(box)
    box_w = max(len(ln) for ln in box)
    term_rows, term_cols = _terminal_size(tty_fd)
    start_row, start_col = _box_origin(term_rows, term_cols, box_h, box_w, position)

    buf = "\033[s"
    for i, line in enumerate(box):
        buf += "\033[{};{}H{}".format(start_row + i, start_col, line)
    buf += "\033[u"
    os.write(tty_fd, buf.encode("utf-8"))

    return _BoxGeometry(tty_fd, start_row, start_col, box_h, box_w)


def _erase_box(geom):
    # type: (_BoxGeometry) -> None
    """Erase a box drawn by _draw_box using its returned geometry."""
    buf = "\033[s"
    for i in range(geom.box_h):
        buf += "\033[{};1H\033[2K".format(geom.start_row + i)
    buf += "\033[u"
    os.write(geom.tty_fd, buf.encode("utf-8"))


def _read_single_key(tty_fd):
    # type: (int) -> str
    """Read one keypress from an open TTY file descriptor."""
    b = os.read(tty_fd, 1)
    if not b:
        return ""
    ch = b.decode("utf-8", errors="replace")
    if ch == "\x1b":
        flags = fcntl.fcntl(tty_fd, fcntl.F_GETFL)
        fcntl.fcntl(tty_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            if os.read(tty_fd, 8):
                return ""
        except BlockingIOError:
            pass
        finally:
            fcntl.fcntl(tty_fd, fcntl.F_SETFL, flags)
        return "esc"
    return ch


def _read_any_key(tty_fd):
    # type: (int) -> None
    """Block until any single byte arrives on the TTY."""
    try:
        os.read(tty_fd, 1)
    except OSError:
        pass


def _terminal_size(tty_fd):
    # type: (int) -> Tuple[int, int]
    """Return (rows, cols) of the terminal attached to tty_fd."""
    try:
        packed = fcntl.ioctl(tty_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols = struct.unpack("HHHH", packed)[:2]
        return (rows or 24, cols or 80)
    except Exception:
        return (24, 80)


def _run_which_key_menu(tty_fd, groups, menu_pos):
    # type: (int, dict, str) -> str
    """Drive the two-level which-key navigation and return the chosen action_id."""
    while True:
        group_items = [
            "[{}] {}".format(gk, gv["label"]) for gk, gv in sorted(groups.items())
        ] + ["[q] cancel"]
        geom = _draw_box(tty_fd, group_items, menu_pos, title="actions")

        gk = _read_single_key(tty_fd)
        if not gk or gk in ("q", "esc"):
            _erase_box(geom)
            return ""
        if gk not in groups:
            _erase_box(geom)
            continue

        group = groups[gk]
        actions = group.get("actions", {})
        if not actions:
            _erase_box(geom)
            continue

        while True:
            action_items = [
                "[{}] {}".format(ak, av["label"]) for ak, av in sorted(actions.items())
            ] + ["[q] back"]
            geom = _draw_box(
                tty_fd, action_items, menu_pos, title=group.get("label", gk)
            )

            ak = _read_single_key(tty_fd)
            if not ak or ak in ("q", "esc"):
                _erase_box(geom)
                break
            if ak not in actions:
                _erase_box(geom)
                continue

            _erase_box(geom)
            return "{}.{}".format(gk, ak)


def cmd_internal_action_menu(argv):
    # type: (List[str]) -> int
    """Present the which-key action menu and execute the chosen action."""
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

    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
        old_attrs = termios.tcgetattr(tty_fd)
        tty.setraw(tty_fd)
        os.write(tty_fd, b"\033[?25l")
    except OSError:
        if fzf_pid:
            os.kill(fzf_pid, signal.SIGCONT)
        return 1

    def _restore():
        # type: () -> None
        os.write(tty_fd, b"\033[?25h")
        termios.tcsetattr(tty_fd, termios.TCSADRAIN, old_attrs)
        os.close(tty_fd)
        if fzf_pid:
            os.kill(fzf_pid, signal.SIGCONT)
            os.kill(fzf_pid, signal.SIGWINCH)

    menu_pos = custom_actions.get("menu_position", "bottom-right")
    action_id = ""

    try:
        action_id = _run_which_key_menu(tty_fd, groups, menu_pos)
    except KeyboardInterrupt:
        _restore()
        return 0
    except Exception:
        pass

    if not action_id:
        _restore()
        return 0

    overlay_out = {}  # type: dict
    rc = cmd_internal_exec(
        [state_path_str, action_id] + selected_paths,
        overlay_out=overlay_out,
    )

    if overlay_out.get("lines"):
        try:
            geom = _draw_box(
                tty_fd,
                overlay_out["lines"],
                overlay_out.get("position", "bottom-left"),
                title=overlay_out.get("title", ""),
                footer="any key to dismiss",
            )
            _read_any_key(tty_fd)
            _erase_box(geom)
        except Exception:
            pass

    _restore()
    return rc
