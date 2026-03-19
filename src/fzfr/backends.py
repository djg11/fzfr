"""fzfr.backends — Backend protocol and implementations for local and remote search.

The Backend protocol abstracts the difference between searching a local filesystem
and an SSH-remote one. All fzf callbacks (preview, reload, open) go through the
backend so the rest of the code has no if-remote branches.

Three concrete types:

    SearchContext   — immutable connection parameters (host, base path, ssh socket)
    LocalBackend    — operations on the local filesystem via fd, bat, rga, etc.
    RemoteBackend   — same operations forwarded over SSH; streams the script to the
                      remote python3 interpreter for preview sub-commands

The circular-import problem: LocalBackend.preview() calls cmd_preview, and
RemoteBackend calls into remote.py and copy.py. Those modules import from this
one. The cycle is broken at runtime with try/except ImportError late imports;
the built single-file script has no packages so the except branch fires and
pulls the function from globals() instead.
"""
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .cache import _PreviewCache
from .config import AVAILABLE_TOOLS
from .ssh import _ssh_opts
from .utils import _capture, _get_mime, _parse_extensions

@dataclass
class SearchContext:
    """Consolidates path and connection state for a fzfr session."""

    remote: str  # SSH host string, e.g. "user@host", or "" for local
    safe_remote: str  # shlex.quote(remote)
    base_path: str  # absolute base directory to search
    safe_base: str  # shlex.quote(base_path)
    target: str = "local"
    # ControlPath socket managed by fzfr, or "" to rely on ~/.ssh/config.
    # Non-empty only when config["ssh_multiplexing"] is True.
    ssh_control: str = field(default="")
    ftype: str = "f"  # "f" for files, "d" for directories
    ext: str = ""  # file extension filter, e.g. "py"
    exclude_patterns: list[str] = field(default_factory=list)  # exclude patterns
    self_path: Path | None = None  # path to the script (original or frozen)


# =============================================================================
# Backend abstraction  (LocalBackend / RemoteBackend)
# =============================================================================
#
# Almost every action in fzfr has a local variant and a remote variant.
# Previously this split lived as inline `if ctx.remote:` branches scattered
# across cmd_dispatch and _open.
#
# The Backend Protocol centralises the divergence into two concrete classes.
# Call sites ask the backend what to do; they never inspect ctx.remote directly.
#
# The backend instance is constructed once in cmd_search (from SearchContext)
# and re-constructed from the state dict in cmd_dispatch — exactly the places
# where we already know whether the session is local or remote.


@runtime_checkable
class Backend(Protocol):
    """Operations that differ between local-filesystem and SSH-remote sessions."""

    base_path: str  # absolute search root for this session
    ssh_control: str  # ControlPath socket, or "" to defer to ~/.ssh/config

    def resolve_base(self, raw: str) -> str:
        """Expand a raw path argument to an absolute path for this backend.

        For local backends, resolves relative paths and ~ via pathlib.
        For remote backends, asks the remote shell to expand ~ and pwd.
        """
        ...

    def is_safe_subpath(self, path: str) -> bool:
        """Return True if path resolves to a location inside base_path.

        Uses realpath / os.path.realpath to follow symlinks before checking,
        so symlinks pointing outside the search root are correctly blocked.
        """
        ...

    def is_dir(self, path: str) -> bool:
        """Return True if path is a directory (following symlinks)."""
        ...

    def get_mime(self, path: str) -> str:
        """Return the MIME type string for path, e.g. 'text/plain'.

        Returns "" if file(1) is unavailable or fails.
        """
        ...

    def preview(self, filename: str, query: str, mode: str) -> int:
        """Render a preview of filename to stdout. Returns exit code."""
        ...

    def reload(self, query: str, ftype: str, ext: str, mode: str) -> int:
        """Emit the file/match list to stdout for fzf to display. Returns exit code."""
        ...

    def initial_list_cmd(self, frozen_self: Path) -> list[str]:
        """Return the argv list for the initial file list (name-mode only)."""
        ...


class LocalBackend:
    """Backend implementation for local filesystem searches."""

    def __init__(
        self,
        base_path: str,
        ssh_control: str = "",
        cache: "_PreviewCache | None" = None,
        frozen_self: "Path | None" = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        self.base_path = base_path
        self.ssh_control = ssh_control  # unused locally; kept for Protocol compat
        self._cache = cache
        self._frozen_self = frozen_self  # frozen script path for cache-miss subprocess
        self.exclude_patterns = exclude_patterns if exclude_patterns is not None else []

    def resolve_base(self, raw: str) -> str:
        if raw:
            return str(Path(raw).resolve())
        try:
            from .search import _find_git_root
        except ImportError:
            _find_git_root = globals()["_find_git_root"]  # flat built file
        git_root = _find_git_root()
        return git_root if git_root else str(Path.cwd().resolve())

    def is_safe_subpath(self, path: str) -> bool:
        try:
            return Path(path).resolve().is_relative_to(Path(self.base_path).resolve())
        except (FileNotFoundError, ValueError):
            return False

    def is_dir(self, path: str) -> bool:
        return Path(path).is_dir()

    def get_mime(self, path: str) -> str:
        return _get_mime(path)

    def preview(self, filename: str, query: str, mode: str) -> int:
        # PERF: Check the preview cache before spawning any subprocess.
        #       Cache key: path + nanosecond mtime (detects file changes) + query
        #       (different queries produce different highlighted output).
        cache = self._cache
        mtime: int | None = None
        cache_key: str = ""
        if cache is not None:
            mtime = _PreviewCache._local_mtime(filename)
            if mtime is not None:
                cache_key = f"local:{filename}:{mtime}:{query}"
                hit = cache.get(cache_key)
                if hit is not None:
                    sys.stdout.buffer.write(hit)
                    sys.stdout.buffer.flush()
                    return 0

        # Cache miss: build the argv for cmd_preview.
        preview_args = [filename]
        if mode == "content":
            preview_args.append(query)

        if cache is not None and mtime is not None and self._frozen_self is not None:
            # DESIGN: _passthrough() uses subprocess.run with inherited stdout,
            #         so Python-level stdout redirection cannot capture its output.
            #         We re-invoke this script as a subprocess with capture_output=True
            #         to collect the rendered bytes, then replay them and cache.
            #         The extra fork cost (~5 ms) is paid only on a cache miss;
            #         all subsequent visits to the same file cost ~0.1 ms.
            r = subprocess.run(
                [sys.executable, str(self._frozen_self), "fzfr-preview"] + preview_args,
                capture_output=True,
            )
            data = r.stdout
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if r.returncode == 0:
                cache.put(cache_key, data)
            return r.returncode

        try:
            from .preview import cmd_preview
        except ImportError:
            cmd_preview = globals()["cmd_preview"]  # flat built file
        return cmd_preview(preview_args)

    def reload(
        self,
        query: str,
        ftype: str,
        ext: str,
        mode: str,
        hidden: bool = False,
        exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
    ) -> int:
        if exclude_patterns is None:
            exclude_patterns = []
        fd_args = ["fd", "-L", "--type", ftype]
        if hidden:
            fd_args.append("--hidden")
        for e in _parse_extensions(ext):
            fd_args += ["-e", e]
        for p in exclude_patterns:
            fd_args += ["-E", p]

        # DESIGN: For relative paths, run fd from base_path (cwd) with root "."
        #         so output is relative to the search root. For absolute paths,
        #         pass base_path as the search root so fd emits full paths.
        if path_format == "relative":
            fd_root, fd_cwd = ["."], self.base_path
        else:
            fd_root, fd_cwd = [".", self.base_path], None

        if mode == "name":
            return subprocess.run(fd_args + fd_root, cwd=fd_cwd).returncode

        if query:
            return _local_content_search(
                fd_args, fd_root, fd_cwd, query, ext, hidden, self.base_path, path_format
            )

        return subprocess.run(fd_args + fd_root, cwd=fd_cwd).returncode

    def initial_list_cmd(
        self, _frozen_self: Path, hidden: bool = False, exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
    ) -> list[str]:
        # _frozen_self unused here — LocalBackend lists files directly via fd.
        # RemoteBackend uses it to locate the script for SSH upload.
        if exclude_patterns is None:
            exclude_patterns = []
        args = ["fd", "-L", "--type", "f"]
        if hidden:
            args.append("--hidden")
        for p in exclude_patterns:
            args += ["-E", p]
        if path_format == "relative":
            return args + ["."]
        return args + [".", self.base_path]


class RemoteBackend:
    """Backend implementation for SSH-remote searches."""

    def __init__(
        self,
        remote: str,
        base_path: str,
        ssh_control: str = "",
        cache: "_PreviewCache | None" = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        self.remote = remote
        self.base_path = base_path
        self.ssh_control = ssh_control
        self._cache = cache
        self.exclude_patterns = exclude_patterns if exclude_patterns is not None else []

    def _ssh(self) -> list[str]:
        """Return the base SSH argv prefix: ["ssh", <opts...>, <remote>]."""
        return ["ssh"] + _ssh_opts(self.ssh_control) + [self.remote]

    def resolve_base(self, raw: str) -> str:
        try:
            from .copy import _resolve_remote_path
        except ImportError:
            _resolve_remote_path = globals()["_resolve_remote_path"]  # flat built file
        return _resolve_remote_path(self.remote, raw, self.ssh_control)

    def is_safe_subpath(self, path: str) -> bool:
        ssh_base = self._ssh()
        out_path, rc_path = _capture(
            ssh_base + [shlex.join(["realpath", "-e", "--", path])]
        )
        if rc_path != 0:
            return False
        out_base, rc_base = _capture(
            ssh_base + [shlex.join(["realpath", "-e", "--", self.base_path])]
        )
        if rc_base != 0:
            return False
        resolved_path = out_path.strip()
        resolved_base = out_base.strip()
        if not resolved_path or not resolved_base:
            return False
        base_prefix = resolved_base.rstrip("/") + "/"
        return resolved_path == resolved_base or resolved_path.startswith(base_prefix)

    def is_dir(self, path: str) -> bool:
        _, rc = _capture(self._ssh() + [shlex.join(["test", "-d", path])])
        return rc == 0

    def get_mime(self, path: str) -> str:
        out, rc = _capture(
            self._ssh()
            + [shlex.join(["file", "-L", "--mime-type", "-b", path]) + " 2>/dev/null"]
        )
        return out.strip() if rc == 0 else ""

    def preview(self, filename: str, query: str, mode: str) -> int:
        # PERF: Debounce remote previews to avoid thrashing SSH connections
        #       during fast scrolling. 50 ms is negligible for SSH latency but
        #       saves many redundant round-trips during fast cursor movement.
        time.sleep(0.05)

        full_path = (
            filename
            if Path(filename).is_absolute()
            else str(Path(self.base_path) / filename)
        )

        # PERF: Check cache before paying the SSH round-trip + ~60 KB script
        #       transfer cost. Remote mtime requires one extra SSH call, but on
        #       a multiplexed connection that is ~5 ms vs ~200 ms for the full
        #       preview — a net saving of ~195 ms on every cache hit.
        cache = self._cache
        mtime: str | None = None
        cache_key: str = ""
        if cache is not None:
            mtime = _PreviewCache._remote_mtime(self._ssh(), full_path)
            if mtime is not None:
                cache_key = f"remote:{self.remote}:{full_path}:{mtime}:{query}"
                hit = cache.get(cache_key)
                if hit is not None:
                    sys.stdout.buffer.write(hit)
                    sys.stdout.buffer.flush()
                    return 0

        args = [self.remote, self.base_path, self.ssh_control, filename]
        if mode == "content":
            args.append(query)

        if cache is not None and mtime is not None:
            # Capture the SSH preview output so we can cache it before replay.
            try:
                from .remote import _cmd_remote_preview_capture
            except ImportError:
                _cmd_remote_preview_capture = globals()["_cmd_remote_preview_capture"]  # flat built file
            rc, data = _cmd_remote_preview_capture(args)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if rc == 0:
                cache.put(cache_key, data)
            return rc

        try:
            from .remote import cmd_remote_preview
        except ImportError:
            cmd_remote_preview = globals()["cmd_remote_preview"]  # flat built file
        return cmd_remote_preview(args)

    def reload(
        self,
        query: str,
        ftype: str,
        ext: str,
        mode: str,
        hidden: bool = False,
        exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
    ) -> int:
        if exclude_patterns is None:
            exclude_patterns = []
        args = [self.remote, self.base_path, self.ssh_control, ftype, ext]
        # DESIGN: query is only a content search term in content mode.
        #         In name mode it is fzf's fuzzy filter string — it must NOT
        #         be forwarded to cmd_remote_reload, which would interpret any
        #         non-empty string as a grep/rga content query and return
        #         content matches instead of a file listing for fzf to filter.
        if mode == "content" and query:
            args.append(query)
        if hidden:
            args.append("--hidden")
        for p in exclude_patterns:
            args.append("--exclude")
            args.append(p)
        if path_format == "relative":
            args.append("--relative")
        try:
            from .remote import cmd_remote_reload
        except ImportError:
            cmd_remote_reload = globals()["cmd_remote_reload"]  # flat built file
        return cmd_remote_reload(args)

    def initial_list_cmd(
        self, frozen_self: Path, hidden: bool = False, exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
    ) -> list[str]:
        if exclude_patterns is None:
            exclude_patterns = []
        args = [
            sys.executable,
            str(frozen_self),
            "fzfr-remote-reload",
            self.remote,
            self.base_path,
            self.ssh_control,
            "f",
            "",
        ]
        if hidden:
            args.append("--hidden")
        for p in exclude_patterns:
            args.append("--exclude")
            args.append(p)
        if path_format == "relative":
            args.append("--relative")
        return args


def backend_from_state(state: dict) -> LocalBackend | RemoteBackend:
    """Reconstruct the appropriate backend from a persisted state dict.

    Called at the start of every cmd_dispatch invocation. The state dict
    carries all the fields needed to build either backend type, including
    the session dir (derived from self_path) needed to locate the cache.
    """
    remote = state.get("remote", "")
    base_path = state.get("base_path", "")
    ssh_control = state.get("ssh_control", "")
    exclude_patterns = state.get("exclude_patterns", [])
    cache = _PreviewCache.from_state(state)
    if remote:
        return RemoteBackend(
            remote,
            base_path,
            ssh_control,
            cache=cache,
            exclude_patterns=exclude_patterns,
        )
    self_path_str = state.get("self_path", "")
    frozen_self = (
        Path(self_path_str) if self_path_str and self_path_str != "None" else None
    )
    return LocalBackend(
        base_path,
        ssh_control,
        cache=cache,
        frozen_self=frozen_self,
        exclude_patterns=exclude_patterns,
    )


# =============================================================================
# Local content search helper
# =============================================================================


def _local_content_search(
    fd_args: list[str],
    fd_root: list[str],
    fd_cwd: str | None,
    query: str,
    ext: str,
    hidden: bool,
    base_path: str,
    path_format: str,
) -> int:
    """Run a local content search using rga or fd+grep fallback.

    Returns the exit code of the search command. rga is preferred when
    available; fd | xargs grep is used as a fallback.
    """
    if "rga" in AVAILABLE_TOOLS:
        # PERF: Check AVAILABLE_TOOLS before forking. Previously this called
        #       subprocess.run(["rga", ...]) and caught FileNotFoundError —
        #       a wasted fork()+exec() on every keystroke when rga is absent.
        rga_cmd = ["rga"]
        if hidden:
            rga_cmd.append("--hidden")
        for e in _parse_extensions(ext):
            rga_cmd += ["-g", f"*.{e}"]
        rga_cmd += [
            "--files-with-matches",
            "--fixed-strings",
            query,
            "." if path_format == "relative" else base_path,
        ]
        r = subprocess.run(rga_cmd, cwd=fd_cwd, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return 0
        # rga found but returned non-zero — no matches, don't fallback
        # to grep (different tool, different scope). Return as-is.
        return r.returncode

    # grep fallback (rga absent): fd -0 | xargs -P4 grep
    # PERF: -P4 parallelises grep across up to 4 worker processes,
    #       significantly speeding up content searches on large repos.
    p1 = subprocess.Popen(
        fd_args + ["-0"] + fd_root,
        cwd=fd_cwd,
        stdout=subprocess.PIPE,
    )
    assert p1.stdout is not None
    p1.stdout.close()
    p2 = subprocess.run(
        ["xargs", "-P4", "-0", "grep", "-ilF", query], stdin=p1.stdout
    )
    p1.wait()
    return p2.returncode
