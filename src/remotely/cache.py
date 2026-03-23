"""remotely.cache — Preview output cache keyed on (path, mtime, query).

_PreviewCache stores the rendered bytes that would be written to stdout by
the preview renderer (bat, cat, rga, or the remote SSH preview). On a hit the
bytes are replayed directly, saving a subprocess spawn (local) or an SSH
round-trip plus script transfer (remote).
"""

import hashlib
import os
import shlex
from pathlib import Path

from .utils import _capture


class _PreviewCache:
    """File-backed preview output cache keyed on (path, mtime_ns, query).

    Each cache entry is the raw bytes that would be written to stdout by the
    preview renderer (bat, cat, rga, or the remote SSH preview). On a cache
    hit the bytes are replayed to stdout directly, saving the subprocess
    spawn (local) or SSH round-trip + ~60 KB script transfer (remote).

    Storage: one file per entry under <session_dir>/preview-cache/.
    The directory is inside the existing session dir so it is automatically
    cleaned up on session exit by the existing _cleanup() logic.

    Eviction: LRU by file mtime. When the entry count exceeds MAX_ENTRIES
    the oldest entry (by atime/mtime) is deleted before writing the new one.
    With typical previews of 2-20 KB and MAX_ENTRIES=200 the cache stays
    well under 5 MB.

    Thread-safety: each fzf callback runs in its own process (fzf spawns a
    new python3 for every preview), so there are no concurrent writes from
    the same session. Multiple sessions have distinct cache dirs.
    """

    MAX_ENTRIES = 200

    def __init__(self, session_dir: Path) -> None:
        self._dir = session_dir / "preview-cache"
        self._dir.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def _local_mtime(path: str) -> int | None:
        """Return nanosecond mtime for a local path, or None if stat fails."""
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _remote_mtime(ssh_prefix: list[str], path: str) -> str | None:
        """Return mtime string for a remote path via stat, or None on failure.

        Tries Linux stat first (stat -c %Y), then macOS/BSD stat (stat -f %m).
        Both return seconds since epoch, sufficient for cache key uniqueness.

        Previously only the Linux form was tried. On macOS remotes stat -c %Y
        fails silently, this function returns None, and the entire local output
        cache is bypassed — every cursor move triggers a full remote round-trip.
        The macOS fallback fixes this.
        """
        # Linux: stat -c %Y (seconds since epoch)
        out, rc = _capture(ssh_prefix + [shlex.join(["stat", "-c", "%Y", path])])
        if rc == 0 and out.strip():
            return out.strip()
        # macOS / BSD: stat -f %m (seconds since epoch, same meaning)
        out, rc = _capture(ssh_prefix + [shlex.join(["stat", "-f", "%m", path])])
        return out.strip() if rc == 0 and out.strip() else None

    def _entry_path(self, cache_key: str) -> Path:
        """Return the Path for a cache entry given its string key."""
        h = hashlib.blake2b(cache_key.encode(), digest_size=16).hexdigest()
        return self._dir / h

    def get(self, cache_key: str) -> bytes | None:
        """Return cached bytes for cache_key, or None on miss/error."""
        p = self._entry_path(cache_key)
        try:
            data = p.read_bytes()
            # Touch atime for LRU ordering.
            p.touch()
            return data
        except OSError:
            return None

    def put(self, cache_key: str, data: bytes) -> None:
        """Store data under cache_key, evicting the oldest entry if needed."""
        if not data:
            return
        try:
            entries = list(self._dir.iterdir())
            if len(entries) >= self.MAX_ENTRIES:
                # Evict the entry with the oldest modification time.
                oldest = min(entries, key=lambda p: p.stat().st_mtime)
                oldest.unlink(missing_ok=True)
            self._entry_path(cache_key).write_bytes(data)
        except OSError:
            pass  # Cache write failure is non-fatal; preview still rendered.

    @classmethod
    def from_state(cls, state: dict) -> "_PreviewCache | None":
        """Construct a cache from the persisted state dict, or None if unavailable.

        Derives the session dir from self_path (session_dir/remotely-frozen.py).
        Returns None when self_path is absent (e.g. stdin-mode invocations) so
        callers can treat None as "cache disabled" without special-casing.
        """
        self_path_str = state.get("self_path", "")
        if not self_path_str or self_path_str == "None":
            return None
        try:
            return cls(Path(self_path_str).parent)
        except OSError:
            return None
