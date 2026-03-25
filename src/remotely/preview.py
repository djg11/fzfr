"""remotely.preview -- remotely-preview sub-command: render file content for fzf.

fzf calls this once per cursor movement. Dispatch order:

    PDF          -> pdftotext | rga --pretty (text extraction + highlighting)
    Archive      -> list contents via the appropriate tool (tar/7z/unzip/etc.)
    Directory    -> eza/exa/tree/ls
    Text/empty   -> bat --color=always (syntax highlighting)
    Binary       -> xxd/hexdump (hex dump, first 256 bytes)

All output goes to stdout via _passthrough() to preserve streaming and ANSI
colour codes. _capture() is used only for pdftotext where the output needs
to be piped into rga for query highlighting.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .archive import FileKind, _list_archive, classify
from .config import AVAILABLE_TOOLS
from .utils import (
    _CAPTURE_PDF_MAX,
    _capture,
    _get_mime,
    _is_text_mime,
    _passthrough,
    _try_run,
)
from .workbase import WORK_BASE


def _preview_pdf(filepath, query):
    # type: (str, str) -> None
    """Render a PDF file in the preview pane."""
    if query:
        if (
            _try_run(
                [
                    [
                        "rga",
                        "--pretty",
                        "--color=always",
                        "--context",
                        "5",
                        query,
                        filepath,
                    ]
                ],
                "",
            )
            == 0
        ):
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


def _preview_text(filepath, query):
    # type: (str, str) -> None
    """Render a text file in the preview pane."""
    if query:
        _try_run(
            [
                [
                    "rga",
                    "--pretty",
                    "--color=always",
                    "--context",
                    "5",
                    query,
                    filepath,
                ],
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


def _preview_git_context(filepath):
    # type: (str) -> None
    """Append git log and diff context to the preview pane for the given file."""
    if "git" not in AVAILABLE_TOOLS:
        return

    log_result = subprocess.run(
        ["git", "log", "--oneline", "--color=always", "-5", "--", filepath],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    diff_result = subprocess.run(
        ["git", "diff", "HEAD", "--color=always", "--", filepath],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    log_out = log_result.stdout.strip()
    diff_out = diff_result.stdout.strip()

    if not log_out and not diff_out:
        return

    print("\n\033[2m" + "-" * 40 + "\033[0m")
    if log_out:
        print("\033[2mgit log\033[0m")
        print(log_out)
    if diff_out:
        if log_out:
            print()
        print("\033[2mgit diff HEAD\033[0m")
        print(diff_out)


def _preview_archive(filepath, hint, query):
    # type: (str, str, str) -> None
    """Render an archive file in the preview pane."""
    if query:
        if (
            _passthrough(
                ["rga", "--pretty", "--color=always", "--context", "5", query, filepath]
            )
            != 0
        ):
            print("[Archive search requires rga with dependencies]")
    else:
        _list_archive(filepath, hint)


def _preview_directory(filepath):
    # type: (str) -> None
    """Render a directory listing in the preview pane."""
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


def _preview_binary(filepath):
    # type: (str) -> None
    """Render a truncated hexdump of a binary file (first 256 bytes)."""
    _try_run(
        [
            ["xxd", "-l", "256", filepath],
            ["hexdump", "-C", "-n", "256", filepath],
            ["od", "-t", "x1z", "-N", "256", filepath],
        ],
        "Binary preview failed",
    )


def _dispatch_preview(filepath, hint, mime, query):
    # type: (str, str, str, str) -> None
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


def _preview_stdin(hint, query):
    # type: (str, str) -> None
    """Buffer stdin to a temp file and preview it."""
    fd, tmpfile = tempfile.mkstemp(prefix="remotely-preview-", dir=str(WORK_BASE))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(sys.stdin.buffer.read())
        mime = _get_mime(tmpfile)
        if hint.endswith(".pdf"):
            mime = "application/pdf"
        _dispatch_preview(tmpfile, hint, mime, query)
    finally:
        try:
            Path(tmpfile).unlink()
        except (FileNotFoundError, OSError):
            pass


def _preview_file(filepath, query):
    # type: (str, str) -> None
    """Preview a file on the local filesystem."""
    if Path(filepath).is_dir():
        _preview_directory(filepath)
        return

    kind = classify(filepath)
    if kind is FileKind.ARCHIVE:
        _preview_archive(filepath, filepath, query)
    elif kind is FileKind.PDF:
        _preview_pdf(filepath, query)
    else:
        mime = _get_mime(filepath)
        _dispatch_preview(filepath, filepath, mime, query)


def cmd_preview(argv):
    # type: (List[str]) -> int
    """Entry point for the remotely-preview sub-command.

    Arguments:
        argv[0]  filepath  -- path to preview, or "/dev/stdin" for piped input
        argv[1]  query     -- current fzf search query (optional)
        argv[2]  hint      -- original filename when filepath is a temp file (optional)
    """
    if not argv:
        print("Usage: remotely-preview <file> [query] [hint]", file=sys.stderr)
        return 1

    file_arg = argv[0]
    query = argv[1] if len(argv) > 1 else ""
    hint = argv[2] if len(argv) > 2 else file_arg

    if not file_arg:
        return 0

    if file_arg == "/dev/stdin":
        _preview_stdin(hint, query)
        return 0

    if not Path(file_arg).exists():
        print("[File not found: {}]".format(file_arg))
        return 0

    _preview_file(file_arg, query)
    return 0
