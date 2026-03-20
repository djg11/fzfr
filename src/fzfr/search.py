"""fzfr.search — Main search UI: fzf invocation, session lifecycle, dependencies.

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
from .state import _save_state
from .backends import SearchContext, LocalBackend, RemoteBackend
from .workbase import WORK_BASE
from .utils import _capture

def _self_cmd(path: Path | str | None) -> str:
    """Return a shell-safe invocation string for the given script path.

    Produces:  python3 '/absolute/path/to/script'

    Using an absolute path ensures callbacks work regardless of PATH.
    If path is None (e.g. running via stdin over SSH), returns 'python3'
    and relies on the script being piped to the remote process.
    """
    if path is None:
        return "python3"
    return f"python3 {shlex.quote(str(path))}"


# Minimum fzf version required for the action chains used in build_fzf_invocation:
# transform-prompt, transform-header, execute-silent, disable-search (0.38+).
_FZF_MIN_VERSION = (0, 38)


def _parse_fzf_version(version_str: str) -> tuple[int, ...]:
    """Parse the first two numeric components from 'fzf --version' output.

    fzf --version emits e.g. "0.44.1" or "0.44.1 (debian)".
    Returns a tuple of ints for comparison, e.g. (0, 44).
    Returns (0, 0) if the string cannot be parsed.
    """
    m = re.match(r"(\d+)\.(\d+)", version_str.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def check_dependencies() -> None:
    """Verify that mandatory tools (fzf, fd) are installed and version-compatible.

    Also checks that fzf meets the minimum version required for the action
    chains (transform-prompt, transform-header, execute-silent, disable-search)
    used by the UI bindings. Prints a warning for missing optional tools so
    the user understands which features will be degraded.

    DESIGN: Only called for local mode. In remote mode the tools run on the
            remote host where we cannot check them without an SSH round-trip;
            remote tool absence surfaces naturally as a fallback or empty result.
    """
    mandatory = ["fzf", "fd"]
    optional = ["bat", "rga", "pdftotext", "tmux", "file"]
    # PERF: Use the module-level AVAILABLE_TOOLS cache instead of calling
    #       shutil.which() again — the results were already computed at import.
    missing_mandatory = [t for t in mandatory if t not in AVAILABLE_TOOLS]
    missing_optional = [t for t in optional if t not in AVAILABLE_TOOLS]

    if missing_mandatory:
        print(
            f"Error: Missing mandatory tools: {', '.join(missing_mandatory)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Version check: fzf must be >= _FZF_MIN_VERSION for the action chains
    # used in build_fzf_invocation (transform-prompt, execute-silent, etc.).
    # Only run if fzf was found above.
    fzf_ver_str, rc = _capture(["fzf", "--version"])
    if rc == 0:
        fzf_ver = _parse_fzf_version(fzf_ver_str)
        if fzf_ver < _FZF_MIN_VERSION:
            min_str = ".".join(str(v) for v in _FZF_MIN_VERSION)
            got_str = fzf_ver_str.strip().split()[0]
            print(
                f"Error: fzf {min_str} or later is required "
                f"(found {got_str}). "
                "Please upgrade fzf.",
                file=sys.stderr,
            )
            sys.exit(1)

    if missing_optional:
        print(
            f"Note: Optional tools not found (reduced functionality): "
            f"{', '.join(missing_optional)}",
            file=sys.stderr,
        )


def _dispatch_cmd(
    ctx: SearchContext, state_path: Path, subcommand: str, *extra: str
) -> str:
    """Return a shell-safe callback string: python3 <script> <subcommand> <state> [extra...].

    All fzf --preview, reload, and open commands are callbacks into this same
    script via the internal dispatcher. Every one of them needs the same prefix
    (python3 + script path + subcommand + state path), differing only in the
    fzf placeholders that follow. Centralising that prefix here removes the
    repeated _self_cmd / shlex.quote boilerplate from every builder.

    fzf placeholders (e.g. {}, {q}, {+}) must be passed as *extra and are
    appended verbatim — they must not be shell-quoted because fzf itself
    performs the substitution and quoting before the sub-shell sees them.
    """
    self_cmd = _self_cmd(ctx.self_path)
    safe_state = shlex.quote(str(state_path))
    parts = [self_cmd, subcommand, safe_state] + list(extra)
    return " ".join(parts)



# NOTE: _build_custom_action_binds disabled — fzf does not support
# sequential key chaining (key1+key2+key3). Needs redesign as
# mini-fzf picker. See TODO: Custom Action Trigger Redesign.
# def _build_custom_action_binds(
#     self_cmd: str,
#     safe_state: str,
#     get_header: str,
#     custom_actions: dict,
# ) -> list[str]:
#     """Generate fzf --bind strings for the custom action which-key system.
# 
#     Produces three layers of chained binds per configured action:
# 
#       Layer 1 — leader key shows group list in header:
#         leader → change-header([g] git  [f] file  [esc] cancel)
# 
#       Layer 2 — group key shows action list in header:
#         leader+g → change-header(git ›  [a] add  [r] restore  [esc] cancel)
# 
#       Layer 3 — action key runs the command and restores normal header:
#         leader+g+a → execute-silent(fzfr _internal-exec state g.a {+})
#                    + transform-header(get_header)
# 
#     ESC bindings:
#       leader+esc         → transform-header(get_header)   (cancel, restore)
#       leader+g+esc       → change-header(<group list>)    (back to groups)
# 
#     Header strings for the which-key menus are baked at session start from
#     the config — no subprocess spawned for group/action navigation. The
#     "restore normal header" path uses transform-header so it correctly
#     reflects the current mode/ext/hidden state even after CTRL-T toggles.
# 
#     fzf's native key1+key2+key3 chained bind syntax handles sequencing
#     with no timeout or platform-specific interception.
#     """
#     leader = custom_actions.get("leader", "ctrl-space")
#     groups = custom_actions.get("groups", {})
# 
#     if not groups:
#         return []
# 
#     binds = []
# 
#     # Layer 1: leader → show group list
#     group_list = "  ".join(
#         f"[{gk}] {gv['label']}" for gk, gv in sorted(groups.items())
#     )
#     leader_header = f"actions ›  {group_list}  [esc] cancel"
# 
#     binds.append(
#         f"--bind={leader}:change-header({leader_header})"
#     )
#     # ESC from leader level: restore dynamic header via transform-header
#     binds.append(
#         f"--bind={leader}+esc:transform-header({get_header})"
#     )
# 
#     # Layers 2+3: per group
#     for gk, gv in sorted(groups.items()):
#         actions = gv.get("actions", {})
#         g_label = gv.get("label", gk)
# 
#         # Layer 2: leader+group_key → show action list for this group
#         action_list = "  ".join(
#             f"[{ak}] {av['label']}" for ak, av in sorted(actions.items())
#         )
#         group_header = f"{g_label} ›  {action_list}  [esc] back"
# 
#         binds.append(
#             f"--bind={leader}+{gk}:change-header({group_header})"
#         )
#         # ESC from group level: back to group list (static — no mode info needed)
#         binds.append(
#             f"--bind={leader}+{gk}+esc:change-header({leader_header})"
#         )
# 
#         # Layer 3: leader+group_key+action_key → execute + restore header
#         for ak, av in sorted(actions.items()):
#             output = av.get("output", "silent")
#             exec_cmd = (
#                 f"{self_cmd} _internal-exec {safe_state} {gk}.{ak} {{+}}"
#             )
#             if output == "tmux":
#                 fzf_action = f"execute({exec_cmd})"
#             elif output == "preview":
#                 fzf_action = f"execute({exec_cmd})+change-preview({exec_cmd})"
#             else:  # silent
#                 fzf_action = f"execute-silent({exec_cmd})"
# 
#             binds.append(
#                 f"--bind={leader}+{gk}+{ak}:"
#                 f"{fzf_action}"
#                 f"+transform-header({get_header})"
#             )
# 
#     return binds
# 

def build_fzf_invocation(
    ctx: SearchContext,
    fzf_remote_dir: Path,
    state_path: Path,
) -> list[str]:
    """Return the complete fzf argv list for one fzfr session.

    Requires fzf >= 0.38 for the action chains used:
      execute-silent    — mutate state file without disrupting the UI
      transform-prompt  — re-read state, print new prompt string
      transform-header  — re-read state, print new header string
      transform         — re-read state, emit disable-search or enable-search
      reload            — repopulate the item list

    DESIGN notes:
      • change:reload fires on every keystroke. In content mode the dispatcher
        runs rga/grep; in name mode it runs fd and fzf fuzzy-filters the list.
        Both modes share the same reload command string; the dispatcher reads
        the state file to decide what to do.
      • {q} and {+} placeholders are left UNQUOTED. fzf shell-escapes them
        before substitution; quoting them would cause double-escaping.
      • --history path is passed as a plain string (not shlex.quote'd) because
        fzf receives it as an argv element, not a shell string.
    """
    self_cmd = _self_cmd(ctx.self_path)
    safe_state = shlex.quote(str(state_path))
    safe_self = shlex.quote(str(ctx.self_path))
    safe_ctrl = shlex.quote(ctx.ssh_control)
    keybindings = CONFIG.get("keybindings", {})

    reload_cmd = _dispatch_cmd(ctx, state_path, "_internal-dispatch", "reload", "{q}")
    preview_cmd = _dispatch_cmd(
        ctx, state_path, "_internal-dispatch", "preview", "{}", "{q}"
    )
    get_prompt = f"{self_cmd} _internal-get-prompt {safe_state}"
    get_header = f"{self_cmd} _internal-get-header {safe_state}"
    get_search = f"{self_cmd} _internal-get-search-action {safe_state}"

    # DESIGN: {q} is passed to fzfr-open so the query is written to history on
    #         every Enter press. fzf only writes --history on ESC exit, so queries
    #         that end in an open-and-continue workflow would be lost without this.
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
        """Build a fzf key binding: mutate state → update prompt/header/search → reload."""
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
        # Static display placeholders — overwritten immediately by start: chain.
        "--prompt=: ",
        "--header= ",
        # DESIGN: --history is omitted when search_history=false. Passed as a
        #         plain string (not shlex.quote'd) because fzf receives it as an
        #         argv element, not a shell string. The file stores every query
        #         typed; users can opt out to avoid logging sensitive terms.
        *([f"--history={HISTORY_PATH}"] if CONFIG.get("search_history", False) else []),
        # start: fires once on launch — sets prompt/header/search-mode then loads items.
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
        # Explicit history navigation binds so the keys are remappable.
        # fzf's --history binds ctrl-p/ctrl-n implicitly; we override them
        # here so the user can remap without losing the functionality.
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
            if (
                "xclip" in AVAILABLE_TOOLS
                or "pbcopy" in AVAILABLE_TOOLS
                or "wl-copy" in AVAILABLE_TOOLS
            )
            else []
        ),
        # Custom action leader bind — stub pending action-menu redesign.
        # TODO: replace execute target with _internal-action-menu once
        # mini-fzf picker is implemented. The which-key chained bind
        # approach (key1+key2+key3) is not supported by fzf.
        *([
            "--bind=" + CONFIG.get("custom_actions", {}).get("leader", "ctrl-b") + ":"
            "execute(" + self_cmd + " _internal-action-menu " + safe_state + " {+})"
        ] if CONFIG.get("custom_actions", {}).get("groups") else []),
    ]


def _cleanup(session_dir: Path, ssh_control: str) -> None:
    """Remove temporary files and optionally close the managed SSH master.

    The SSH master is only closed when ssh_control is non-empty, i.e. when
    config["ssh_multiplexing"] is True and fzfr created the socket
    itself. When the user relies on their own ~/.ssh/config multiplexing we
    do not touch their connection.

    Also sweeps orphaned session directories in WORK_BASE that are older than
    5 minutes (left over from crashed previous sessions).
    """
    # Only close the SSH master when we created it. If the user relies on their
    # own ~/.ssh/config multiplexing we must not touch their connection.
    if ssh_control and Path(ssh_control).exists():
        remote = os.environ.get("FZFR_REMOTE", "")
        if remote:
            subprocess.run(
                ["ssh", "-O", "exit", "-S", ssh_control, remote],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    # rmtree removes the frozen script, state file, remote-bin dir, SSH
    # socket, and any fzfr-open-* temp files created during this session.
    if session_dir.exists():
        shutil.rmtree(str(session_dir), ignore_errors=True)

    # Sweep orphaned session directories left behind by previous crashes or SIGKILL.
    # The 5-minute mtime threshold avoids racing with a concurrently running session.
    # fzfr-open-* temp files are swept by the background monitor thread instead.
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
    """Search upwards from the current directory for a .git folder.

    Returns the absolute path to the directory containing .git, or None
    if no Git repository is found.

    DESIGN: Used as the default BASE_PATH when none is supplied on the command
            line. This aligns the search root with the developer's project
            boundary rather than defaulting to cwd, which may be a deeply
            nested subdirectory with few results.
    """
    curr = Path.cwd().resolve()
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def cmd_search(argv: list[str]) -> int:
    """Entry point for the main fzfr UI.

    Sets up the environment, resolves paths, writes the initial state file,
    and runs fzf exactly once. All dynamic behaviour (mode switching, type
    toggling, extension filtering, prompt/header updates) is handled at
    runtime via fzf's transform actions — no process restart required.

    Requires fzf >= 0.38 (transform-prompt, transform-header, disable-search,
    change-header, execute-silent).

    State file (state_path):
        Written once here with the initial state; updated in place by
        transform callbacks on every CTRL-T / CTRL-D / CTRL-F. The
        _internal-dispatch preview/reload handlers read it on every fzf event.

    DESIGN: Single-script, zero-remote-install architecture — the same file
            drives the local UI, all fzf callbacks, and remote preview (via
            stdin-over-SSH).
    """
    if argv and argv[0] in ("--help", "-h"):
        print((__doc__ or "").strip())
        return 0
    if argv and argv[0] in ("--version", "-v"):
        print(f"fzfr {VERSION}")
        return 0

    exclude_patterns_cli: list[str] = []
    positional_args: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--exclude":
            if i + 1 < len(argv):
                exclude_patterns_cli.append(argv[i + 1])
                i += 1  # Skip next arg as it's the pattern
            else:
                print("Error: --exclude requires an argument.", file=sys.stderr)
                return 1
        else:
            positional_args.append(arg)
        i += 1

    target = positional_args[0] if len(positional_args) > 0 else "local"
    raw_base = positional_args[1] if len(positional_args) > 1 else ""
    mode = (
        positional_args[2]
        if len(positional_args) > 2
        else CONFIG.get("default_mode", "content")
    )

    if target == "local":
        check_dependencies()
    else:
        if "ssh" not in AVAILABLE_TOOLS:
            print(
                "Error: 'ssh' is required for remote mode but was not found in PATH.",
                file=sys.stderr,
            )
            sys.exit(1)

    # SECURITY: mkdtemp creates the directory with mode 0o700 (owner-only),
    #           preventing other local users from reading state, injecting
    #           into the frozen script, or hijacking the remote-bin directory.
    session_dir = Path(tempfile.mkdtemp(prefix="fzfr-session-", dir=str(WORK_BASE)))

    # SECURITY: Copy the running script into the private session dir. All fzf
    #           callbacks reference this frozen copy, so replacing the source
    #           file on disk after launch cannot affect this session.
    frozen_self = session_dir / "fzfr-frozen.py"
    frozen_self.write_bytes(SCRIPT_BYTES)
    frozen_self.chmod(0o700)

    # DESIGN: ssh_control is non-empty only when the user has opted in via
    #         config["ssh_multiplexing"]. Default is "" which defers all
    #         multiplexing decisions to the user's ~/.ssh/config.
    ssh_control = ""
    if target != "local" and CONFIG.get("ssh_multiplexing"):
        ssh_control = str(session_dir / "ssh.sock")
        os.environ["FZFR_REMOTE"] = target

    state_path = session_dir / "state.json"
    fzf_remote_dir = session_dir / "remote-bin"
    fzf_remote_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # DESIGN: Register cleanup via both signal handlers and atexit for
    #         defence-in-depth:
    #
    #         signal handlers — catch Ctrl-C (SIGINT) and external SIGTERM
    #             so the session dir and SSH socket are removed even when
    #             the process is killed from outside.
    #
    #         atexit — catches normal exits and unhandled Python exceptions
    #             that the signal handlers cannot intercept (e.g. an
    #             exception raised inside the try block before fzf starts).
    #             Does NOT fire on SIGKILL, but covers far more cases than
    #             signal handlers alone.
    #
    #         _cleanup() is idempotent: shutil.rmtree(ignore_errors=True) and
    #         the SSH socket close are both no-ops on a path that no longer
    #         exists, so the double-call from atexit + finally is harmless.
    #
    # LIMITATION: signal.signal() only works in the main thread. If cmd_search
    #             is ever called from a secondary thread, remove the signal
    #             registrations and rely solely on atexit.
    atexit.register(_cleanup, session_dir, ssh_control)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(
            sig,
            lambda *_: (_cleanup(session_dir, ssh_control), sys.exit(0)),
        )

    # Background thread: delete fzfr-open-* temp files from this session's
    # directory once they are older than 30 seconds. Each file is a remote
    # binary streamed for xdg-open — 30 s is enough for any application to
    # finish reading from tmpfs. Scoped to session_dir only, so concurrent
    # sessions never interfere with each other. Daemon thread so it never
    # blocks process exit; _cleanup() removes anything left on exit anyway.
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
                pass  # session_dir removed by _cleanup — thread will exit next iteration

    threading.Thread(target=_open_file_sweeper, daemon=True, name="fzfr-open-sweeper").start()

    try:
        # Build the backend — it resolves the base path and owns all local/remote
        # divergence for the rest of the session.
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
        safe_base = shlex.quote(base_path)

        # Initial state — persisted to disk so every fzf callback can
        # reconstruct the backend via backend_from_state().
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

        # DESIGN: SearchContext carries the quoted path/remote strings needed
        #         by build_fzf_invocation() to embed them into fzf bind strings.
        ctx = SearchContext(
            remote,
            safe_remote,
            base_path,
            safe_base,
            target,
            ssh_control,
            "f",
            "",
            state["exclude_patterns"],
            frozen_self,
        )

        fzf_args = build_fzf_invocation(ctx, fzf_remote_dir, state_path)

        # DESIGN: In name mode, fzf needs an initial item list to fuzzy-filter
        #         against before the user types anything. We pipe the backend's
        #         initial list command into fzf's stdin so items appear immediately.
        #
        #         In content mode the list IS the search result — nothing meaningful
        #         to show before the user types. The start: reload populates it.
        if mode == "content":
            fzf_proc = subprocess.Popen(["fzf"] + fzf_args)
            fzf_proc.wait()
        else:
            path_format = state["path_format"]
            path_format = state["path_format"]
            file_source = state.get("file_source", "auto")
            list_cmd = be.initial_list_cmd(
                frozen_self,
                hidden=state["show_hidden"],
                path_format=path_format,
                file_source=file_source,
            )
            # DESIGN: For relative local paths, or when using git ls-files,
            #         run the list command with cwd=base_path so output paths
            #         are relative to the search root. git ls-files always
            #         needs cwd=base_path to find the repo root correctly.
            list_cwd = (
                base_path
                if (not remote and (path_format == "relative" or file_source in ("auto", "git")))
                else None
            )
            list_proc = subprocess.Popen(
                list_cmd,
                stdout=subprocess.PIPE,
                cwd=list_cwd,
            )
            fzf_proc = subprocess.Popen(["fzf"] + fzf_args, stdin=list_proc.stdout)
            assert list_proc.stdout is not None
            list_proc.stdout.close()
            fzf_proc.wait()
            list_proc.wait()

    finally:
        _cleanup(session_dir, ssh_control)

    return 0
