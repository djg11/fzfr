"""remotely.backends -- Backend protocol and implementations for local and remote search.

The Backend protocol abstracts the difference between searching a local filesystem
and an SSH-remote one. All preview calls go through the backend so the rest of
the code has no if-remote branches.

Two concrete types:

    SearchContext   -- immutable connection parameters (host, base path, ssh socket)
    LocalBackend    -- preview operations on the local filesystem
    RemoteBackend   -- same operations forwarded over SSH; streams the script to the
                      remote python3 interpreter for preview sub-commands

The circular-import problem: LocalBackend.preview() calls cmd_preview, and
RemoteBackend calls into remote.py. Those modules import from this one. The
cycle is broken at runtime with try/except ImportError late imports; the built
single-file script has no packages so the except branch fires and pulls the
function from globals() instead.
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import List

from .cache import _PreviewCache
from .ssh import _ssh_opts
from .utils import (
    _capture,
    _get_mime,
    _resolve_remote_path,
    _shlex_join,
)


class LocalBackend:
    """Backend implementation for local filesystem operations."""

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


class RemoteBackend:
    """Backend implementation for SSH-remote operations."""

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


def _find_git_root():
    # type: () -> Optional[str]
    """Search upwards from cwd for a .git folder."""
    curr = Path.cwd().resolve()
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").is_dir():
            return str(parent)
    return None
