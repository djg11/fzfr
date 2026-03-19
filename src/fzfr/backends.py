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
from .config import AVAILABLE_TOOLS, CONFIG
from .ssh import _ssh_opts
from .utils import _capture, _get_mime, _parse_extensions, _validate_exclude_pattern


def _is_git_repo(path: str) -> bool:
    """Return True if path is inside a git repository.

    Checks for a .git directory at the given path or any parent. Uses the
    same upward-walk approach as _find_git_root() but is self-contained so
    backends.py does not need to import from search.py.

    PERF: Pure filesystem check — no subprocess. Called once per reload on
    the local backend when file_source is "auto".
    """
    p = Path(path).resolve()
    for parent in [p] + list(p.parents):
        if (parent / ".git").is_dir():
            return True
    return False


def _git_ls_files_cmd(
    hidden: bool,
    exclude_patterns: list[str],
    ext: str,
) -> list[str]:
    """Return the argv list for a git ls-files invocation.

    Tracked files only by default (-c). With hidden=True, also includes
    untracked files that are not gitignored (--others --exclude-standard),
    mirroring what fd --hidden adds: files the user chose not to ignore.

    Extension filtering uses git pathspecs (-- '*.py' '*.rs') appended after
    a -- separator, equivalent to fd's -e flag.

    exclude_patterns are passed as --exclude globs. git ls-files accepts them
    natively so no post-filter is needed.

    DESIGN: path_format (absolute vs relative) is NOT handled here — git
    ls-files always outputs paths relative to cwd when run from inside the
    repo. The caller is responsible for prepending base_path for absolute
    output (see _reload_git and _build_git_remote_cmd).
    """
    args = ["git", "ls-files", "-c"]
    if hidden:
        args += ["--others", "--exclude-standard"]
    for p in exclude_patterns:
        if not _validate_exclude_pattern(p):
            print(f"Warning: ignoring unsafe exclude pattern {p!r}", file=sys.stderr)
            continue
        args += ["--exclude", p]
    # Extension pathspecs: must come after -- to be treated as patterns not flags
    exts = _parse_extensions(ext)
    if exts:
        args.append("--")
        for e in exts:
            args.append(f"*.{e}")
    return args


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
    exclude_patterns: list[str] = field(default_factory=list)
    self_path: Path | None = None  # path to the script (original or frozen)


@runtime_checkable
class Backend(Protocol):
    """Operations that differ between local-filesystem and SSH-remote sessions."""

    base_path: str
    ssh_control: str

    def resolve_base(self, raw: str) -> str: ...
    def is_safe_subpath(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
    def get_mime(self, path: str) -> str: ...
    def preview(self, filename: str, query: str, mode: str) -> int: ...
    def reload(self, query: str, ftype: str, ext: str, mode: str) -> int: ...
    def initial_list_cmd(self, frozen_self: Path) -> list[str]: ...


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
        self.ssh_control = ssh_control
        self._cache = cache
        self._frozen_self = frozen_self
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

        preview_args = [filename]
        if mode == "content":
            preview_args.append(query)

        if cache is not None and mtime is not None and self._frozen_self is not None:
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

    def _use_git(self, ftype: str, file_source: str) -> bool:
        """Return True if git ls-files should be used for this listing.

        Only used for file listings (ftype=="f"). Directory listings always
        use fd since git ls-files does not list directories.
        For "auto": check for a .git directory at or above base_path.
        """
        if ftype != "f":
            return False
        if file_source == "git":
            return True
        if file_source == "auto" and "git" in AVAILABLE_TOOLS:
            return _is_git_repo(self.base_path)
        return False

    def reload(
        self,
        query: str,
        ftype: str,
        ext: str,
        mode: str,
        hidden: bool = False,
        exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
        file_source: str = "auto",
    ) -> int:
        if exclude_patterns is None:
            exclude_patterns = []

        # Git ls-files path (name mode only — content search still uses rga/grep)
        if mode == "name" and self._use_git(ftype, file_source):
            return self._reload_git(hidden, exclude_patterns, path_format, query, ext)

        # fd path (original behaviour)
        fd_args = ["fd", "-L", "--type", ftype]
        if hidden:
            fd_args.append("--hidden")
        for e in _parse_extensions(ext):
            fd_args += ["-e", e]
        for p in exclude_patterns:
            if not _validate_exclude_pattern(p):
                print(f"Warning: ignoring unsafe exclude pattern {p!r}", file=sys.stderr)
                continue
            fd_args += ["-E", p]

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

    def _reload_git(
        self,
        hidden: bool,
        exclude_patterns: list[str],
        path_format: str,
        query: str,
        ext: str,
    ) -> int:
        """Run git ls-files and emit results to stdout for fzf.

        DESIGN: git ls-files always runs from base_path (cwd) and emits
        paths relative to that directory — the same as fd in relative mode.
        For absolute path_format we prepend base_path to each line via awk,
        keeping the output consistent with what fzf expects.
        """
        args = _git_ls_files_cmd(hidden, exclude_patterns, ext)

        if path_format == "relative":
            return subprocess.run(args, cwd=self.base_path).returncode

        # Absolute: pipe through awk to prepend base_path/
        # Using awk avoids a Python per-line loop and keeps output streaming.
        safe_base = self.base_path.rstrip("/")
        p1 = subprocess.Popen(args, cwd=self.base_path, stdout=subprocess.PIPE)
        assert p1.stdout is not None
        p2 = subprocess.run(
            ["awk", f'{{print "{safe_base}/" $0}}'],
            stdin=p1.stdout,
        )
        p1.stdout.close()
        p1.wait()
        return p2.returncode

    def initial_list_cmd(
        self,
        _frozen_self: Path,
        hidden: bool = False,
        exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
        file_source: str = "auto",
    ) -> list[str]:
        if exclude_patterns is None:
            exclude_patterns = []

        if self._use_git("f", file_source):
            # git ls-files is always run with cwd=base_path (handled by the
            # caller in cmd_search via list_cwd). No ext filter at initial list
            # time — the user hasn't typed a filter yet, so pass empty string.
            return _git_ls_files_cmd(hidden, exclude_patterns, "")

        args = ["fd", "-L", "--type", "f"]
        if hidden:
            args.append("--hidden")
        for p in exclude_patterns:
            if not _validate_exclude_pattern(p):
                print(f"Warning: ignoring unsafe exclude pattern {p!r}", file=sys.stderr)
                continue
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
        time.sleep(0.05)

        full_path = (
            filename
            if Path(filename).is_absolute()
            else str(Path(self.base_path) / filename)
        )

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
        file_source: str = "auto",
    ) -> int:
        if exclude_patterns is None:
            exclude_patterns = []
        args = [self.remote, self.base_path, self.ssh_control, ftype, ext]
        if mode == "content" and query:
            args.append(query)
        if hidden:
            args.append("--hidden")
        for p in exclude_patterns:
            args.append("--exclude")
            args.append(p)
        if path_format == "relative":
            args.append("--relative")
        # For remote "auto" defaults to "fd" — detecting a remote git repo
        # requires an extra SSH round-trip, too expensive per keystroke.
        # Users who want git ls-files on remote should set file_source="git".
        if file_source == "git":
            args.append("--file-source=git")
        try:
            from .remote import cmd_remote_reload
        except ImportError:
            cmd_remote_reload = globals()["cmd_remote_reload"]  # flat built file
        return cmd_remote_reload(args)

    def initial_list_cmd(
        self,
        frozen_self: Path,
        hidden: bool = False,
        exclude_patterns: list[str] | None = None,
        path_format: str = "absolute",
        file_source: str = "auto",
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
        if file_source == "git":
            args.append("--file-source=git")
        return args


def backend_from_state(state: dict) -> LocalBackend | RemoteBackend:
    """Reconstruct the appropriate backend from a persisted state dict."""
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
    """Run a local content search using rga or fd+grep fallback."""
    if "rga" in AVAILABLE_TOOLS:
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
        return r.returncode

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
