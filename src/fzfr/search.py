"""fzfr.search -- Main search UI: fzf invocation, session lifecycle, dependencies.

Entry point is cmd_search(). It:

  1. Parses argv, resolves the base path (git root or cwd)
  2. Constructs a LocalBackend or RemoteBackend from the parsed arguments
  3. Freezes a copy of the script into the session directory so fzf callbacks
     reference a stable path even if the source file is updated mid-session
  4. Builds the full fzf invocation (--bind, --preview, --transform strings)
     via build_fzf_invocation()
  5. Runs fd | fzf (local) or launches fzf and streams remote fd output to it
  6. Cleans up the session directory and SSH socket on exit via _cleanup().
     A background daemon thread also sweeps fzfr-open-* temp files from the
     session directory after 30 seconds during long sessions. Remote agents
     leverage /dev/shm for transience where available.

The session directory lives under WORK_BASE (usually /dev/shm/fzfr/) for
low-latency I/O. Each session gets a uuid-named subdirectory that is removed
on clean exit or swept by the next session if the previous one crashed.
"""
import atexit
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ._script import VERSION, SCRIPT_BYTES
from .config import CONFIG, _CONFIG_DEFAULTS, HISTORY_PATH, AVAILABLE_TOOLS
from .state import _save_state, _load_state
from .backends import SearchContext, LocalBackend, RemoteBackend
from .workbase import WORK_BASE
from .utils import _capture

def _self_cmd(path: Path | str | None) -> str:
    if path is None:
        return "python3"
    return f"python3 {shlex.quote(str(path))}"

_FZF_MIN_VERSION = (0, 38)

def _parse_fzf_version(version_str: str) -> tuple[int, ...]:
    m = re.match(r"(\d+)\.(\d+)", version_str.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

def check_dependencies() -> None:
    mandatory = ["fzf", "fd"]
    optional = ["bat", "rga", "pdftotext", "tmux", "file"]
    missing_mandatory = [t for t in mandatory if t not in AVAILABLE_TOOLS]
    missing_optional = [t for t in optional if t not in AVAILABLE_TOOLS]

    if missing_mandatory:
        print(f"Error: Missing mandatory tools: {', '.join(missing_mandatory)}", file=sys.stderr)
        sys.exit(1)

    fzf_ver_str, rc = _capture(["fzf", "--version"])
    if rc == 0:
        fzf_ver = _parse_fzf_version(fzf_ver_str)
        if fzf_ver < _FZF_MIN_VERSION:
            min_str = ".".join(str(v) for v in _FZF_MIN_VERSION)
            got_str = fzf_ver_str.strip().split()[0]
            print(
                f"Error: fzf {min_str} or later is required (found {got_str}). Please upgrade fzf.",
                file=sys.stderr,
            )
            sys.exit(1)

    if missing_optional:
        print(
            f"Note: Optional tools not found (reduced functionality): {', '.join(missing_optional)}",
            file=sys.stderr,
        )

def _dispatch_cmd(ctx: SearchContext, state_path: Path, subcommand: str, *extra: str) -> str:
    self_cmd = _self_cmd(ctx.self_path)
    safe_state = shlex.quote(str(state_path))
    parts = [self_cmd, subcommand, safe_state] + list(extra)
    return " ".join(parts)

def _build_custom_action_binds(
    self_cmd: str,
    safe_state: str,
    custom_actions: dict,
) -> list[str]:
    """Generate fzf --bind strings for the custom action leader.

    The which-key chained bind approach (key1+key2+key3) is not supported
    by fzf. The leader key fires execute-silent which calls
    _internal-action-menu, which uses SIGSTOP/SIGCONT to freeze fzf and
    handle the menu itself.

    Returns a single leader bind, or an empty list when no groups are configured.
    """
    leader = custom_actions.get("leader", "ctrl-b")
    groups = custom_actions.get("groups", {})
    if not groups:
        return []
    return [
        f"--bind={leader}:execute-silent({self_cmd} _internal-action-menu {safe_state} {{+}})",
    ]

def build_fzf_invocation(
    ctx: SearchContext,
    fzf_remote_dir: Path,
    state_path: Path,
) -> list[str]:
    """Return the complete fzf argv list for one fzfr session."""
    self_cmd = _self_cmd(ctx.self_path)
    safe_state = shlex.quote(str(state_path))
    safe_self = shlex.quote(str(ctx.self_path))
    safe_ctrl = shlex.quote(ctx.ssh_control)
    keybindings = CONFIG.get("keybindings", {})

    reload_cmd = _dispatch_cmd(ctx, state_path, "_internal-dispatch", "reload", "{q}")
    preview_cmd = _dispatch_cmd(ctx, state_path, "_internal-dispatch", "preview", "{}", "{q}")
    get_prompt = f"{self_cmd} _internal-get-prompt {safe_state}"
    get_header = f"{self_cmd} _internal-get-header {safe_state}"
    get_search = f"{self_cmd} _internal-get-search-action {safe_state}"

    if ctx.remote:
        open_cmd = (
            f"{self_cmd} fzfr-open {ctx.safe_remote} {ctx.safe_base} "
            f"{ctx.safe_remote} {shlex.quote(str(fzf_remote_dir))} "
            f"{safe_ctrl} {safe_state} {safe_self} {{q}} {{+}}"
        )
    else:
        open_cmd = (
            f"{self_cmd} fzfr-open local {ctx.safe_base} "
            f"'' '' '' {safe_state} {safe_self} {{q}} {{+}}"
        )

    def _toggle(action_name: str, op: str) -> str:
        key = keybindings.get(action_name, _CONFIG_DEFAULTS["keybindings"][action_name])
        mutate = f"{self_cmd} _internal-toggle-{op} {safe_state}"
        return (
            f"--bind={key}:"
            f"execute-silent({mutate})"
            f"+transform-prompt({get_prompt})"
            f"+transform-header({get_header})"
            f"+transform({get_search})"
            f"+reload({reload_cmd})"
        )

    return [
        "--prompt=: ",
        "--header= ",
        *([f"--history={HISTORY_PATH}"] if CONFIG.get("search_history", False) else []),
        (
            f"--bind=start:"
            f"transform-prompt({get_prompt})"
            f"+transform-header({get_header})"
            f"+transform({get_search})"
            f"+reload({reload_cmd})"
        ),
        f"--bind=change:reload:{reload_cmd}",
        _toggle("toggle_mode", "mode"),
        _toggle("toggle_ftype", "ftype"),
        _toggle("toggle_hidden", "hidden"),
        (
            f"--bind={keybindings.get('filter_ext', _CONFIG_DEFAULTS['keybindings']['filter_ext'])}:"
            f"execute({self_cmd} _internal-prompt {safe_state} ext 'Extension filter (empty to clear): ')"
            f"+transform-prompt({get_prompt})"
            f"+transform-header({get_header})"
            f"+reload({reload_cmd})"
        ),
        (
            f"--bind={keybindings.get('add_exclude', _CONFIG_DEFAULTS['keybindings']['add_exclude'])}:"
            f"execute({self_cmd} _internal-exclude {safe_state})"
            f"+transform-prompt({get_prompt})"
            f"+transform-header({get_header})"
            f"+reload({reload_cmd})"
        ),
        *(
            [
                f"--bind={keybindings.get('history_prev', _CONFIG_DEFAULTS['keybindings']['history_prev'])}:previous-history",
                f"--bind={keybindings.get('history_next', _CONFIG_DEFAULTS['keybindings']['history_next'])}:next-history",
            ]
            if CONFIG.get("search_history", False)
            else []
        ),
        f"--bind={keybindings.get('refresh_list', _CONFIG_DEFAULTS['keybindings']['refresh_list'])}:reload({reload_cmd})",
        f"--bind={keybindings.get('sort_list', _CONFIG_DEFAULTS['keybindings']['sort_list'])}:reload({reload_cmd} | sort)",
        f"--bind={keybindings.get('preview_half_page_down', _CONFIG_DEFAULTS['keybindings']['preview_half_page_down'])}:preview-half-page-down,"
        f"{keybindings.get('preview_half_page_up', _CONFIG_DEFAULTS['keybindings']['preview_half_page_up'])}:preview-half-page-up",
        f"--preview={preview_cmd}",
        "--preview-window=right:60%:wrap",
        "--multi",
        "--tiebreak=begin,length",
        "--ansi",
        f"--bind={keybindings.get('open_file', _CONFIG_DEFAULTS['keybindings']['open_file'])}:execute({open_cmd})",
        f"--bind={keybindings.get('exit', _CONFIG_DEFAULTS['keybindings']['exit'])}:abort",
        *(
            [
                (
                    f"--bind={keybindings.get('copy_path', _CONFIG_DEFAULTS['keybindings']['copy_path'])}:execute-silent({self_cmd} fzfr-copy {ctx.safe_remote} {ctx.safe_base} {ctx.safe_remote} {safe_ctrl} {{}})"
                    if ctx.remote
                    else f"--bind={keybindings.get('copy_path', _CONFIG_DEFAULTS['keybindings']['copy_path'])}:execute-silent({self_cmd} fzfr-copy local {ctx.safe_base} '' '' {{}})"
                )
            ]
            if "xclip" in AVAILABLE_TOOLS or "pbcopy" in AVAILABLE_TOOLS or "wl-copy" in AVAILABLE_TOOLS
            else []
        ),
        *_build_custom_action_binds(
            self_cmd=self_cmd,
            safe_state=safe_state,
            custom_actions=CONFIG.get("custom_actions", {}),
        ),
    ]


def _cleanup(session_dir: Path, ssh_control: str) -> None:
    """Remove temporary files and optionally close the managed SSH master."""
    if ssh_control and Path(ssh_control).exists():
        remote = os.environ.get("FZFR_REMOTE", "")
        if remote:
            subprocess.run(
                ["ssh", "-O", "exit", "-S", ssh_control, remote],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    if session_dir.exists():
        shutil.rmtree(str(session_dir), ignore_errors=True)
    if WORK_BASE.is_dir():
        now = time.time()
        for entry in os.scandir(str(WORK_BASE)):
            if (
                entry.is_dir()
                and entry.name.startswith("fzfr-session-")
                and now - entry.stat().st_mtime > 300
            ):
                shutil.rmtree(entry.path, ignore_errors=True)


def _find_git_root() -> str | None:
    """Search upwards from cwd for a .git folder."""
    curr = Path.cwd().resolve()
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def _parse_argv(argv: list[str]) -> tuple[str, str, str, list[str]]:
    """Parse cmd_search argv into (target, raw_base, mode, exclude_patterns).

    Separates --exclude flags from positional arguments so cmd_search stays
    focused on session setup rather than argument wrangling.
    """
    exclude_patterns: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--exclude":
            if i + 1 < len(argv):
                exclude_patterns.append(argv[i + 1])
                i += 2
            else:
                print("Error: --exclude requires an argument.", file=sys.stderr)
                sys.exit(1)
        else:
            positional.append(argv[i])
            i += 1

    target   = positional[0] if positional else "local"
    raw_base = positional[1] if len(positional) > 1 else ""
    mode     = positional[2] if len(positional) > 2 else CONFIG.get("default_mode", "content")
    return target, raw_base, mode, exclude_patterns


def cmd_search(argv: list[str]) -> int:
    """Entry point for the main fzfr UI."""
    if argv and argv[0] in ("--help", "-h"):
        print((__doc__ or "").strip())
        return 0
    if argv and argv[0] in ("--version", "-v"):
        print(f"fzfr {VERSION}")
        return 0

    target, raw_base, mode, exclude_patterns_cli = _parse_argv(argv)

    if target == "local":
        check_dependencies()
    else:
        if "ssh" not in AVAILABLE_TOOLS:
            print("Error: 'ssh' is required for remote mode but was not found in PATH.", file=sys.stderr)
            sys.exit(1)

    session_dir = Path(tempfile.mkdtemp(prefix="fzfr-session-", dir=str(WORK_BASE)))

    frozen_self = session_dir / "fzfr-frozen.py"
    frozen_self.write_bytes(SCRIPT_BYTES)
    frozen_self.chmod(0o700)

    ssh_control = ""
    if target != "local" and CONFIG.get("ssh_multiplexing"):
        ssh_control = str(session_dir / "ssh.sock")
        os.environ["FZFR_REMOTE"] = target

    state_path = session_dir / "state.json"
    fzf_remote_dir = session_dir / "remote-bin"
    fzf_remote_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    atexit.register(_cleanup, session_dir, ssh_control)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_cleanup(session_dir, ssh_control), sys.exit(0)))

    def _open_file_sweeper() -> None:
        while True:
            time.sleep(10)
            now = time.time()
            try:
                for entry in os.scandir(str(session_dir)):
                    if (
                        entry.is_file()
                        and entry.name.startswith("fzfr-open-")
                        and now - entry.stat().st_mtime > 30
                    ):
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass
            except OSError:
                pass

    threading.Thread(target=_open_file_sweeper, daemon=True, name="fzfr-open-sweeper").start()

    try:
        if target != "local":
            be: LocalBackend | RemoteBackend = RemoteBackend(target, "", ssh_control)
            base_path = be.resolve_base(raw_base)
            be.base_path = base_path
            remote = target
        else:
            be = LocalBackend("", ssh_control)
            base_path = be.resolve_base(raw_base)
            be.base_path = base_path
            remote = ""

        safe_remote = shlex.quote(remote)
        safe_base   = shlex.quote(base_path)

        state = {
            "mode": mode,
            "ftype": "f",
            "ext": "",
            "show_hidden": CONFIG.get("show_hidden", False),
            "exclude_patterns": CONFIG["exclude_patterns"] + exclude_patterns_cli,
            "target": target,
            "remote": remote,
            "base_path": base_path,
            "ssh_control": ssh_control,
            "self_path": str(frozen_self),
            "fzf_remote_dir": str(fzf_remote_dir),
            "path_format": CONFIG.get("path_format", "absolute"),
            "file_source": CONFIG.get("file_source", "auto"),
        }
        _save_state(state_path, state)

        ctx = SearchContext(
            remote, safe_remote, base_path, safe_base,
            target, ssh_control, "f", "",
            state["exclude_patterns"], frozen_self,
        )

        fzf_args = build_fzf_invocation(ctx, fzf_remote_dir, state_path)

        path_format  = state["path_format"]
        file_source  = state.get("file_source", "auto")

        if mode == "content":
            fzf_proc = subprocess.Popen(["fzf"] + fzf_args)
            _save_state(state_path, {**_load_state(state_path), "fzf_pid": fzf_proc.pid})
            fzf_proc.wait()
        else:
            list_cmd = be.initial_list_cmd(
                frozen_self,
                hidden=state["show_hidden"],
                path_format=path_format,
                file_source=file_source,
            )
            list_cwd = (
                base_path
                if (not remote and (path_format == "relative" or file_source in ("auto", "git")))
                else None
            )
            list_proc = subprocess.Popen(list_cmd, stdout=subprocess.PIPE, cwd=list_cwd)
            fzf_proc  = subprocess.Popen(["fzf"] + fzf_args, stdin=list_proc.stdout)
            assert list_proc.stdout is not None
            list_proc.stdout.close()
            _save_state(state_path, {**_load_state(state_path), "fzf_pid": fzf_proc.pid})
            fzf_proc.wait()
            list_proc.wait()

    finally:
        _cleanup(session_dir, ssh_control)

    return 0