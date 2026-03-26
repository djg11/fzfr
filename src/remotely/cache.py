"""remotely.cache -- Per-session file-backed LRU preview cache.

Cache directory: <session_dir>/preview/
Cache key:       "local:<path>:<mtime_ns>:<query>"
                 "remote:<host>:<path>:<mtime_epoch>:<query>"
Cache entry:     raw bytes written to stdout by the preview renderer.

On a hit the bytes are replayed to stdout directly, saving:
  - a subprocess spawn + disk read for local previews
  - an SSH round-trip + script transfer for remote previews

Eviction: LRU by file atime.  When the entry count exceeds MAX_ENTRIES the
oldest entry (by atime) is deleted before the new entry is written.

The session directory is created and managed by session.py.  The cache is
automatically cleaned up by the reaper when the anchor shell exits.
"""

import hashlib
import os
from pathlib import Path
from typing import Optional

from .utils import _capture, _shlex_join


MAX_ENTRIES = 200


def _cache_dir() -> Path:
    """Return (and create) the preview cache directory for this session.

    DESIGN: get_session_dir is imported inside this function to break the
    cache->session->config/workbase import cycle at module load time.
    The try/except ImportError pattern is the established pattern for
    circular-import resolution in the flat built file (see backends.py).
    """
    # fmt: off
    try:
        from .session import get_session_dir
    except ImportError:
        get_session_dir = globals()["get_session_dir"]  # flat built file
    # fmt: on
    d = get_session_dir() / "preview"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _entry_path(cache_key: str) -> Path:
    h = hashlib.blake2b(cache_key.encode(), digest_size=16).hexdigest()
    return _cache_dir() / h


def _evict_if_needed(cache_d: Path) -> None:
    """Remove the LRU entry when MAX_ENTRIES is exceeded."""
    try:
        entries = list(cache_d.iterdir())
        if len(entries) < MAX_ENTRIES:
            return
        oldest = min(entries, key=lambda p: p.stat().st_atime)
        try:
            oldest.unlink()
        except (FileNotFoundError, OSError):
            pass
    except OSError:
        pass


def get(cache_key: str) -> Optional[bytes]:
    """Return cached bytes for cache_key, or None on miss."""
    try:
        p = _entry_path(cache_key)
        data = p.read_bytes()
        p.touch()  # update atime for LRU ordering
        return data
    except OSError:
        return None


def put(cache_key: str, data: bytes) -> None:
    """Store data under cache_key, evicting the LRU entry if needed."""
    if not data:
        return
    try:
        cache_d = _cache_dir()
        _evict_if_needed(cache_d)
        h = hashlib.blake2b(cache_key.encode(), digest_size=16).hexdigest()
        (cache_d / h).write_bytes(data)
    except OSError:
        pass  # cache write failure is non-fatal; preview still rendered


def local_mtime(path: str) -> Optional[int]:
    """Return nanosecond mtime for a local path, or None if stat fails."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def remote_mtime(ssh_prefix, path: str) -> Optional[str]:
    # type: (list, str) -> Optional[str]
    """Return mtime string for a remote path, or None on failure.

    Tries Linux stat -c %Y first, then macOS/BSD stat -f %m.
    """
    out, rc = _capture(ssh_prefix + [_shlex_join(["stat", "-c", "%Y", path])])
    if rc == 0 and out.strip():
        return out.strip()
    out, rc = _capture(ssh_prefix + [_shlex_join(["stat", "-f", "%m", path])])
    return out.strip() if rc == 0 and out.strip() else None


def local_cache_key(path: str, mtime: int, query: str) -> str:
    return "local:" + path + ":" + str(mtime) + ":" + query


def remote_cache_key(host: str, path: str, mtime: str, query: str) -> str:
    return "remote:" + host + ":" + path + ":" + mtime + ":" + query


# ---------------------------------------------------------------------------
# Compat shim -- backends.py instantiates _PreviewCache.
# Delegates to the module-level functions above.
# ---------------------------------------------------------------------------


class _PreviewCache:
    """Backwards-compatible wrapper used by backends.py.

    The session_dir constructor argument is accepted but ignored -- the
    cache directory is now derived from the anchor PID via session.py.
    """

    MAX_ENTRIES = MAX_ENTRIES

    def __init__(self, session_dir=None):
        # session_dir ignored; kept for API compatibility
        pass

    @staticmethod
    def _local_mtime(path):
        # type: (str) -> Optional[int]
        return local_mtime(path)

    @staticmethod
    def _remote_mtime(ssh_prefix, path):
        # type: (list, str) -> Optional[str]
        return remote_mtime(ssh_prefix, path)

    def get(self, cache_key):
        # type: (str) -> Optional[bytes]
        return get(cache_key)

    def put(self, cache_key, data):
        # type: (str, bytes) -> None
        put(cache_key, data)

    @classmethod
    def from_state(cls, state):
        # type: (dict) -> "_PreviewCache"
        """Always returns a live cache -- no longer depends on session state."""
        return cls()
