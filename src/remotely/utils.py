"""remotely.utils — Low-level subprocess and MIME helpers.

_capture()         run a command and return (stdout, returncode), bounded
                   by max_bytes to prevent memory exhaustion on large output
_passthrough()     run a command with inherited stdout (streaming, no capture)
_try_run()         attempt each command in a list until one succeeds
_get_mime()        detect MIME type via the `file` command
_is_text_mime()    return True for text/* and inode/x-empty (empty files)
_parse_extensions() parse and sanitise a whitespace-separated extension string
"""

import re
import subprocess
import sys

from .config import AVAILABLE_TOOLS


_CAPTURE_DEFAULT_MAX = 4096  # 4 KB

# PDF text extraction can legitimately be large; we cap it here so that
# a multi-hundred-page PDF does not load megabytes into RAM.
_CAPTURE_PDF_MAX = 512 * 1024  # 512 KB (~250 pages of dense text)


def _capture(cmd: list[str], max_bytes: int = _CAPTURE_DEFAULT_MAX) -> tuple[str, int]:
    """Run a command, capture up to max_bytes of stdout, and return (stdout, rc).

    Used when we need the output of a command as a Python string, e.g. to
    detect MIME types or get the remote $HOME path. Stderr is captured and
    discarded so it never leaks into the fzf preview pane.

    PERF/SAFETY: Uses Popen + a bounded read instead of
    subprocess.run(capture_output=True) so that commands producing large
    output (e.g. pdftotext on a large PDF) cannot exhaust process memory.
    Output beyond max_bytes is silently truncated; callers that need the
    full output should use _passthrough() instead.

    Returns ("", 127) if the executable is not found, matching the shell
    convention for "command not found".
    """
    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as proc:
            assert proc.stdout is not None
            chunk = proc.stdout.read(max_bytes)
            # Drain and discard any remaining output so the child is not
            # blocked on a full pipe buffer when we call wait().
            proc.stdout.read()
            proc.wait()
            return chunk.decode("utf-8", errors="replace"), proc.returncode
    except FileNotFoundError:
        return "", 127


def _passthrough(cmd: list[str], head_n: int | None = None) -> int:
    """Run a command and let its stdout flow directly to our stdout.

    This is the correct way to run preview commands. Using _capture() and
    then print()ing the result would buffer the entire output in memory and
    also strip ANSI colour codes that bat/rga/grep emit — causing the fzf
    preview pane to show plain, uncoloured text.

    If head_n is given, the output is piped through `head -n <head_n>` so
    we don't flood the preview pane with thousands of lines from large archives.

    Stderr is suppressed so tool-not-found errors don't appear in the pane;
    the caller is responsible for printing a user-friendly fallback message.

    Returns the exit code of the main command (not head's exit code), so
    callers can decide whether to try a fallback tool.
    """
    try:
        if head_n is not None:
            p1 = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            p2 = subprocess.Popen(["head", "-n", str(head_n)], stdin=p1.stdout)
            assert p1.stdout is not None
            p1.stdout.close()  # allows p1 to receive SIGPIPE when p2 exits early
            p2.wait()
            p1.wait()
            # DESIGN: p1 may exit with SIGPIPE (rc=141) because head closed the
            #         read end of the pipe before p1 finished writing. That is
            #         normal and expected; we treat p2's success as overall success.
            return 0 if p2.returncode == 0 else p1.returncode
        else:
            r = subprocess.run(cmd, stderr=subprocess.DEVNULL)
            return r.returncode
    except FileNotFoundError:
        return 127


def _try_run(commands: list[list[str]], status_msg: str) -> int:
    """Execute each command in sequence until one returns success (0).

    Three outcomes per command:
      rc == 0    success → return immediately, no message printed.
      rc == 127  tool not installed → skip silently, try next in chain.
      anything else  tool ran but failed (e.g. no matches) → stop here,
                     print status_msg, return rc. No point trying the next
                     tool; the failure is definitive.

    DESIGN: Centralises tool-fallback logic (e.g. bat → cat) so each call site
            states the preference chain rather than implementing it. rc==127 is
            the shell convention for "command not found" and is the only case
            where we silently move to the next option; any other failure is
            definitive and stops the chain.
    """
    last_rc = 127
    for cmd in commands:
        # PERF: Skip the fork entirely if the tool is known-absent at startup.
        #       _passthrough() would catch FileNotFoundError and return 127,
        #       but that still costs a fork()+exec() pair on every call.
        if cmd[0] not in AVAILABLE_TOOLS:
            continue
        rc = _passthrough(cmd)
        if rc == 0:
            return 0
        last_rc = rc
        if rc == 127:
            continue  # tool not found (PATH changed at runtime), try next
        break  # tool ran but failed — stop here
    if status_msg:
        print(f"[{status_msg}]")
    return last_rc


def _get_mime(filepath: str) -> str:
    """Return the MIME type of a file as reported by file(1), e.g. 'text/plain'.

    Uses the -b (brief) flag to suppress the filename prefix in the output.
    Uses -L to dereference symlinks and report the type of the target file.
    Returns an empty string if file(1) is unavailable or fails.
    """
    out, rc = _capture(["file", "-L", "--mime-type", "-b", filepath])
    return out.strip() if rc == 0 else ""


def _is_text_mime(mime: str) -> bool:
    """Return True if the MIME type indicates a file that can be opened in a text editor.

    Covers plain text and the most common structured-text application types.
    Used to decide between opening with an editor vs. xdg-open.

    inode/x-empty is included because zero-byte files should open in the editor
    rather than xdg-open — a new empty file is almost always a text file.
    """
    return mime.startswith("text/") or mime in (
        "application/json",
        "application/xml",
        "application/javascript",
        "inode/x-empty",
    )


def _parse_extensions(ext_str: str) -> list[str]:
    """Sanitize and split a whitespace-separated string of extensions.

    Removes leading dots, strips whitespace, and discards empty entries.
    Example: ".txt  .pdf py" -> ["txt", "pdf", "py"]

    SECURITY: Only alphanumeric characters are accepted after dot-stripping.
    A crafted extension like "py $(rm -rf ~)" or "py;evil" would survive
    shlex.quote at one shell level but could break out at a second level
    (e.g. inside a remote shell command or a glob argument). Rejecting
    non-alphanumeric values here closes that path entirely — the offending
    token is silently dropped rather than passed to fd/rga/grep.
    """
    result = []
    for raw in ext_str.split():
        e = raw.lstrip(".").strip()
        if not e:
            continue
        if not re.fullmatch(r"[A-Za-z0-9]+", e):
            print(
                f"Warning: ignoring unsafe extension {raw!r} "
                "(only alphanumeric characters are allowed)",
                file=sys.stderr,
            )
            continue
        result.append(e)
    return result


def _validate_exclude_pattern(pattern: str) -> bool:
    """Return True if the pattern is safe to pass to fd -E / rga --exclude.

    Allows glob metacharacters (* ? [ ] {}) but rejects shell operators
    that could inject commands into remote shell fragments.
    """
    SHELL_OPERATORS = (
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
    return not any(op in pattern for op in SHELL_OPERATORS)
