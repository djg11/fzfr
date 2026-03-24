"""remotely.remote -- SSH remote search and preview sub-commands.

Two public entry points:

    cmd_remote_reload   -- runs fd (or git ls-files) on the remote host and
                          streams results back to local fzf as its item list.
                          Called once at startup and on every reload keystroke.

    cmd_remote_preview  -- previews a single remote file. Uses a two-phase
                          bootstrap to avoid sending the full ~60 KB script on
                          every cursor movement:
                            1. Send SCRIPT_BOOTSTRAP (~250 bytes) which checks
                               /dev/shm/remotely/<hash>.py then ~/.cache/remotely/<hash>.py.
                            2. On cache miss (exit 99), upload the full script
                               once via _upload_remote_script(), then retry.
                          After the first preview call, all subsequent calls
                          hit the remote cache and transfer ~250 bytes.

The remote host only needs python3 and fd in its PATH. No installation or
file copying is required beyond the automatic bootstrap on first use.
git is optional -- only needed when file_source="git" is configured.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List

from ._script import _BOOTSTRAP_CACHE_MISS, SCRIPT_BOOTSTRAP, SCRIPT_BYTES, SCRIPT_HASH
from .ssh import _ssh_opts
from .utils import _parse_extensions, _shlex_join, _validate_exclude_pattern


def _build_remote_cmd(fd_args, rga_glob_args, query, base_path, relative):
    # type: (List[str], List[str], str, str, bool) -> str
    """Build the shell command string to run on the remote host via SSH.

    Returns a shell fragment safe to pass as the final argument to ssh.
    All tokens are built with shlex.join / shlex.quote so no injection
    is possible from unusual base_path, query, or extension values.

    relative=True  -- cd to base_path first; output paths are relative.
    relative=False -- pass base_path directly; output paths are absolute.
    """
    safe_base = shlex.quote(base_path)
    fd_cmd = _shlex_join(fd_args)
    grep_cmd = _shlex_join(["xargs", "-P4", "-0", "grep", "-ilF", query])
    error_suffix = "|| { echo 'Error: cannot access directory' >&2; exit 1; }"

    if not query:
        root = "." if relative else f". {safe_base}"
        prefix = f"cd {safe_base} 2>/dev/null && " if relative else ""
        return f"{prefix}{fd_cmd} {root} {error_suffix}"

    if relative:
        rga_cmd = _shlex_join(
            ["rga"]
            + rga_glob_args
            + ["--files-with-matches", "--fixed-strings", query, "."]
        )
        fd_grep_cmd = _shlex_join(fd_args + ["-0", "."])
        return (
            f"cd {safe_base} 2>/dev/null && "
            f"({rga_cmd} 2>/dev/null || {fd_grep_cmd} | {grep_cmd} 2>/dev/null)"
        )
    else:
        rga_cmd = _shlex_join(
            ["rga"]
            + rga_glob_args
            + ["--files-with-matches", "--fixed-strings", query, base_path]
        )
        fd_grep_cmd = _shlex_join(fd_args + ["-0", ".", base_path])
        return f"({rga_cmd} 2>/dev/null || {fd_grep_cmd} | {grep_cmd} 2>/dev/null)"


def _build_git_remote_cmd(hidden, exclude_patterns, base_path, relative, ext=""):
    # type: (bool, List[str], str, bool, str) -> str
    """Build a git ls-files shell command for the remote host."""
    safe_base = shlex.quote(base_path)
    git_args = ["git", "ls-files", "-c"]
    if hidden:
        git_args += ["--others", "--exclude-standard"]
    for p in exclude_patterns:
        if _validate_exclude_pattern(p):
            git_args += ["--exclude", shlex.quote(p)]
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
    awk_cmd = _shlex_join(
        ["awk", '{{print "{}/{}" $0}}'.format(base_path.rstrip("/"), "")]
    )
    return (
        f"cd {safe_base} 2>/dev/null && {git_cmd} | {awk_cmd} "
        "|| { echo 'Error: cannot access git repository' >&2; exit 1; }"
    )


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
        file_source="fd"
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
    """Parse argv for cmd_remote_reload. Returns None on error."""
    if len(argv) < 5:
        print(
            "Usage: remotely-remote-reload <remote> <base_path> <ssh_control> <type> <ext> "
            "[query] [--hidden] [--relative] [--exclude <pattern> ...] [--file-source=git]",
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


def _build_fd_rga_args(ftype, ext, hidden, exclude_patterns):
    # type: (str, str, bool, List[str]) -> Tuple[List[str], List[str]]
    """Build fd and rga argument lists from shared search parameters."""
    fd_args = ["fd", "-L", "--type", ftype]  # type: List[str]
    rga_glob_args = []  # type: List[str]
    if hidden:
        fd_args.append("--hidden")
        rga_glob_args.append("--hidden")
    for e in _parse_extensions(ext):
        fd_args += ["-e", e]
        rga_glob_args += ["-g", f"*.{e}"]
    for p in exclude_patterns:
        if not _validate_exclude_pattern(p):
            print(
                f"Warning: ignoring unsafe exclude pattern {p!r}",
                file=sys.stderr,
            )
            continue
        fd_args += ["-E", p]
        rga_glob_args += ["--exclude", p]
    return fd_args, rga_glob_args


def cmd_remote_reload(argv):
    # type: (List[str]) -> int
    """Entry point for the remotely-remote-reload sub-command."""
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


def _upload_remote_script(ssh_prefix):
    # type: (List[str]) -> bool
    """Upload SCRIPT_BYTES to the remote script cache in one SSH call."""
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
    """Run a remote preview command using the bootstrap/upload/inline strategy."""
    run_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if capture else {}  # type: dict

    def _run(input_bytes):
        # type: (bytes) -> subprocess.CompletedProcess
        return subprocess.run(
            ssh_prefix + [remote_cmd], input=input_bytes, **run_kwargs
        )

    def _emit(r):
        # type: (subprocess.CompletedProcess) -> Union[Tuple[int, bytes], int]
        if capture:
            return r.returncode, r.stdout
        return r.returncode

    if SCRIPT_BOOTSTRAP:
        r = _run(SCRIPT_BOOTSTRAP)
        if r.returncode == 0:
            return _emit(r)
        if r.returncode == _BOOTSTRAP_CACHE_MISS and _upload_remote_script(ssh_prefix):
            r = _run(SCRIPT_BOOTSTRAP)
            if r.returncode == 0:
                return _emit(r)

    r = _run(SCRIPT_BYTES)
    return _emit(r)


def _cmd_remote_preview_capture(argv):
    # type: (List[str]) -> Tuple[int, bytes]
    """Capturing variant of cmd_remote_preview -- returns (rc, stdout_bytes)."""
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
    """Entry point for the remotely-remote-preview sub-command."""
    if len(argv) < 4:
        print(
            "Usage: remotely-remote-preview <remote> <base_path> <ssh_control> <filename> [query]",
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
