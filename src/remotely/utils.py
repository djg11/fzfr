"""remotely.utils -- Low-level subprocess, MIME, and path helpers.

Public API
----------
_capture(cmd, max_bytes)         Run a command, return (stdout_str, returncode).
                                  Bounded read prevents memory exhaustion.
_passthrough(cmd, head_n)        Run a command with stdout flowing to our stdout.
                                  Preserves ANSI colour codes for preview rendering.
_try_run(commands, fallback_msg) Run each command until one succeeds (rc == 0).
                                  Skips silently on rc == 127 (tool not found).
_get_mime(path)                  Detect MIME type via file(1).
_is_text_mime(mime)              True for text/* and related editable types.
_parse_extensions(ext_str)       Parse and sanitise a whitespace-separated
                                  extension string; rejects non-alphanumeric.
_validate_exclude_pattern(p)     Reject patterns containing shell operators.
_removeprefix(s, prefix)         str.removeprefix() backport for Python 3.6-3.8.
_shlex_join(args)                shlex.join() backport for Python 3.6-3.7.
_resolve_absolute_path(path, base, remote)
                                  Resolve a relative path against a base.
_resolve_remote_path(remote, raw, ssh_control)
                                  Expand tilde / relative paths on a remote host.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

from .config import AVAILABLE_TOOLS
from .ssh import _ssh_opts


# ---------------------------------------------------------------------------
# Compat backports
# ---------------------------------------------------------------------------


def _shlex_join(args):
    # type: (Iterable[str]) -> str
    """Return a shell-safe string by joining shlex.quote(a) for each arg.

    Backport of ``shlex.join()`` (added in Python 3.8) so the built script
    runs on Python 3.6 remote hosts without modification.
    """
    return " ".join(shlex.quote(arg) for arg in args)


def _removeprefix(s, prefix):
    # type: (str, str) -> str
    """Return s with prefix removed, or s unchanged if it does not start with prefix.

    Backport of ``str.removeprefix()`` (added in Python 3.9).
    """
    if prefix and s.startswith(prefix):
        return s[len(prefix) :]
    return s


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

_CAPTURE_DEFAULT_MAX = 4096  # 4 KB -- sufficient for MIME lines, paths, mtimes

# PDF text extraction can be large; cap it so a multi-hundred-page document
# does not load megabytes into RAM during a preview call.
_CAPTURE_PDF_MAX = 512 * 1024  # 512 KB (~250 pages of dense text)


def _capture(cmd, max_bytes=_CAPTURE_DEFAULT_MAX):
    # type: (List[str], int) -> Tuple[str, int]
    """Run cmd and return (stdout_text, returncode), reading at most max_bytes.

    Uses Popen + a bounded read rather than subprocess.run(capture_output=True)
    so that commands producing large output cannot exhaust process memory.
    Output beyond max_bytes is silently discarded; callers that need the full
    stream should use _passthrough() instead.

    Stderr is always suppressed to prevent tool error messages from leaking
    into the fzf preview pane.

    Returns ("", 127) when the executable is not found -- matching the shell
    convention for "command not found" so callers can treat it like any other
    missing-tool case.
    """
    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as proc:
            assert proc.stdout is not None
            chunk = proc.stdout.read(max_bytes)
            # Drain remaining output so the child is not blocked on a full
            # pipe buffer when we call wait().
            proc.stdout.read()
            proc.wait()
            return chunk.decode("utf-8", errors="replace"), proc.returncode
    except FileNotFoundError:
        return "", 127


def _passthrough(cmd, head_n=None):
    # type: (List[str], Optional[int]) -> int
    """Run cmd with its stdout flowing directly to our stdout.

    This is the right function for preview commands. Capturing with _capture()
    and then print()-ing would buffer everything in memory and strip the ANSI
    colour codes that bat / rga / grep emit.

    When head_n is given the output is piped through ``head -n <head_n>`` to
    prevent flooding the preview pane from large archives. The return code is
    that of the main command, not head's, so callers can decide whether to
    fall back to an alternative tool.

    Stderr is suppressed; callers are responsible for printing a user-friendly
    fallback message on failure.
    """
    try:
        if head_n is not None:
            p1 = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            p2 = subprocess.Popen(["head", "-n", str(head_n)], stdin=p1.stdout)
            assert p1.stdout is not None
            p1.stdout.close()  # let p1 receive SIGPIPE when p2 exits early
            p2.wait()
            p1.wait()
            # DESIGN: p1 may exit with SIGPIPE (rc=141) because head closed
            # the pipe before p1 finished writing. That is normal; treat p2
            # success as overall success.
            return 0 if p2.returncode == 0 else p1.returncode
        else:
            return subprocess.run(cmd, stderr=subprocess.DEVNULL).returncode
    except FileNotFoundError:
        return 127


def _try_run(commands, fallback_msg):
    # type: (List[List[str]], str) -> int
    """Run each command in sequence until one succeeds (returns 0).

    Per-command outcomes:
      rc == 0    -- success; return immediately without printing anything.
      rc == 127  -- tool not installed; skip silently and try the next entry.
      anything else -- tool ran but failed; stop the chain, print fallback_msg.

    rc == 127 is the only case that silently advances to the next option;
    any other failure is treated as definitive.

    PERF: Skips the fork entirely when AVAILABLE_TOOLS already reports the
    tool as absent at process startup (avoids a useless execve + ENOENT).
    """
    last_rc = 127
    for cmd in commands:
        if cmd[0] not in AVAILABLE_TOOLS:
            continue
        rc = _passthrough(cmd)
        if rc == 0:
            return 0
        last_rc = rc
        if rc == 127:
            continue  # tool disappeared from PATH at runtime; try next
        break  # tool ran but produced an error; stop
    if fallback_msg:
        print(f"[{fallback_msg}]")
    return last_rc


# ---------------------------------------------------------------------------
# MIME detection
# ---------------------------------------------------------------------------


def _get_mime(filepath):
    # type: (str) -> str
    """Return the MIME type of filepath as reported by file(1).

    Uses ``-b`` (brief: no filename prefix) and ``-L`` (dereference symlinks).
    Returns an empty string when file(1) is unavailable or the call fails.
    """
    out, rc = _capture(["file", "-L", "--mime-type", "-b", filepath])
    return out.strip() if rc == 0 else ""


def _is_text_mime(mime):
    # type: (str) -> bool
    """Return True when mime indicates a file that should open in a text editor.

    Covers plain text (text/*) and the most common structured-text application
    subtypes. ``inode/x-empty`` is included so that zero-byte files open in
    the editor rather than being handed to xdg-open.
    """
    return mime.startswith("text/") or mime in (
        "application/json",
        "application/xml",
        "application/javascript",
        "inode/x-empty",
    )


# ---------------------------------------------------------------------------
# Extension and pattern validation
# ---------------------------------------------------------------------------


def _parse_extensions(ext_str):
    # type: (str) -> List[str]
    """Parse a whitespace-separated extension string into a sanitised list.

    Leading dots are stripped; empty entries are ignored.
    Example: ``".txt  .pdf py"`` -> ``["txt", "pdf", "py"]``

    SECURITY: Only alphanumeric characters are accepted after dot-stripping.
    An extension like ``"py$(rm -rf ~)"`` would survive one level of
    shlex.quote but could break out at a second shell level (e.g. inside a
    remote SSH command). Rejecting non-alphanumeric values here closes that
    path entirely.
    """
    result = []
    for raw in ext_str.split():
        ext = raw.lstrip(".").strip()
        if not ext:
            continue
        if not re.fullmatch(r"[A-Za-z0-9]+", ext):
            print(
                f"Warning: ignoring unsafe extension {raw!r} "
                "(only alphanumeric characters are allowed)",
                file=sys.stderr,
            )
            continue
        result.append(ext)
    return result


def _validate_exclude_pattern(pattern):
    # type: (str) -> bool
    """Return True when pattern is safe to pass to ``fd -E`` or ``rga --exclude``.

    Glob metacharacters (``* ? [ ] { }``) are allowed; shell operators that
    could inject commands into remote shell fragments are rejected.
    """
    _SHELL_OPERATORS = (
        ";",
        "|",
        "&&",
        "||",
        "$",
        "`",
        ">",
        "<",
        "\n",
        "(",
        ")",
        "&",
        "\\",
    )
    return not any(op in pattern for op in _SHELL_OPERATORS)


# ---------------------------------------------------------------------------
# fzf version parsing
# ---------------------------------------------------------------------------


def _parse_fzf_version(version_str):
    # type: (str) -> Tuple[int, int]
    """Parse a fzf version string and return ``(major, minor)``.

    Returns ``(0, 0)`` when the string does not match the expected
    ``MAJOR.MINOR[.PATCH]`` format.
    """
    m = re.match(r"(\d+)\.(\d+)", version_str.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_absolute_path(path, base_path, remote=False):
    # type: (str, str, bool) -> str
    """Resolve path relative to base_path.

    When remote=True uses PurePosixPath (no local filesystem access).
    When remote=False uses pathlib.Path (resolves against local cwd).
    Absolute paths are returned unchanged in both modes.
    """
    if not path:
        return path

    if remote:
        p = PurePosixPath(path)
        if p.is_absolute():
            return path
        return str(PurePosixPath(base_path) / p) if base_path else path

    p = Path(path)
    if p.is_absolute():
        return path
    return str((Path(base_path) if base_path else Path.cwd()) / p)


def _resolve_remote_path(remote, raw, ssh_control):
    # type: (str, str, str) -> str
    """Expand a remote path to its absolute form by querying the remote host.

    Three cases:
      - Empty or ``"."``: run ``pwd`` on the remote to get the current directory.
      - ``"~"`` or ``"~/..."``: expand via ``python3 -c os.path.expanduser``
        on the remote (avoids shell injection -- the raw value is passed via
        stdin, not embedded in the command string).
      - Anything else: return raw unchanged (already absolute).

    SECURITY: Tilde expansion uses python3 stdin rather than shell string
    interpolation to prevent a crafted path from injecting shell commands.

    Calls sys.exit(1) on SSH failure so that a network or auth error cannot
    silently cause fzf to search from the remote filesystem root (/).
    """
    if not raw or raw == ".":
        r = subprocess.run(
            ["ssh"] + _ssh_opts(ssh_control) + [remote, "pwd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode != 0:
            print(
                f"Error: SSH failed to resolve path for {remote} (rc={r.returncode})",
                file=sys.stderr,
            )
            sys.exit(1)
        return r.stdout.strip()

    if raw == "~" or raw.startswith("~"):
        r = subprocess.run(
            ["ssh"]
            + _ssh_opts(ssh_control)
            + [
                remote,
                "python3 -c "
                "'import os,sys; print(os.path.expanduser(sys.stdin.read().strip()))'",
            ],
            input=raw.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if r.returncode != 0:
            print(
                f"Error: SSH failed to expand tilde for {remote} (rc={r.returncode})",
                file=sys.stderr,
            )
            sys.exit(1)
        return r.stdout.decode("utf-8", errors="replace").strip()

    return raw
