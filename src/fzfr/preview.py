"""fzfr.preview — fzfr-preview sub-command: render file content for fzf.

fzf calls this once per cursor movement. Dispatch order:

    PDF          → pdftotext | rga --pretty (text extraction + highlighting)
    Archive      → list contents via the appropriate tool (tar/7z/unzip/etc.)
    Text/empty   → bat --color=always (syntax highlighting)
    Directory    → eza/exa/tree/ls
    Binary       → xxd/hexdump (hex dump, first 512 bytes)

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

    With a query: use rga for highlighted, context-aware search results;
    fall back to pdftotext + grep if rga is unavailable.

    Without a query: extract the first 50 lines of text via pdftotext;
    fall back to rga for scanned/image-only PDFs that have no text layer.

    DESIGN: pdftotext handles native-text PDFs; rga extends coverage to scanned
            documents (via OCR plugins) and adds match highlighting. Plain cat
            cannot be used because PDFs are binary; a text-extraction step is
            always required.
    """
    if query:
        # Prefer rga: handles OCR'd and mixed PDFs, adds colour highlighting.
        # Fall back to pdftotext + grep for systems without rga.
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
        # Prefer pdftotext: fast and produces clean line-oriented output.
        # Fall back to rga for scanned/image-only PDFs that have no text layer.
        out, rc = _capture(["pdftotext", filepath, "-"], max_bytes=_CAPTURE_PDF_MAX)
        if rc == 0 and out.strip():
            print("\n".join(out.splitlines()[:50]))
            return

        _try_run(
            [["rga", "--pretty", "--color=always", ".", filepath]],
            "PDF: no text layer, may be scanned or encrypted",
        )


def _preview_text(filepath: str, query: str) -> None:
    """Render a text file (or any non-archive, non-PDF file) in the preview pane.

    With a query: show only the matching lines with surrounding context so the
    developer can see why this file was returned by the content search.
    rga is preferred because it handles more file types and adds highlighting;
    grep is the fallback for plain text files.

    Without a query: show the full file with syntax highlighting via bat.
    Falls back to plain cat if bat is not installed.

    DESIGN: --color=always is required for both rga and grep because fzf's
            preview sub-shell connects stdout to a pipe, not a TTY. Without
            this flag both tools suppress all ANSI colour codes automatically.
    """
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


def _preview_git_context(filepath: str) -> None:
    """Append a git context block to the preview pane for the given file.

    Shows the last 5 commits that touched the file and the current
    uncommitted diff. Both sections are omitted silently when git is
    unavailable, the file is not tracked, or the working directory is
    not inside a git repository.

    DESIGN: Runs git with stderr suppressed so no error messages leak into
    the preview pane. Uses subprocess.run with capture_output rather than
    _passthrough so we can check for empty output before printing the
    separator — avoiding a trailing divider when the file is outside a repo.
    """
    if "git" not in AVAILABLE_TOOLS:
        return

    log_result = subprocess.run(
        ["git", "log", "--oneline", "--color=always", "-5", "--", filepath],
        capture_output=True,
        text=True,
    )
    log_out = log_result.stdout.strip()

    diff_result = subprocess.run(
        ["git", "diff", "HEAD", "--color=always", "--", filepath],
        capture_output=True,
        text=True,
    )
    diff_out = diff_result.stdout.strip()

    if not log_out and not diff_out:
        return  # not in a repo, or file is untracked with no changes

    print("\n\033[2m" + "─" * 40 + "\033[0m")  # dim separator

    if log_out:
        print("\033[2mgit log\033[0m")
        print(log_out)

    if diff_out:
        if log_out:
            print()
        print("\033[2mgit diff HEAD\033[0m")
        print(diff_out)

def _preview_archive(filepath: str, hint: str, query: str) -> None:
    """Render an archive file in the preview pane.

    Extracted from cmd_preview so the archive path can be taken without
    first calling _get_mime() — the extension is already definitive.
    """
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


def _preview_directory(filepath: str) -> None:
    """Render a directory listing in the preview pane.

    Tries modern listing tools (eza, exa) first for icons and color, then
    falls back to tree(1) for structure, and finally ls(1) as a last resort.

    DESIGN: --color=always is forced because fzf's preview sub-shell is a pipe.
            -L 2 (for tree/eza) ensures we don't dump the entire tree of a
            deep directory into the pane.
    """
    _try_run(
        [
            ["eza", "--color=always", "--tree", "--level=2", "--icons", filepath],
            ["exa", "--color=always", "--tree", "--level=2", filepath],
            ["tree", "-L", "2", "-C", filepath],
            ["ls", "-la", "--color=always", filepath],
            ["ls", "-la", filepath],  # fallback if ls doesn't support --color
        ],
        "Directory preview failed",
    )


def _preview_binary(filepath: str) -> None:
    """Render a truncated hexdump of a binary file.

    Capped at 256 bytes (16 lines) to provide a quick look at file headers
    and content without flooding the preview pane or loading large files.
    """
    _try_run(
        [
            ["xxd", "-l", "256", filepath],
            ["hexdump", "-C", "-n", "256", filepath],
            ["od", "-t", "x1z", "-N", "256", filepath],
        ],
        "Binary preview failed",
    )


def _dispatch_preview(filepath: str, hint: str, mime: str, query: str) -> None:
    """Dispatch to the correct renderer once MIME type is known.

    Called from cmd_preview after MIME detection. Separated so both the
    stdin path (which always needs MIME) and the ambiguous-extension path
    can share the same branching logic without duplication.
    """
    kind = classify(hint, mime)
    if kind is FileKind.ARCHIVE:
        _preview_archive(filepath, hint, query)
    elif kind is FileKind.PDF:
        _preview_pdf(filepath, query)
    elif kind is FileKind.DIRECTORY:
        _preview_directory(filepath)
    else:
        # For TEXT kind, we further distinguish between true text and binary
        # if the MIME type is available.
        if mime and not _is_text_mime(mime):
            _preview_binary(filepath)
        else:
            _preview_text(filepath, query)


def cmd_preview(argv: list[str]) -> int:
    """Entry point for the fzfr-preview sub-command.

    Called by fzf once per cursor movement with the path of the highlighted
    file. Dispatches to the correct renderer based on MIME type and extension.

    Arguments:
        argv[0]  filepath  — path to preview, or "/dev/stdin" for piped input
        argv[1]  query     — current fzf search query (optional, for highlighting)
        argv[2]  hint      — original filename when filepath is a temp file (optional)

    The 'hint' parameter exists for the stdin case: when content is piped in
    we buffer it to a temp file so archive/PDF tools can seek in it, but we
    still need the original filename to determine the file type by extension.

    DESIGN: Dispatches on MIME type rather than extension alone so that files
            without extensions (or with misleading ones) are handled correctly.
            Extension hints are used only as a tiebreaker when file(1) is
            inconclusive (e.g. headerless PDFs).
    """
    if not argv:
        print("Usage: fzfr-preview <file> [query] [hint]", file=sys.stderr)
        return 1

    file_arg = argv[0]
    query = argv[1] if len(argv) > 1 else ""
    hint = argv[2] if len(argv) > 2 else file_arg

    if not file_arg:
        return 0

    tmpfile = None
    filepath = file_arg

    # LIMITATION: Filenames containing a literal newline character are split by
    #             fd into multiple fzf entries (one per fragment), because fd uses
    #             newlines as its output separator. Each fragment resolves to a
    #             path that does not exist on disk. We catch that here and show a
    #             clear message rather than letting tools fail with cryptic errors.
    #             Root fix would require fd --print0 + fzf --read0, but --read0 is
    #             a global fzf flag that breaks content-mode (rga/grep) output.
    if file_arg != "/dev/stdin" and not Path(filepath).exists():
        print(f"[File not found: {filepath}]")
        return 0

    try:
        if file_arg == "/dev/stdin":
            # DESIGN: Archive and PDF tools require a seekable file descriptor;
            #         stdin is a pipe and is not seekable. We buffer the content
            #         to a RAM-backed temp file in WORK_BASE so those tools can
            #         seek freely without touching disk on Linux.
            fd, tmpfile = tempfile.mkstemp(prefix="fzfr-preview-", dir=str(WORK_BASE))
            with os.fdopen(fd, "wb") as fh:
                fh.write(sys.stdin.buffer.read())
            filepath = tmpfile
            # For stdin we must detect type by content; extension alone is
            # unreliable. file(1) is the tiebreaker for ambiguous content.
            mime = _get_mime(tmpfile)
            # DESIGN: file(1) may not identify a PDF by content alone if it
            #         lacks a standard %PDF header (e.g. some encrypted PDFs).
            #         The original filename extension is honoured as a tiebreaker.
            if hint.endswith(".pdf"):
                mime = "application/pdf"
            _dispatch_preview(filepath, hint, mime, query)
        else:
            hint = filepath  # use the real path for extension matching
            # PERF: Check if it's a directory first. This is cheap for local
            #       files and avoids calling file(1) or extension matching
            #       for directory entries in CTRL-D mode.
            if Path(filepath).is_dir():
                _preview_directory(filepath)
                return 0

            # PERF: classify() checks extension first for the two unambiguous
            #       categories (archives and PDFs) before forking file(1). For
            #       the common case of source files and documents this avoids a
            #       subprocess on every cursor movement — file(1) is only called
            #       when classify() returns TEXT and the extension is ambiguous.
            kind = classify(hint)
            if kind is FileKind.ARCHIVE:
                _preview_archive(filepath, hint, query)
            elif kind is FileKind.PDF:
                _preview_pdf(filepath, query)
            else:
                # Extension is ambiguous (no extension, or e.g. ".dat").
                # Fall back to MIME detection to distinguish text from binary.
                mime = _get_mime(filepath)
                _dispatch_preview(filepath, hint, mime, query)
        return 0

    finally:
        # Always delete the temp file, even if an exception propagated above.
        if tmpfile:
            Path(tmpfile).unlink(missing_ok=True)
