"""fzfr.preview -- fzfr-preview sub-command: render file content for fzf.

fzf calls this once per cursor movement. Dispatch order:

    PDF          -> pdftotext | rga --pretty (text extraction + highlighting)
    Archive      -> list contents via the appropriate tool (tar/7z/unzip/etc.)
    Directory    -> eza/exa/tree/ls
    Text/empty   -> bat --color=always (syntax highlighting)
    Binary       -> xxd/hexdump (hex dump, first 256 bytes)

All output goes to stdout via _passthrough() to preserve streaming and ANSI
colour codes. _capture() is used only for pdftotext where the output needs
to be piped into rga for query highlighting.

The preview cache (_PreviewCache) stores rendered output keyed on
(path, mtime_ns, query) so repeated cursor visits to the same file cost
~0.1 ms instead of spawning a new subprocess each time.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .utils import _capture, _passthrough, _try_run, _get_mime, _is_text_mime, _CAPTURE_PDF_MAX
from .config import AVAILABLE_TOOLS
from .archive import FileKind, classify, _list_archive
from .workbase import WORK_BASE


def _preview_pdf(filepath: str, query: str) -> None:
    """Render a PDF file in the preview pane.

    With a query: rga for highlighted, context-aware results; falls back to
    pdftotext + grep when rga is unavailable.

    Without a query: pdftotext for the first 50 lines of text; falls back to
    rga for scanned/image-only PDFs that have no native text layer.
    """
    if query:
        if _try_run(
            [["rga", "--pretty", "--color=always", "--context", "5", query, filepath]],
            "",
        ) == 0:
            return
        out, rc = _capture(["pdftotext", filepath, "-"], max_bytes=_CAPTURE_PDF_MAX)
        if rc == 0:
            hits = [line for line in out.splitlines() if query.lower() in line.lower()]
            print("\n".join(hits[:20]) if hits else "[No matches found]")
        else:
            print("[PDF: install poppler or rga]")
    else:
        out, rc = _capture(["pdftotext", filepath, "-"], max_bytes=_CAPTURE_PDF_MAX)
        if rc == 0 and out.strip():
            print("\n".join(out.splitlines()[:50]))
            return
        _try_run(
            [["rga", "--pretty", "--color=always", ".", filepath]],
            "PDF: no text layer, may be scanned or encrypted",
        )


def _preview_text(filepath: str, query: str) -> None:
    """Render a text file in the preview pane.

    With a query: matching lines with context via rga or grep.
    Without a query: full file with syntax highlighting via bat or cat.
    """
    if query:
        _try_run(
            [
                ["rga", "--pretty", "--color=always", "--context", "5", query, filepath],
                ["grep", "--color=always", "-iF", "-C", "5", query, filepath],
            ],
            "No matches found",
        )
    else:
        _try_run(
            [
                ["bat", "--color=always", "--style=numbers", filepath],
                ["cat", filepath],
            ],
            "Preview failed",
        )
    _preview_git_context(filepath)


def _preview_git_context(filepath: str) -> None:
    """Append git log and diff context to the preview pane for the given file.

    Shows the last 5 commits and any uncommitted diff. Both are omitted
    silently when git is unavailable or the file is outside a repository.
    """
    if "git" not in AVAILABLE_TOOLS:
        return

    log_result = subprocess.run(
        ["git", "log", "--oneline", "--color=always", "-5", "--", filepath],
        capture_output=True, text=True,
    )
    diff_result = subprocess.run(
        ["git", "diff", "HEAD", "--color=always", "--", filepath],
        capture_output=True, text=True,
    )

    log_out  = log_result.stdout.strip()
    diff_out = diff_result.stdout.strip()

    if not log_out and not diff_out:
        return

    print("\n\033[2m" + "-" * 40 + "\033[0m")  # dim separator
    if log_out:
        print("\033[2mgit log\033[0m")
        print(log_out)
    if diff_out:
        if log_out:
            print()
        print("\033[2mgit diff HEAD\033[0m")
        print(diff_out)


def _preview_archive(filepath: str, hint: str, query: str) -> None:
    """Render an archive file in the preview pane."""
    if query:
        if _passthrough(
            ["rga", "--pretty", "--color=always", "--context", "5", query, filepath]
        ) != 0:
            print("[Archive search requires rga with dependencies]")
    else:
        _list_archive(filepath, hint)


def _preview_directory(filepath: str) -> None:
    """Render a directory listing in the preview pane.

    Tries eza/exa for icons and colour, then tree for structure, then ls.
    --color=always is forced because fzf's preview sub-shell is a pipe.
    """
    _try_run(
        [
            ["eza", "--color=always", "--tree", "--level=2", "--icons", filepath],
            ["exa", "--color=always", "--tree", "--level=2", filepath],
            ["tree", "-L", "2", "-C", filepath],
            ["ls", "-la", "--color=always", filepath],
            ["ls", "-la", filepath],
        ],
        "Directory preview failed",
    )


def _preview_binary(filepath: str) -> None:
    """Render a truncated hexdump of a binary file (first 256 bytes)."""
    _try_run(
        [
            ["xxd", "-l", "256", filepath],
            ["hexdump", "-C", "-n", "256", filepath],
            ["od", "-t", "x1z", "-N", "256", filepath],
        ],
        "Binary preview failed",
    )


def _dispatch_preview(filepath: str, hint: str, mime: str, query: str) -> None:
    """Dispatch to the correct renderer once MIME type is known."""
    kind = classify(hint, mime)
    if kind is FileKind.ARCHIVE:
        _preview_archive(filepath, hint, query)
    elif kind is FileKind.PDF:
        _preview_pdf(filepath, query)
    elif kind is FileKind.DIRECTORY:
        _preview_directory(filepath)
    elif mime and not _is_text_mime(mime):
        _preview_binary(filepath)
    else:
        _preview_text(filepath, query)


def _preview_stdin(hint: str, query: str) -> None:
    """Buffer stdin to a temp file and preview it.

    Archive and PDF tools require a seekable file descriptor; stdin is a
    pipe and is not seekable. We buffer to RAM-backed tmpfs in WORK_BASE.
    """
    fd, tmpfile = tempfile.mkstemp(prefix="fzfr-preview-", dir=str(WORK_BASE))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(sys.stdin.buffer.read())
        mime = _get_mime(tmpfile)
        # Honour the original filename as a tiebreaker for headerless PDFs
        if hint.endswith(".pdf"):
            mime = "application/pdf"
        _dispatch_preview(tmpfile, hint, mime, query)
    finally:
        Path(tmpfile).unlink(missing_ok=True)


def _preview_file(filepath: str, query: str) -> None:
    """Preview a file on the local filesystem."""
    # PERF: Check directory first -- cheap and avoids forking file(1).
    if Path(filepath).is_dir():
        _preview_directory(filepath)
        return

    # PERF: classify() checks extension before forking file(1).
    # For the common case of source files this avoids a subprocess.
    kind = classify(filepath)
    if kind is FileKind.ARCHIVE:
        _preview_archive(filepath, filepath, query)
    elif kind is FileKind.PDF:
        _preview_pdf(filepath, query)
    else:
        mime = _get_mime(filepath)
        _dispatch_preview(filepath, filepath, mime, query)


def cmd_preview(argv: list[str]) -> int:
    """Entry point for the fzfr-preview sub-command.

    Called by fzf once per cursor movement with the path of the highlighted
    file. Dispatches to the correct renderer based on MIME type and extension.

    Arguments:
        argv[0]  filepath  -- path to preview, or "/dev/stdin" for piped input
        argv[1]  query     -- current fzf search query (optional, for highlighting)
        argv[2]  hint      -- original filename when filepath is a temp file (optional)

    DESIGN: Dispatches on MIME type rather than extension alone so that files
            without extensions (or with misleading ones) are handled correctly.
            Extension hints are used as a tiebreaker when file(1) is inconclusive.

    LIMITATION: Filenames containing a literal newline character are split by
                fd into multiple fzf entries. Each fragment resolves to a path
                that does not exist -- we catch that here and show a clear message.
    """
    if not argv:
        print("Usage: fzfr-preview <file> [query] [hint]", file=sys.stderr)
        return 1

    file_arg = argv[0]
    query    = argv[1] if len(argv) > 1 else ""
    hint     = argv[2] if len(argv) > 2 else file_arg

    if not file_arg:
        return 0

    if file_arg == "/dev/stdin":
        _preview_stdin(hint, query)
        return 0

    if not Path(file_arg).exists():
        print(f"[File not found: {file_arg}]")
        return 0

    _preview_file(file_arg, query)
    return 0