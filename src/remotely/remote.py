"""remotely.remote -- SSH remote search and preview sub-commands.

Public entry points
-------------------
cmd_remote_reload    Run fd (or git ls-files) on a remote host and stream
                     results to stdout. Called once at startup and on every
                     reload keystroke.

cmd_remote_preview   Preview a single remote file. Uses a two-phase bootstrap
                     to minimise data transfer:
                       1. Send SCRIPT_BOOTSTRAP (~250 bytes), which checks the
                          remote script cache at /dev/shm/remotely/<hash>.py
                          then ~/.cache/remotely/<hash>.py.
                       2. On cache miss (exit 99), upload the full script once
                          via _upload_remote_script(), then retry.
                     After the first call, all subsequent calls send only the
                     250-byte bootstrap.

Remote requirements
-------------------
Only ``python3`` and ``fd`` need to be in PATH on the remote host. No prior
installation or configuration is required beyond the automatic first-use
bootstrap. ``git`` is optional and only used when file_source="git".
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path

from ._script import _BOOTSTRAP_CACHE_MISS, SCRIPT_BOOTSTRAP, SCRIPT_BYTES, SCRIPT_HASH
from .ssh import _ssh_opts
from .utils import _parse_extensions, _shlex_join, _validate_exclude_pattern


# ---------------------------------------------------------------------------
# Remote command construction
# ---------------------------------------------------------------------------


def _build_remote_cmd(
    fd_args,
    rga_glob_args,
    query,
    base_path,
    relative,
    search_type="content",
    parents=False,
):
    # type: (List[str], List[str], str, str, bool, str, bool) -> str
    """Build a shell command string to run on the remote host via SSH.

    search_type="content" -- search inside files (uses rga/grep).
    search_type="name"    -- search for matching filenames (uses fd).
    parents=True          -- return the parent directory of each match.
    """
    safe_base = shlex.quote(base_path)
    fd_cmd = _shlex_join(fd_args)
    error_suffix = "|| { echo 'Error: cannot access directory' >&2; exit 1; }"

    # Determine if we are primarily looking for directories (to add trailing slashes)
    is_dir_search = parents or "--type d" in " ".join(fd_args)

    post_process = ""
    if is_dir_search:
        # Filter out . and .. then deduplicate and add trailing slash
        post_process = " | grep -vE '^\\.?\\.?$' | sort -u | sed 's|[^/]$|&/|'"

    if parents:
        # prepend dirname to post_process
        post_process = " | xargs -d '\\n' dirname" + post_process

    if not query:
        root = "." if relative else safe_base
        prefix = f"cd {safe_base} 2>/dev/null && " if relative else ""
        # Search '.' inside root to avoid redundancy
        cmd = f"{prefix}{fd_cmd} . {root}"
        return f"({cmd}){post_process} {error_suffix}"

    if search_type == "name":
        # Name search: query is passed to fd as a pattern
        if relative:
            cmd = f"cd {safe_base} 2>/dev/null && {fd_cmd} {shlex.quote(query)} ."
        else:
            cmd = f"{fd_cmd} {shlex.quote(query)} {safe_base}"
        return f"({cmd}){post_process} {error_suffix}"

    # Content search (default)
    grep_cmd = _shlex_join(["xargs", "-P4", "-0", "grep", "-ilF", query])
    if relative:
        rga_cmd = _shlex_join(
            ["rga"]
            + rga_glob_args
            + ["--files-with-matches", "--fixed-strings", query, "."]
        )
        fd_grep_cmd = _shlex_join(fd_args + ["-0", "."])
        cmd = (
            f"cd {safe_base} 2>/dev/null && "
            f"({rga_cmd} 2>/dev/null || {fd_grep_cmd} | {grep_cmd} 2>/dev/null)"
        )
    else:
        rga_cmd = _shlex_join(
            ["rga"]
            + rga_glob_args
            + ["--files-with-matches", "--fixed-strings", query, base_path]
        )
        fd_grep_cmd = _shlex_join(fd_args + ["-0", base_path])
        cmd = f"({rga_cmd} 2>/dev/null || {fd_grep_cmd} | {grep_cmd} 2>/dev/null)"

    return f"({cmd}){post_process} {error_suffix}"


def _build_git_remote_cmd(hidden, exclude_patterns, base_path, relative, ext=""):
    # type: (bool, List[str], str, bool, str) -> str
    """Build a ``git ls-files`` command string for the remote host."""
    safe_base = shlex.quote(base_path)

    git_args = ["git", "ls-files", "-c"]
    if hidden:
        git_args += ["--others", "--exclude-standard"]

    for pattern in exclude_patterns:
        if _validate_exclude_pattern(pattern):
            git_args += ["--exclude", shlex.quote(pattern)]

    exts = _parse_extensions(ext)
    if exts:
        git_args.append("--")
        git_args.extend(f"*.{e}" for e in exts)

    git_cmd = _shlex_join(git_args)

    if relative:
        return (
            f"cd {safe_base} 2>/dev/null && {git_cmd} "
            "|| { echo 'Error: cannot access git repository' >&2; exit 1; }"
        )

    # Absolute mode: stay out of the shell cwd and prefix the repo path to each line.
    base_prefix = base_path.rstrip("/")
    awk_cmd = _shlex_join(["awk", "-v", f"base={base_prefix}", '{print base "/" $0}'])

    git_cmd_abs = _shlex_join(["git", "-C", base_path] + git_args[1:])

    return (
        f"{git_cmd_abs} | {awk_cmd} "
        "|| { echo 'Error: cannot access git repository' >&2; exit 1; }"
    )


def _build_fd_rga_args(ftype, ext, hidden, exclude_patterns):
    # type: (str, str, bool, List[str]) -> Tuple[List[str], List[str]]
    """Build fd and rga argument lists from shared search parameters.

    Returns (fd_args, rga_glob_args) -- two parallel argument lists that
    encode the same search constraints (hidden flag, extensions, excludes)
    in the syntax each tool expects.
    """
    fd_args = ["fd", "-L"]  # type: List[str]
    if ftype and ftype != "a":
        fd_args += ["--type", ftype]

    rga_glob_args = []  # type: List[str]

    if hidden:
        fd_args.append("--hidden")
        rga_glob_args.append("--hidden")

    for ext_item in _parse_extensions(ext):
        fd_args += ["-e", ext_item]
        rga_glob_args += ["-g", f"*.{ext_item}"]

    for pattern in exclude_patterns:
        if not _validate_exclude_pattern(pattern):
            print(
                f"Warning: ignoring unsafe exclude pattern {pattern!r}",
                file=sys.stderr,
            )
            continue
        fd_args += ["-E", pattern]
        rga_glob_args += ["--exclude", pattern]

    return fd_args, rga_glob_args


# ---------------------------------------------------------------------------
# Argument parser for cmd_remote_reload
# ---------------------------------------------------------------------------


class _RemoteReloadArgs:
    """Parsed arguments for cmd_remote_reload."""

    def __init__(
        self,
        remote,
        base_path,
        ssh_control,
        ftype,
        ext,
        query="",
        hidden=False,
        relative=False,
        exclude_patterns=None,
        file_source="fd",
    ):
        self.remote = remote
        self.base_path = base_path
        self.ssh_control = ssh_control
        self.ftype = ftype
        self.ext = ext
        self.query = query
        self.hidden = hidden
        self.relative = relative
        self.exclude_patterns = exclude_patterns or []
        self.file_source = file_source


def _parse_remote_reload_args(argv):
    # type: (List[str]) -> Optional[_RemoteReloadArgs]
    """Parse argv for cmd_remote_reload. Returns None and prints usage on error."""
    if len(argv) < 5:
        print(
            "Usage: remotely-remote-reload <remote> <base_path> <ssh_control> "
            "<type> <ext> [query] [--hidden] [--relative] "
            "[--exclude <pattern> ...] [--file-source=git]",
            file=sys.stderr,
        )
        return None

    args = _RemoteReloadArgs(argv[0], argv[1], argv[2], argv[3], argv[4])
    i = 5
    while i < len(argv):
        token = argv[i]
        if token == "--hidden":
            args.hidden = True
        elif token == "--relative":
            args.relative = True
        elif token == "--exclude":
            if i + 1 < len(argv):
                args.exclude_patterns.append(argv[i + 1])
                i += 1
            else:
                print("Error: --exclude requires an argument.", file=sys.stderr)
                return None
        elif token == "--file-source=git":
            args.file_source = "git"
        elif not args.query:
            args.query = token
        i += 1
    return args


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def cmd_remote_reload(argv):
    # type: (List[str]) -> int
    """Entry point for the remotely-remote-reload sub-command.

    Runs fd or git ls-files on the remote host and streams the file list
    to stdout for fzf to consume.
    """
    args = _parse_remote_reload_args(argv)
    if args is None:
        return 1

    if args.file_source == "git" and args.ftype == "f" and not args.query:
        remote_cmd = _build_git_remote_cmd(
            args.hidden, args.exclude_patterns, args.base_path, args.relative
        )
    else:
        fd_args, rga_glob_args = _build_fd_rga_args(
            args.ftype, args.ext, args.hidden, args.exclude_patterns
        )
        remote_cmd = _build_remote_cmd(
            fd_args, rga_glob_args, args.query, args.base_path, args.relative
        )

    r = subprocess.run(
        ["ssh"] + _ssh_opts(args.ssh_control) + [args.remote, remote_cmd]
    )
    return r.returncode


# ---------------------------------------------------------------------------
# Script upload and remote preview execution
# ---------------------------------------------------------------------------


def _upload_remote_script(ssh_prefix):
    # type: (List[str]) -> bool
    """Upload the full remotely script to the remote cache in one SSH call.

    Writes to a temp file then atomically renames to the final path so the
    remote bootstrap's os.path.exists() check never sees a partial upload.

    SECURITY: SCRIPT_HASH is verified as a 16-char hex string before
    interpolation to ensure the generated shell commands cannot be injected
    via a crafted hash value.
    """
    if not SCRIPT_BYTES or not SCRIPT_HASH:
        return False

    assert re.fullmatch(r"[0-9a-f]{16}", SCRIPT_HASH), (
        f"Unexpected SCRIPT_HASH format: {SCRIPT_HASH!r}"
    )

    script_name = f"{SCRIPT_HASH}.py"  # nosemgrep: remotely-upload-cmd-unquoted-var
    script_tmp = f"{SCRIPT_HASH}.py.tmp"  # nosemgrep: remotely-upload-cmd-unquoted-var

    install_cmd = (
        "if [ -d /dev/shm ] && [ -w /dev/shm ]; then D=/dev/shm/remotely; "
        "else D=~/.cache/remotely; fi && "
        'mkdir -p "$D" && '
        f'cat > "$D/{script_tmp}" && '  # nosemgrep: remotely-upload-cmd-unquoted-var
        f'mv "$D/{script_tmp}" "$D/{script_name}" && '  # nosemgrep: remotely-upload-cmd-unquoted-var
        f'chmod 700 "$D/{script_name}"'  # nosemgrep: remotely-upload-cmd-unquoted-var
    )

    r = subprocess.run(ssh_prefix + [install_cmd], input=SCRIPT_BYTES)
    return r.returncode == 0


def _remote_preview_run(ssh_prefix, remote_cmd, capture):
    # type: (List[str], str, bool) -> Union[Tuple[int, bytes], int]
    """Execute a remote preview command via the bootstrap / upload / inline strategy.

    Three-phase execution:
      1. Send SCRIPT_BOOTSTRAP -- hit: return immediately.
      2. On cache miss (exit 99): upload the script and retry the bootstrap.
      3. If upload fails or bootstrap is unavailable: fall back to sending
         the full SCRIPT_BYTES inline (slow but always works).

    Returns (returncode, stdout_bytes) when capture=True, or just returncode
    when capture=False.
    """
    run_kwargs = (
        {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if capture else {}
    )  # type: dict

    def _run(input_bytes):
        # type: (bytes) -> subprocess.CompletedProcess
        return subprocess.run(
            ssh_prefix + [remote_cmd], input=input_bytes, **run_kwargs
        )

    def _result(r):
        # type: (subprocess.CompletedProcess) -> Union[Tuple[int, bytes], int]
        if capture:
            return r.returncode, r.stdout
        return r.returncode

    if SCRIPT_BOOTSTRAP:
        r = _run(SCRIPT_BOOTSTRAP)
        if r.returncode == 0:
            return _result(r)
        if r.returncode == _BOOTSTRAP_CACHE_MISS and _upload_remote_script(ssh_prefix):
            r = _run(SCRIPT_BOOTSTRAP)
            if r.returncode == 0:
                return _result(r)

    # Fallback: pipe the full script bytes. Slower but always available.
    r = _run(SCRIPT_BYTES)
    return _result(r)


def _cmd_remote_preview_capture(argv):
    # type: (List[str]) -> Tuple[int, bytes]
    """Capturing variant of cmd_remote_preview -- returns (returncode, stdout_bytes).

    Used by preview_cmd.py and backends.py when the caller needs to store
    the preview output in the cache before writing it to stdout.
    """
    if len(argv) < 4:
        return 1, b""
    remote, base_path, ssh_control, filename = argv[0], argv[1], argv[2], argv[3]
    query = argv[4] if len(argv) > 4 else ""

    full_path = (
        filename if Path(filename).is_absolute() else str(Path(base_path) / filename)
    )
    ssh_prefix = ["ssh"] + _ssh_opts(ssh_control) + [remote]
    args = ["remotely-preview", full_path] + ([query] if query else [])
    remote_cmd = _shlex_join(["python3", "-"] + args)

    result = _remote_preview_run(ssh_prefix, remote_cmd, capture=True)
    assert isinstance(result, tuple)
    return result


def cmd_remote_preview(argv):
    # type: (List[str]) -> int
    """Entry point for the remotely-remote-preview sub-command.

    Previews a single remote file by running the remotely-preview sub-command
    on the remote host via the bootstrap / upload / inline strategy.

    Usage:
        remotely-remote-preview <remote> <base_path> <ssh_control> <filename> [query]
    """
    if len(argv) < 4:
        print(
            "Usage: remotely-remote-preview <remote> <base_path> "
            "<ssh_control> <filename> [query]",
            file=sys.stderr,
        )
        return 1

    remote, base_path, ssh_control, filename = argv[0], argv[1], argv[2], argv[3]
    query = argv[4] if len(argv) > 4 else ""

    full_path = (
        filename if Path(filename).is_absolute() else str(Path(base_path) / filename)
    )
    ssh_prefix = ["ssh"] + _ssh_opts(ssh_control) + [remote]
    args = ["remotely-preview", full_path] + ([query] if query else [])
    remote_cmd = _shlex_join(["python3", "-"] + args)

    result = _remote_preview_run(ssh_prefix, remote_cmd, capture=False)
    assert isinstance(result, int)
    return result
