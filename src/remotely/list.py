"""remotely.list -- remotely list sub-command.

Streams file paths from one or more hosts (or the local filesystem) to
stdout and exits. No TUI, no state file, no fzf dependency.

Usage:
    remotely list [TARGET ...] [options]

    TARGET is one of:
        local               -- local filesystem (default if omitted)
        user@host           -- single remote host, searches ~/ by default
        user@host:/path     -- remote host with explicit base path
        user@host:~/path    -- remote host with tilde-expanded path
        .                   -- local filesystem, current directory (shorthand)
        /abs/path           -- local filesystem, absolute path (shorthand)
        ~/path              -- local filesystem, tilde path (shorthand)
        examples            -- local filesystem, relative path (any bare word
                               without "@" or ":" is treated as a local path)

    Multiple targets are accepted. Results from all targets are streamed
    to stdout in arrival order, each line prefixed with "host:" so that
    remotely preview and remotely open can route back to the correct host.
    Local results are not prefixed.

    SSH host strings must contain either "@" (user@host) or ":" (host:/path)
    to be distinguished from local relative directory names.

Options:
    --path PATH         Base path to search (alternative to host:/path syntax)
    --hidden            Include hidden files
    --exclude PATTERN   Exclude glob pattern (repeatable)
    --format json       Emit {"host":"...","path":"...","kind":"..."} per line
                        instead of plain paths
    --query QUERY       Search for content inside files (uses rga or grep)
    --query-type TYPE   Type of search: content (default) or name
    --type TYPE         Type of entries to list: f (files), d (dirs), a (all)
    --parents           Return the parent directory of each match

Examples:
    remotely list .
    remotely list examples
    remotely list local ~/projects
    remotely list user@host:/var/log
    remotely list host1:/var/log host2:/var/log --hidden
    remotely list user@host --format json
    remotely list . --query "todo"
    remotely list . --type d
    remotely list . --query "main" --query-type name --parents
"""

import json
import queue
import subprocess
import sys
import threading

from .archive import FileKind, classify
from .remote import _build_fd_rga_args, _build_remote_cmd
from .session import SSH_DEFERRED, acquire_socket
from .utils import _resolve_remote_path, _validate_exclude_pattern


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _is_local_path(tok: str) -> bool:
    """Return True when tok should be treated as a local filesystem path.

    Tokens that are unambiguously local paths (not SSH host strings):
      - "local"       -- explicit keyword
      - "."           -- current directory
      - "./"          -- relative path prefix
      - "/"           -- absolute path prefix
      - "~"  "~/"     -- tilde home expansion
      - no "@" and no ":"  -- bare word with no SSH indicators (e.g. "examples",
                              "logs", any relative directory name). SSH host
                              strings always contain either "@" (user@host) or
                              ":" (host:/path). A bare word like "examples" is
                              therefore always a local relative path.
    """
    return (
        tok == "local"
        or tok.startswith(".")
        or tok.startswith("/")
        or tok.startswith("~")
        or ("@" not in tok and ":" not in tok)
    )


def _parse_targets(
    argv: list,
) -> "tuple[list[dict], list[str], bool, str, str, str, str, str, bool]":
    """Parse argv into search parameters. Returns 9 values."""
    targets = []
    exclude_patterns: list = []
    hidden = False
    path_override = ""
    fmt = "plain"
    query = ""
    ftype = "f"
    qtype = "content"
    parents = False

    i = 0
    positional = []
    while i < len(argv):
        tok = argv[i]
        if tok == "--hidden":
            hidden = True
        elif tok == "--exclude":
            if i + 1 < len(argv):
                exclude_patterns.append(argv[i + 1])
                i += 1
            else:
                print("remotely list: --exclude requires an argument", file=sys.stderr)
                sys.exit(1)
        elif tok == "--path":
            if i + 1 < len(argv):
                path_override = argv[i + 1]
                i += 1
            else:
                print("remotely list: --path requires an argument", file=sys.stderr)
                sys.exit(1)
        elif tok == "--format":
            if i + 1 < len(argv):
                fmt = argv[i + 1]
                i += 1
            else:
                print("remotely list: --format requires an argument", file=sys.stderr)
                sys.exit(1)
        elif tok in ("--query", "-q"):
            if i + 1 < len(argv):
                query = argv[i + 1]
                i += 1
            else:
                print(f"remotely list: {tok} requires an argument", file=sys.stderr)
                sys.exit(1)
        elif tok == "--type":
            if i + 1 < len(argv):
                ftype = argv[i + 1]
                i += 1
            else:
                print("remotely list: --type requires an argument", file=sys.stderr)
                sys.exit(1)
        elif tok == "--query-type":
            if i + 1 < len(argv):
                qtype = argv[i + 1]
                i += 1
            else:
                print(
                    "remotely list: --query-type requires an argument", file=sys.stderr
                )
                sys.exit(1)
        elif tok == "--parents":
            parents = True
        elif tok.startswith("--"):
            print(f"remotely list: unknown option {tok}", file=sys.stderr)
            sys.exit(1)
        else:
            positional.append(tok)
        i += 1

    if not positional:
        positional = ["local"]

    for tok in positional:
        if _is_local_path(tok):
            path = path_override if tok == "local" else (path_override or tok)
            targets.append({"host": "", "path": path})
        elif ":" in tok:
            host, sep, path = tok.partition(":")
            targets.append({"host": host, "path": path_override or path})
        else:
            targets.append({"host": tok, "path": path_override})

    return (
        targets,
        exclude_patterns,
        hidden,
        path_override,
        fmt,
        query,
        ftype,
        qtype,
        parents,
    )


# ---------------------------------------------------------------------------
# Kind detection for JSON output
# ---------------------------------------------------------------------------


def _kind_for_path(path: str) -> str:
    """Return a kind string for JSON output based on the file extension."""
    kind = classify(path)
    if kind is FileKind.ARCHIVE:
        return "archive"
    if kind is FileKind.PDF:
        return "pdf"
    if kind is FileKind.DIRECTORY:
        return "directory"
    if kind is FileKind.BINARY:
        return "binary"
    return "text"


# ---------------------------------------------------------------------------
# Per-host list workers
# ---------------------------------------------------------------------------


def _list_remote(
    host: str,
    path: str,
    hidden: bool,
    exclude_patterns: list,
    out_queue: "queue.Queue",
    fmt: str,
    query: str = "",
    ftype: str = "f",
    qtype: str = "content",
    parents: bool = False,
) -> None:
    """Worker: list files on one remote host."""
    sock = acquire_socket(host)

    if not path or path.startswith("~") or not path.startswith("/"):
        ssh_control = sock if sock and sock is not SSH_DEFERRED else ""
        path = _resolve_remote_path(host, path or "", ssh_control)
        if not path:
            out_queue.put(None)
            return

    safe_patterns = [p for p in exclude_patterns if _validate_exclude_pattern(p)]
    fd_args, rga_glob_args = _build_fd_rga_args(ftype, "", hidden, safe_patterns)
    remote_cmd = _build_remote_cmd(
        fd_args,
        rga_glob_args,
        query,
        path or ".",
        relative=False,
        search_type=qtype,
        parents=parents,
    )

    if sock and sock is not SSH_DEFERRED:
        ssh_opts = [
            "-o",
            "ControlMaster=no",
            "-o",
            f"ControlPath={sock}",
            "-o",
            "ConnectTimeout=5",
        ]
    else:
        ssh_opts = ["-o", "ConnectTimeout=10"]

    proc = subprocess.Popen(
        ["ssh"] + ssh_opts + [host, remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            continue
        if fmt == "json":
            out_queue.put({"host": host, "path": line, "kind": _kind_for_path(line)})
        else:
            out_queue.put(f"{host}:{line}")

    proc.stdout.close()
    proc.wait()
    out_queue.put(None)


def _list_local(
    path: str,
    hidden: bool,
    exclude_patterns: list,
    out_queue: "queue.Queue",
    fmt: str,
    query: str = "",
    ftype: str = "f",
    qtype: str = "content",
    parents: bool = False,
) -> None:
    """Worker: list files on the local filesystem."""
    safe_patterns = [p for p in exclude_patterns if _validate_exclude_pattern(p)]
    fd_args, rga_glob_args = _build_fd_rga_args(ftype, "", hidden, safe_patterns)

    base = path or "."
    if query:
        cmd = _build_remote_cmd(
            fd_args,
            rga_glob_args,
            query,
            base,
            relative=False,
            search_type=qtype,
            parents=parents,
        )
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    else:
        proc = subprocess.Popen(
            fd_args + [".", base], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            continue
        if fmt == "json":
            out_queue.put({"host": "", "path": line, "kind": _kind_for_path(line)})
        else:
            out_queue.put(line)

    proc.stdout.close()
    proc.wait()
    out_queue.put(None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_list(argv: list) -> int:
    """Entry point for the remotely list sub-command."""
    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__, file=sys.stderr)
        return 0

    targets, exclude_patterns, hidden, _, fmt, query, ftype, qtype, parents = (
        _parse_targets(argv)
    )

    out_queue: queue.Queue = queue.Queue()
    threads = []

    for t in targets:
        host = t["host"]
        path = t["path"]
        if host:
            th = threading.Thread(
                target=_list_remote,
                args=(
                    host,
                    path,
                    hidden,
                    exclude_patterns,
                    out_queue,
                    fmt,
                    query,
                    ftype,
                    qtype,
                    parents,
                ),
                daemon=True,
            )
        else:
            th = threading.Thread(
                target=_list_local,
                args=(
                    path,
                    hidden,
                    exclude_patterns,
                    out_queue,
                    fmt,
                    query,
                    ftype,
                    qtype,
                    parents,
                ),
                daemon=True,
            )
        th.start()
        threads.append(th)

    pending = len(threads)
    stdout = sys.stdout
    while pending > 0:
        item = out_queue.get()
        if item is None:
            pending -= 1
            continue
        if fmt == "json":
            stdout.write(json.dumps(item) + "\n")
        else:
            stdout.write(item + "\n")
        try:
            stdout.flush()
        except BrokenPipeError:
            pass

    for th in threads:
        th.join()

    return 0
