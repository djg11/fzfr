"""fzfr.remote — SSH remote search and preview sub-commands.

Two public entry points:

    cmd_remote_reload   — runs fd (and optionally rga) on the remote host and
                          streams results back to local fzf as its item list.
                          Called once at startup and on every reload keystroke.

    cmd_remote_preview  — previews a single remote file. Uses a two-phase
                          bootstrap to avoid sending the full ~60 KB script on
                          every cursor movement:
                            1. Send SCRIPT_BOOTSTRAP (~200 bytes) which checks
                               whether the full script is already cached on the
                               remote at ~/.cache/fzfr/<hash>.py
                            2. On cache miss (exit 99), upload the full script
                               once via _upload_remote_script(), then retry
                          After the first preview call, all subsequent calls
                          hit the remote cache and transfer ~200 bytes instead
                          of ~60 KB.

The remote host only needs python3 and fd in its PATH. No installation or
file copying is required beyond the automatic bootstrap on first use.
"""
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .ssh import _ssh_opts
from .utils import _parse_extensions
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

    # Content search: rga preferred, fd | grep fallback.
    # PERF: -P4 parallelises grep across up to 4 workers on the remote host,
    #       matching the local grep fallback behaviour.
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


def _parse_remote_reload_args(argv: list[str]) -> "_RemoteReloadArgs | None":
    """Parse argv for cmd_remote_reload. Returns None on error."""
    if len(argv) < 5:
        print(
            "Usage: fzfr-remote-reload <remote> <base_path> <ssh_control> <type> <ext> "
            "[query] [--hidden] [--relative] [--exclude <pattern> ...]",
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
    """Build fd and rga argument lists from shared search parameters.

    Returns (fd_args, rga_glob_args). Both lists are ready to be extended
    with search root and query arguments before use.

    DESIGN: shlex.join() quotes every element so the resulting shell fragment
            is safe to embed in the remote shell command string sent over SSH.
    """
    fd_args = ["fd", "-L", "--type", ftype]
    rga_glob_args: list[str] = []
    if hidden:
        fd_args.append("--hidden")
        rga_glob_args.append("--hidden")
    for e in _parse_extensions(ext):
        fd_args += ["-e", e]
        rga_glob_args += ["-g", f"*.{e}"]
    for p in exclude_patterns:
        fd_args += ["-E", p]
        rga_glob_args += ["--exclude", p]
    return fd_args, rga_glob_args


def cmd_remote_reload(argv: list[str]) -> int:
    """Entry point for the fzfr-remote-reload sub-command.

    Unified remote reload handler. With no query it lists files via fd;
    with a query it searches file contents via rga (falling back to
    fd | xargs grep). The single entry point covers both list and search.

    Usage: fzfr fzfr-remote-reload <remote> <base_path> <ssh_control>
                                         <type> <ext> [query] [--hidden] [--relative] [--exclude <pattern> ...]
    """
    args = _parse_remote_reload_args(argv)
    if args is None:
        return 1

    fd_args, rga_glob_args = _build_fd_rga_args(
        args.ftype, args.ext, args.hidden, args.exclude_patterns
    )
    remote_cmd = _build_remote_cmd(
        fd_args, rga_glob_args, args.query, args.base_path, args.relative
    )
    r = subprocess.run(["ssh"] + _ssh_opts(args.ssh_control) + [args.remote, remote_cmd])
    return r.returncode


# DESIGN: Script-over-SSH — the entire script is piped to 'python3 -' on the
#         remote host via stdin on every preview call. The remote only needs
#         python3 in its PATH; no installation or file copying is required.
#         SCRIPT_BYTES is cached at startup (see top of file) so repeated
#         preview calls read from memory rather than disk.
# LIMITATION: The full script (~60 KB) is sent over the SSH connection on
#             every cursor movement. On high-latency links this dominates
#             preview latency; SSH multiplexing (reusing the transport TCP
#             connection) mitigates most of the overhead.


def _upload_remote_script(ssh_prefix: list[str]) -> bool:
    """Upload SCRIPT_BYTES to the remote script cache in one SSH call.

    Creates ~/.cache/fzfr/ if needed, then writes the full script to
    ~/.cache/fzfr/<SCRIPT_HASH>.py and marks it executable.

    Called exactly once per remote host per script version — when the
    bootstrap script exits with _BOOTSTRAP_CACHE_MISS (99), indicating the
    cached copy is absent (first launch or after a script upgrade).

    Returns True on success, False if the upload failed (in which case the
    caller falls back to piping the full script inline as before).
    """
    if not SCRIPT_BYTES or not SCRIPT_HASH:
        return False
    remote_cache_dir_relative = ".cache/fzfr"
    script_file_name = f"{SCRIPT_HASH}.py"
    script_file_tmp_name = f"{SCRIPT_HASH}.py.tmp"

    install_cmd = (
        f"REMOTE_HOME=$(echo ~); "
        f'REMOTE_CACHE_DIR="$REMOTE_HOME/{remote_cache_dir_relative}"; '
        f'REMOTE_SCRIPT_TMP="$REMOTE_CACHE_DIR/{script_file_tmp_name}"; '
        f'REMOTE_SCRIPT="$REMOTE_CACHE_DIR/{script_file_name}"; '
        f'mkdir -p "$REMOTE_CACHE_DIR" && '
        f'cat > "$REMOTE_SCRIPT_TMP" && '
        f'mv "$REMOTE_SCRIPT_TMP" "$REMOTE_SCRIPT" && '
        f'chmod 700 "$REMOTE_SCRIPT"'
    )
    r = subprocess.run(
        ssh_prefix + [install_cmd],
        input=SCRIPT_BYTES,
    )
    return r.returncode == 0


def _cmd_remote_preview_capture(argv: list[str]) -> tuple[int, bytes]:
    """Capturing variant of cmd_remote_preview — returns (rc, stdout_bytes).

    Uses the bootstrap/hash caching strategy (same as cmd_remote_preview) so
    the caller gets the benefit of the ~200 byte bootstrap transfer on all
    subsequent calls after the first visit. capture_output=True lets the
    caller inspect and cache the rendered output before writing it to stdout.
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

    # Try bootstrap first (fast path: ~200 bytes sent).
    if SCRIPT_BOOTSTRAP:
        r = subprocess.run(
            ssh_prefix + [remote_cmd],
            input=SCRIPT_BOOTSTRAP,
            capture_output=True,
        )
        if r.returncode != _BOOTSTRAP_CACHE_MISS:
            return r.returncode, r.stdout
        # Cache miss: upload once, then retry.
        if _upload_remote_script(ssh_prefix):
            r = subprocess.run(
                ssh_prefix + [remote_cmd],
                input=SCRIPT_BOOTSTRAP,
                capture_output=True,
            )
            return r.returncode, r.stdout

    # Fallback: send full script inline (upload failed or SCRIPT_BOOTSTRAP empty).
    r = subprocess.run(
        ssh_prefix + [remote_cmd],
        input=SCRIPT_BYTES,
        capture_output=True,
    )
    return r.returncode, r.stdout


def cmd_remote_preview(argv: list[str]) -> int:
    """Entry point for the fzfr-remote-preview sub-command.

    Generates a preview of a file on a remote host. Uses hash-based remote
    script caching to avoid piping the full ~60 KB script on every call:

      Steady state  — sends SCRIPT_BOOTSTRAP (~200 bytes). The remote finds
                      the cached script at ~/.cache/fzfr/<hash>.py and
                      executes it directly.
      First call    — bootstrap exits 99 (cache miss). The local side uploads
                      the full script once via _upload_remote_script(), then
                      retries with the bootstrap.
      Upload failed — falls back to piping SCRIPT_BYTES inline as before.

    DESIGN: The remote command string is built with shlex.join() so every
            token (path, query) is individually shell-quoted — no injection
            risk from unusual filenames or query strings.
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

    # Fast path: send only the bootstrap (~200 bytes). The bootstrap checks
    # for the cached script and execs it if present.
    if SCRIPT_BOOTSTRAP:
        r = subprocess.run(ssh_prefix + [remote_cmd], input=SCRIPT_BOOTSTRAP)
        if r.returncode != _BOOTSTRAP_CACHE_MISS:
            return r.returncode
        # Cache miss: upload the full script once, then retry with bootstrap.
        if _upload_remote_script(ssh_prefix):
            r = subprocess.run(ssh_prefix + [remote_cmd], input=SCRIPT_BOOTSTRAP)
            return r.returncode

    # Fallback: send full script inline (bootstrap empty or upload failed).
    r = subprocess.run(ssh_prefix + [remote_cmd], input=SCRIPT_BYTES)
    return r.returncode
