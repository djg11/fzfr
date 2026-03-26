"""remotely.cache -- Per-session file-backed LRU preview cache.

Cache directory
---------------
    <session_dir>/preview/

Cache keys
----------
    "local:<path>:<mtime_ns>:<query>"
    "remote:<host>:<path>:<mtime_epoch>:<query>"

Each key is hashed with BLAKE2b to produce the on-disk filename, so special
characters in paths or queries never reach the filesystem.

On a cache hit the stored bytes are replayed to stdout directly, saving:
  - a subprocess spawn + disk read for local previews
  - an SSH round-trip + script transfer for remote previews

Eviction
--------
LRU by file atime. When the entry count reaches MAX_ENTRIES the oldest entry
(by atime) is deleted before the new entry is written.

Lifetime
--------
The session directory (and therefore the cache) is cleaned up by the reaper
process when the anchor shell exits. remotely gc handles stragglers.
"""

import hashlib
import os
from pathlib import Path
from typing import Optional

from .utils import _capture, _shlex_join


MAX_ENTRIES = 200


def _cache_dir() -> Path:
    """Return (and create) the preview cache directory for the current session.

    DESIGN: get_session_dir() is imported inside this function to break the
    cache -> session -> config / workbase import cycle at module-load time.
    The try/except ImportError pattern is the established approach for circular-
    import resolution in the flat built file (see backends.py for the canonical
    example).
    """
    # fmt: off
    try:
        from .session import get_session_dir
    except ImportError:
        get_session_dir = globals()["get_session_dir"]  # flat built file
    # fmt: on
    cache = get_session_dir() / "preview"
    cache.mkdir(mode=0o700, exist_ok=True)
    return cache


def _entry_path(cache_key: str) -> Path:
    """Return the filesystem path for a cache key (hashed to a fixed-length name)."""
    digest = hashlib.blake2b(cache_key.encode(), digest_size=16).hexdigest()
    return _cache_dir() / digest


def _evict_lru_if_needed(cache_dir: Path) -> None:
    """Remove the least-recently-used entry when MAX_ENTRIES is reached."""
    try:
        entries = list(cache_dir.iterdir())
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
    """Return cached preview bytes for cache_key, or None on a miss."""
    try:
        entry = _entry_path(cache_key)
        data = entry.read_bytes()
        entry.touch()  # update atime so LRU eviction stays accurate
        return data
    except OSError:
        return None


def put(cache_key: str, data: bytes) -> None:
    """Store preview bytes under cache_key, evicting the LRU entry if needed.

    Cache write failures are silently ignored -- the preview was already
    rendered to stdout so the only consequence is a missed cache opportunity.
    """
    if not data:
        return
    try:
        cache_dir = _cache_dir()
        _evict_lru_if_needed(cache_dir)
        digest = hashlib.blake2b(cache_key.encode(), digest_size=16).hexdigest()
        (cache_dir / digest).write_bytes(data)
    except OSError:
        pass


def local_mtime(path: str) -> Optional[int]:
    """Return nanosecond mtime for a local file, or None if stat() fails."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def remote_mtime(ssh_prefix: list, path: str) -> Optional[str]:
    """Return the mtime string for a remote file, or None on failure.

    Tries ``stat -c %Y`` (Linux / GNU coreutils) first, then falls back to
    ``stat -f %m`` (macOS / BSD).
    """
    out, rc = _capture(ssh_prefix + [_shlex_join(["stat", "-c", "%Y", path])])
    if rc == 0 and out.strip():
        return out.strip()
    out, rc = _capture(ssh_prefix + [_shlex_join(["stat", "-f", "%m", path])])
    return out.strip() if rc == 0 and out.strip() else None


def local_cache_key(path: str, mtime: int, query: str) -> str:
    """Build a cache key for a local file preview."""
    return "local:" + path + ":" + str(mtime) + ":" + query


def remote_cache_key(host: str, path: str, mtime: str, query: str) -> str:
    """Build a cache key for a remote file preview."""
    return "remote:" + host + ":" + path + ":" + mtime + ":" + query


# ---------------------------------------------------------------------------
# Backwards-compatible wrapper used by backends.py
# ---------------------------------------------------------------------------


class _PreviewCache:
    """Thin wrapper around the module-level cache functions.

    backends.py instantiates this class to keep its API stable while the
    underlying implementation moved to module-level functions. The
    session_dir constructor argument is accepted but ignored -- the cache
    directory is now derived from the anchor PID via session.py.
    """

    MAX_ENTRIES = MAX_ENTRIES

    def __init__(self, session_dir=None):
        # session_dir is accepted for API compatibility but not used.
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
        """Return a live cache instance; no longer depends on session state."""
        return cls()
