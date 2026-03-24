"""remotely.backends -- Backend protocol and implementations for local and remote search.

The Backend protocol abstracts the difference between searching a local filesystem
and an SSH-remote one. All fzf callbacks (preview, reload, open) go through the
backend so the rest of the code has no if-remote branches.

Three concrete types:

    SearchContext   -- immutable connection parameters (host, base path, ssh socket)
    LocalBackend    -- operations on the local filesystem via fd, bat, rga, etc.
    RemoteBackend   -- same operations forwarded over SSH; streams the script to the
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
from pathlib import Path
from typing import List

from .cache import _PreviewCache
from .config import AVAILABLE_TOOLS
from .ssh import _ssh_opts
from .utils import (
    _capture,
    _get_mime,
    _parse_extensions,
    _shlex_join,
    _validate_exclude_pattern,
)


def _is_git_repo(path):
    # type: (str) -> bool
    """Return True if path is inside a git repository."""
    p = Path(path).resolve()
    return any((parent / ".git").is_dir() for parent in [p] + list(p.parents))


def _git_ls_files_cmd(hidden, exclude_patterns, ext):
    # type: (bool, List[str], str) -> List[str]
    """Return the argv list for a git ls-files invocation."""
    args = ["git", "ls-files", "-c"]
    if hidden:
        args += ["--others", "--exclude-standard"]
    for p in exclude_patterns:
        if not _validate_exclude_pattern(p):
            print(
                f"Warning: ignoring unsafe exclude pattern {p!r}",
                file=sys.stderr,
            )
            continue
        args += ["--exclude", p]
    exts = _parse_extensions(ext)
    if exts:
        args.append("--")
        for e in exts:
            args.append(f"*.{e}")
    return args


class LocalBackend:
    """Backend implementation for local filesystem searches."""

    def __init__(
        self,
        base_path,
        ssh_control="",
        cache=None,
        frozen_self=None,
        exclude_patterns=None,
    ):
        # type: (str, str, Optional[_PreviewCache], Optional[Path], Optional[List[str]]) -> None
        self.base_path = base_path
        self.ssh_control = ssh_control
        self._cache = cache
        self._frozen_self = frozen_self
        self.exclude_patterns = exclude_patterns if exclude_patterns is not None else []

    def resolve_base(self, raw):
        # type: (str) -> str
        if raw:
            return str(Path(raw).resolve())
        # fmt: off
        try:
            from .search import _find_git_root
        except ImportError:
            _find_git_root = globals()["_find_git_root"]  # flat built file
        # fmt: on
        git_root = _find_git_root()
        return git_root if git_root else str(Path.cwd().resolve())

    def is_safe_subpath(self, path):
        # type: (str) -> bool
        try:
            Path(path).resolve().relative_to(Path(self.base_path).resolve())
            return True
        except (FileNotFoundError, ValueError):
            return False

    def is_dir(self, path):
        # type: (str) -> bool
        return Path(path).is_dir()

    def get_mime(self, path):
        # type: (str) -> str
        return _get_mime(path)

    def preview(self, filename, query, mode):
        # type: (str, str, str) -> int
        cache = self._cache
        mtime = None  # type: Optional[int]
        cache_key = ""
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
                [sys.executable, str(self._frozen_self), "remotely-preview"]
                + preview_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            data = r.stdout
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if r.returncode == 0:
                cache.put(cache_key, data)
            return r.returncode

        # fmt: off
        try:
            from .preview import cmd_preview
        except ImportError:
            cmd_preview = globals()["cmd_preview"]  # flat built file
        # fmt: on
        return cmd_preview(preview_args)

    def _use_git(self, ftype, file_source):
        # type: (str, str) -> bool
        if ftype != "f":
            return False
        if file_source == "git":
            return True
        if file_source == "auto" and "git" in AVAILABLE_TOOLS:
            return _is_git_repo(self.base_path)
        return False

    def reload(
        self,
        query,
        ftype,
        ext,
        mode,
        hidden=False,
        exclude_patterns=None,
        path_format="absolute",
        file_source="auto",
    ):
        # type: (str, str, str, str, bool, Optional[List[str]], str, str) -> int
        if exclude_patterns is None:
            exclude_patterns = []

        if mode == "name" and self._use_git(ftype, file_source):
            return self._reload_git(hidden, exclude_patterns, path_format, query, ext)

        fd_args = ["fd", "-L", "--type", ftype]
        if hidden:
            fd_args.append("--hidden")
        for e in _parse_extensions(ext):
            fd_args += ["-e", e]
        for p in exclude_patterns:
            if not _validate_exclude_pattern(p):
                print(
                    f"Warning: ignoring unsafe exclude pattern {p!r}",
                    file=sys.stderr,
                )
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
                fd_args,
                fd_root,
                fd_cwd,
                query,
                ext,
                hidden,
                self.base_path,
                path_format,
            )

        return subprocess.run(fd_args + fd_root, cwd=fd_cwd).returncode

    def _reload_git(self, hidden, exclude_patterns, path_format, query, ext):
        # type: (bool, List[str], str, str, str) -> int
        args = _git_ls_files_cmd(hidden, exclude_patterns, ext)

        if path_format == "relative":
            return subprocess.run(args, cwd=self.base_path).returncode

        safe_base = self.base_path.rstrip("/")
        p1 = subprocess.Popen(args, cwd=self.base_path, stdout=subprocess.PIPE)
        assert p1.stdout is not None
        p2 = subprocess.run(
            ["awk", '{{print "{}/{}" $0}}'.format(safe_base, "")],
            stdin=p1.stdout,
        )
        p1.stdout.close()
        p1.wait()
        return p2.returncode

    def initial_list_cmd(
        self,
        _frozen_self,
        hidden=False,
        exclude_patterns=None,
        path_format="absolute",
        file_source="auto",
    ):
        # type: (Path, bool, Optional[List[str]], str, str) -> List[str]
        if exclude_patterns is None:
            exclude_patterns = []

        if self._use_git("f", file_source):
            return _git_ls_files_cmd(hidden, exclude_patterns, "")

        args = ["fd", "-L", "--type", "f"]
        if hidden:
            args.append("--hidden")
        for p in exclude_patterns:
            if not _validate_exclude_pattern(p):
                print(
                    f"Warning: ignoring unsafe exclude pattern {p!r}",
                    file=sys.stderr,
                )
                continue
            args += ["-E", p]
        if path_format == "relative":
            return args + ["."]
        return args + [".", self.base_path]


class RemoteBackend:
    """Backend implementation for SSH-remote searches."""

    def __init__(
        self,
        remote,
        base_path,
        ssh_control="",
        cache=None,
        exclude_patterns=None,
    ):
        # type: (str, str, str, Optional[_PreviewCache], Optional[List[str]]) -> None
        self.remote = remote
        self.base_path = base_path
        self.ssh_control = ssh_control
        self._cache = cache
        self.exclude_patterns = exclude_patterns if exclude_patterns is not None else []

    def _ssh(self):
        # type: () -> List[str]
        return ["ssh"] + _ssh_opts(self.ssh_control) + [self.remote]

    def resolve_base(self, raw):
        # type: (str) -> str
        # fmt: off
        try:
            from .copy import _resolve_remote_path
        except ImportError:
            _resolve_remote_path = globals()["_resolve_remote_path"]  # flat built file
        # fmt: on
        return _resolve_remote_path(self.remote, raw, self.ssh_control)

    def is_safe_subpath(self, path):
        # type: (str) -> bool
        ssh_base = self._ssh()
        out_path, rc_path = _capture(
            ssh_base + [_shlex_join(["realpath", "-e", "--", path])]
        )
        if rc_path != 0:
            return False
        out_base, rc_base = _capture(
            ssh_base + [_shlex_join(["realpath", "-e", "--", self.base_path])]
        )
        if rc_base != 0:
            return False
        resolved_path = out_path.strip()
        resolved_base = out_base.strip()
        if not resolved_path or not resolved_base:
            return False
        base_prefix = resolved_base.rstrip("/") + "/"
        return resolved_path == resolved_base or resolved_path.startswith(base_prefix)

    def is_dir(self, path):
        # type: (str) -> bool
        _, rc = _capture(self._ssh() + [_shlex_join(["test", "-d", path])])
        return rc == 0

    def get_mime(self, path):
        # type: (str) -> str
        out, rc = _capture(
            self._ssh()
            + [_shlex_join(["file", "-L", "--mime-type", "-b", path]) + " 2>/dev/null"]
        )
        return out.strip() if rc == 0 else ""

    def preview(self, filename, query, mode):
        # type: (str, str, str) -> int
        time.sleep(0.05)

        full_path = (
            filename
            if Path(filename).is_absolute()
            else str(Path(self.base_path) / filename)
        )

        cache = self._cache
        mtime = None  # type: Optional[str]
        cache_key = ""
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
            # fmt: off
            try:
                from .remote import _cmd_remote_preview_capture
            except ImportError:
                _cmd_remote_preview_capture = globals()["_cmd_remote_preview_capture"]  # flat built file
            # fmt: on
            rc, data = _cmd_remote_preview_capture(args)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if rc == 0:
                cache.put(cache_key, data)
            return rc

        # fmt: off
        try:
            from .remote import cmd_remote_preview
        except ImportError:
            cmd_remote_preview = globals()["cmd_remote_preview"]  # flat built file
        # fmt: on
        return cmd_remote_preview(args)

    def reload(
        self,
        query,
        ftype,
        ext,
        mode,
        hidden=False,
        exclude_patterns=None,
        path_format="absolute",
        file_source="auto",
    ):
        # type: (str, str, str, str, bool, Optional[List[str]], str, str) -> int
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
        if file_source == "git":
            args.append("--file-source=git")
        # fmt: off
        try:
            from .remote import cmd_remote_reload
        except ImportError:
            cmd_remote_reload = globals()["cmd_remote_reload"]  # flat built file
        # fmt: on
        return cmd_remote_reload(args)

    def initial_list_cmd(
        self,
        frozen_self,
        hidden=False,
        exclude_patterns=None,
        path_format="absolute",
        file_source="auto",
    ):
        # type: (Path, bool, Optional[List[str]], str, str) -> List[str]
        if exclude_patterns is None:
            exclude_patterns = []
        args = [
            sys.executable,
            str(frozen_self),
            "remotely-remote-reload",
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


def backend_from_state(state):
    # type: (dict) -> Union[LocalBackend, RemoteBackend]
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
    fd_args,
    fd_root,
    fd_cwd,
    query,
    ext,
    hidden,
    base_path,
    path_format,
):
    # type: (List[str], List[str], Optional[str], str, str, bool, str, str) -> int
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
    p2 = subprocess.run(["xargs", "-P4", "-0", "grep", "-ilF", query], stdin=p1.stdout)
    p1.wait()
    return p2.returncode
