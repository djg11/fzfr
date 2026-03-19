"""fzfr.remote — SSH remote search and preview sub-commands.

Two public entry points:

    cmd_remote_reload   — runs fd (or git ls-files) on the remote host and
                          streams results back to local fzf as its item list.
                          Called once at startup and on every reload keystroke.

    cmd_remote_preview  — previews a single remote file. Uses a two-phase
                          bootstrap to avoid sending the full ~60 KB script on
                          every cursor movement:
                            1. Send SCRIPT_BOOTSTRAP (~250 bytes) which checks
                               /dev/shm/fzfr/<hash>.py (RAM, preferred) then
                               ~/.cache/fzfr/<hash>.py (persistent fallback).
                            2. On cache miss (exit 99), upload the full script
                               once via _upload_remote_script(), then retry.
                          After the first preview call, all subsequent calls
                          hit the remote cache and transfer ~250 bytes.

The remote host only needs python3 and fd in its PATH. No installation or
file copying is required beyond the automatic bootstrap on first use.
git is optional — only required when file_source="git" is configured.
"""
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .ssh import _ssh_opts
from .utils import _parse_extensions, _validate_exclude_pattern
from ._script import SCRIPT_BYTES, SCRIPT_HASH, SCRIPT_BOOTSTRAP, _BOOTSTRAP_CACHE_MISS


def _build_remote_cmd(
    fd_args: list[str],
    rga_glob_args: list[str],
    query: str,
    base_path: str,
    relative: bool,
) -> str:
    """Build the shell command string to run on the remote host via SSH.

    Returns a shell fragment safe to pass as the final argument to ssh.
    All tokens are built with shlex.join / shlex.quote so no injection
    is possible from unusual base_path, query, or extension values.

    relative=True  — cd to base_path first; fd/rga run from "." so output
                     paths are relative to the search root.
    relative=False — pass base_path directly; output paths are absolute.
    """
    safe_base = shlex.quote(base_path)
    fd_cmd = shlex.join(fd_args)
    grep_cmd = shlex.join(["xargs", "-P4", "-0", "grep", "-ilF", query])

    if not query:
        if relative:
            return (
                f"cd {safe_base} 2>/dev/null && {fd_cmd} . "
                f"|| {{ echo 'Error: cannot access directory' >&2; exit 1; }}"
            )
        return (
            f"{fd_cmd} . {safe_base} "
            f"|| {{ echo 'Error: cannot access directory' >&2; exit 1; }}"
        )

    if relative:
        rga_cmd = shlex.join(
            ["rga"] + rga_glob_args
            + ["--files-with-matches", "--fixed-strings", query, "."]
        )
        fd_grep_cmd = shlex.join(fd_args + ["-0", "."])
        return (
            f"cd {safe_base} 2>/dev/null && "
            f"({rga_cmd} 2>/dev/null || {fd_grep_cmd} | {grep_cmd} 2>/dev/null)"
        )
    else:
        rga_cmd = shlex.join(
            ["rga"] + rga_glob_args
            + ["--files-with-matches", "--fixed-strings", query, base_path]
        )
        fd_grep_cmd = shlex.join(fd_args + ["-0", ".", base_path])
        return (
            f"({rga_cmd} 2>/dev/null || {fd_grep_cmd} | {grep_cmd} 2>/dev/null)"
        )


def _build_git_remote_cmd(
    hidden: bool,
    exclude_patterns: list[str],
    base_path: str,
    relative: bool,
    ext: str = "",
) -> str:
    """Build a git ls-files shell command for the remote host.

    Always runs from base_path (cd first). Output is relative to base_path
    natively; for absolute paths we pipe through awk to prepend the base.
    Extension filtering uses git pathspecs (-- '*.py') appended after --.

    DESIGN: git ls-files does not support a content search query — this
    function is only called for name-mode listing, never content search.
    """
    safe_base = shlex.quote(base_path)
    git_args = ["git", "ls-files", "-c"]
    if hidden:
        git_args += ["--others", "--exclude-standard"]
    for p in exclude_patterns:
        if not _validate_exclude_pattern(p):
            continue
        git_args += ["--exclude", shlex.quote(p)]
    exts = _parse_extensions(ext)
    if exts:
        git_args.append("--")
        for e in exts:
            git_args.append(f"*.{e}")
    git_cmd = shlex.join(git_args)

    if relative:
        return (
            f"cd {safe_base} 2>/dev/null && {git_cmd} "
            f"|| {{ echo 'Error: cannot access git repository' >&2; exit 1; }}"
        )
    # Absolute: prepend base_path to each line
    awk_cmd = shlex.join(["awk", f'{{print "{base_path.rstrip("/")}/" $0}}'])
    return (
        f"cd {safe_base} 2>/dev/null && {git_cmd} | {awk_cmd} "
        f"|| {{ echo 'Error: cannot access git repository' >&2; exit 1; }}"
    )


@dataclass
class _RemoteReloadArgs:
    """Parsed arguments for cmd_remote_reload."""
    remote: str
    base_path: str
    ssh_control: str
    ftype: str
    ext: str
    query: str = ""
    hidden: bool = False
    relative: bool = False
    exclude_patterns: list[str] = field(default_factory=list)
    file_source: str = "fd"  # "fd" or "git" — "auto" resolved locally before SSH


def _parse_remote_reload_args(argv: list[str]) -> "_RemoteReloadArgs | None":
    """Parse argv for cmd_remote_reload. Returns None on error."""
    if len(argv) < 5:
        print(
            "Usage: fzfr-remote-reload <remote> <base_path> <ssh_control> <type> <ext> "
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


def _build_fd_rga_args(
    ftype: str,
    ext: str,
    hidden: bool,
    exclude_patterns: list[str],
) -> tuple[list[str], list[str]]:
    """Build fd and rga argument lists from shared search parameters."""
    fd_args = ["fd", "-L", "--type", ftype]
    rga_glob_args: list[str] = []
    if hidden:
        fd_args.append("--hidden")
        rga_glob_args.append("--hidden")
    for e in _parse_extensions(ext):
        fd_args += ["-e", e]
        rga_glob_args += ["-g", f"*.{e}"]
    for p in exclude_patterns:
        if not _validate_exclude_pattern(p):
            print(f"Warning: ignoring unsafe exclude pattern {p!r}", file=sys.stderr)
            continue
        fd_args += ["-E", p]
        rga_glob_args += ["--exclude", p]
    return fd_args, rga_glob_args


def cmd_remote_reload(argv: list[str]) -> int:
    """Entry point for the fzfr-remote-reload sub-command.

    Unified remote reload handler. With file_source="git" uses git ls-files
    for name-mode listing. Otherwise uses fd with rga/grep fallback for
    content search. The single entry point covers both list and search.

    Usage: fzfr fzfr-remote-reload <remote> <base_path> <ssh_control>
                                    <type> <ext> [query] [--hidden]
                                    [--relative] [--exclude <pattern> ...]
                                    [--file-source=git]
    """
    args = _parse_remote_reload_args(argv)
    if args is None:
        return 1

    # git ls-files path — name mode only, no content search
    if args.file_source == "git" and args.ftype == "f" and not args.query:
        remote_cmd = _build_git_remote_cmd(
            args.hidden, args.exclude_patterns, args.base_path, args.relative
        )
        r = subprocess.run(["ssh"] + _ssh_opts(args.ssh_control) + [args.remote, remote_cmd])
        return r.returncode

    # fd / rga path (original behaviour)
    fd_args, rga_glob_args = _build_fd_rga_args(
        args.ftype, args.ext, args.hidden, args.exclude_patterns
    )
    remote_cmd = _build_remote_cmd(
        fd_args, rga_glob_args, args.query, args.base_path, args.relative
    )
    r = subprocess.run(["ssh"] + _ssh_opts(args.ssh_control) + [args.remote, remote_cmd])
    return r.returncode


def _upload_remote_script(ssh_prefix: list[str]) -> bool:
    """Upload SCRIPT_BYTES to the remote script cache in one SSH call.

    Prefers /dev/shm/fzfr/ (tmpfs, RAM-backed, nothing persists on disk after
    reboot) and falls back to ~/.cache/fzfr/ on systems where /dev/shm is
    absent or not writable (e.g. macOS, some containers). Exactly one location
    is written — never both.

    Uses atomic tmp-then-rename to avoid partial reads by a concurrent
    bootstrap call. Returns True on success, False if both locations failed
    (caller falls back to piping SCRIPT_BYTES inline).

    SECURITY: SCRIPT_HASH is asserted hex-only before interpolation.
    """
    if not SCRIPT_BYTES or not SCRIPT_HASH:
        return False
    assert re.fullmatch(r"[0-9a-f]{16}", SCRIPT_HASH), \
        f"Unexpected SCRIPT_HASH format: {SCRIPT_HASH!r}"

    script_name = f"{SCRIPT_HASH}.py"      # nosemgrep: fzfr-upload-cmd-unquoted-var
    script_tmp  = f"{SCRIPT_HASH}.py.tmp"  # nosemgrep: fzfr-upload-cmd-unquoted-var

    install_cmd = (
        f'if [ -d /dev/shm ] && [ -w /dev/shm ]; then '
        f'D=/dev/shm/fzfr; '
        f'else '
        f'D=~/.cache/fzfr; '
        f'fi && '
        f'mkdir -p "$D" && '
        f'cat > "$D/{script_tmp}" && '                  # nosemgrep: fzfr-upload-cmd-unquoted-var
        f'mv "$D/{script_tmp}" "$D/{script_name}" && '  # nosemgrep: fzfr-upload-cmd-unquoted-var
        f'chmod 700 "$D/{script_name}"'                 # nosemgrep: fzfr-upload-cmd-unquoted-var
    )
    r = subprocess.run(ssh_prefix + [install_cmd], input=SCRIPT_BYTES)
    return r.returncode == 0


def _cmd_remote_preview_capture(argv: list[str]) -> tuple[int, bytes]:
    """Capturing variant of cmd_remote_preview — returns (rc, stdout_bytes).

    Uses the bootstrap/hash caching strategy so the caller gets the benefit
    of the ~250 byte bootstrap transfer on all subsequent calls after the
    first visit. capture_output=True lets the caller inspect and cache the
    rendered output before writing it to stdout.

    HARDENING: Any non-zero rc from the bootstrap that is not
    _BOOTSTRAP_CACHE_MISS falls through to the full inline SCRIPT_BYTES path
    rather than being returned directly. This prevents a broken bootstrap from
    silently returning rc≠0 and causing cache.put() to be skipped forever
    (symptom: every cursor move re-fetches from remote even for visited files).
    """
    if len(argv) < 4:
        return 1, b""

    remote, base_path, ssh_control, filename = argv[0], argv[1], argv[2], argv[3]
    query = argv[4] if len(argv) > 4 else ""

    full_path = (
        filename if Path(filename).is_absolute() else str(Path(base_path) / filename)
    )
    ssh_prefix = ["ssh"] + _ssh_opts(ssh_control) + [remote]
    if query:
        remote_cmd = shlex.join(["python3", "-", "fzfr-preview", full_path, query])
    else:
        remote_cmd = shlex.join(["python3", "-", "fzfr-preview", full_path])

    # Phase 1: bootstrap fast path (~250 bytes sent).
    if SCRIPT_BOOTSTRAP:
        r = subprocess.run(
            ssh_prefix + [remote_cmd],
            input=SCRIPT_BOOTSTRAP,
            capture_output=True,
        )
        if r.returncode == 0:
            return 0, r.stdout
        if r.returncode == _BOOTSTRAP_CACHE_MISS:
            if _upload_remote_script(ssh_prefix):
                r = subprocess.run(
                    ssh_prefix + [remote_cmd],
                    input=SCRIPT_BOOTSTRAP,
                    capture_output=True,
                )
                if r.returncode == 0:
                    return 0, r.stdout
        # Any other rc: fall through.

    # Phase 2: inline fallback.
    r = subprocess.run(
        ssh_prefix + [remote_cmd],
        input=SCRIPT_BYTES,
        capture_output=True,
    )
    return r.returncode, r.stdout


def cmd_remote_preview(argv: list[str]) -> int:
    """Entry point for the fzfr-remote-preview sub-command.

    Generates a preview of a file on a remote host. Uses hash-based remote
    script caching to avoid piping the full ~60 KB script on every call.
    See _cmd_remote_preview_capture for the full bootstrap/fallback strategy.
    """
    if len(argv) < 4:
        print(
            "Usage: fzfr-remote-preview <remote> <base_path> <ssh_control> <filename> [query]",
            file=sys.stderr,
        )
        return 1

    remote, base_path, ssh_control, filename = argv[0], argv[1], argv[2], argv[3]
    query = argv[4] if len(argv) > 4 else ""

    full_path = (
        filename if Path(filename).is_absolute() else str(Path(base_path) / filename)
    )
    ssh_prefix = ["ssh"] + _ssh_opts(ssh_control) + [remote]
    if query:
        remote_cmd = shlex.join(["python3", "-", "fzfr-preview", full_path, query])
    else:
        remote_cmd = shlex.join(["python3", "-", "fzfr-preview", full_path])

    # Phase 1: bootstrap fast path.
    if SCRIPT_BOOTSTRAP:
        r = subprocess.run(ssh_prefix + [remote_cmd], input=SCRIPT_BOOTSTRAP)
        if r.returncode == 0:
            return 0
        if r.returncode == _BOOTSTRAP_CACHE_MISS:
            if _upload_remote_script(ssh_prefix):
                r = subprocess.run(ssh_prefix + [remote_cmd], input=SCRIPT_BOOTSTRAP)
                if r.returncode == 0:
                    return 0
        # Broken bootstrap or upload failed: fall through.

    # Phase 2: inline fallback.
    r = subprocess.run(ssh_prefix + [remote_cmd], input=SCRIPT_BYTES)
    return r.returncode
