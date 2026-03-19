import subprocess
from enum import Enum, auto
from pathlib import Path

from .utils import _passthrough, _is_text_mime

ARCHIVE_EXTENSIONS = {
    ".cbt",
    ".tbz2",
    ".tbz",
    ".tgz",
    ".txz",
    ".tar",
    ".cbz",
    ".epub",
    ".zip",
    ".cbr",
    ".rar",
    ".gz",
    ".lzma",
    ".bz2",
    ".xz",
    ".lz4",
    ".zst",
    ".7z",
    ".apk",
    ".arj",
    ".cab",
    ".cb7",
    ".chm",
    ".deb",
    ".iso",
    ".lzh",
    ".msi",
    ".pkg",
    ".rpm",
    ".udf",
    ".wim",
    ".xar",
    ".vhd",
    ".dmg",
    ".cpio",
}
COMPOUND_EXTENSIONS = {
    # DESIGN: Must be checked before ARCHIVE_EXTENSIONS because Path.suffix only
    #         returns the final component — Path("f.tar.gz").suffix == ".gz".
    #         Checking compound forms first ensures ".tar.gz" wins over ".gz".
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".tar.lz4",
    ".tar.zst",
    ".tar.br",
}

# DESIGN: Maps each extension to the command that lists its contents.
#         {filepath} is substituted with the actual path at call time in
#         _list_archive(). Keeping the template here (not inline in the
#         dispatch logic) makes it easy to add support for new formats.
ARCHIVE_LIST_COMMANDS = {
    ".cbt": ["tar", "-tjf", "{filepath}"],
    ".tar.bz2": ["tar", "-tjf", "{filepath}"],
    ".tbz2": ["tar", "-tjf", "{filepath}"],
    ".tbz": ["tar", "-tjf", "{filepath}"],
    ".tar.gz": ["tar", "-tzf", "{filepath}"],
    ".tgz": ["tar", "-tzf", "{filepath}"],
    ".tar.xz": ["tar", "-tJf", "{filepath}"],
    ".txz": ["tar", "-tJf", "{filepath}"],
    ".tar.lz4": ["tar", "--use-compress-program=lz4", "-tf", "{filepath}"],
    ".tar.zst": ["tar", "-I", "zstd", "-tf", "{filepath}"],
    ".tar.br": ["tar", "--use-compress-program=pbzip2", "-tf", "{filepath}"],
    ".tar": ["tar", "-tf", "{filepath}"],
    ".cbz": ["unzip", "-l", "{filepath}"],
    ".epub": ["unzip", "-l", "{filepath}"],
    ".zip": ["unzip", "-l", "{filepath}"],
    ".cbr": ["unrar", "l", "{filepath}"],
    ".rar": ["unrar", "l", "{filepath}"],
    ".bz2": ["bzcat", "{filepath}"],
    ".xz": ["xzcat", "{filepath}"],
    ".lz4": ["lz4", "-d", "{filepath}", "--stdout"],
    ".zst": ["zstd", "-d", "{filepath}", "--stdout"],
}

ARCHIVE_INSTALL_HINTS = {
    ".cbt": "bzip2 tar: install tar",
    ".tar.bz2": "bzip2 tar: install tar",
    ".tbz2": "bzip2 tar: install tar",
    ".tbz": "bzip2 tar: install tar",
    ".tar.gz": "gzip tar: install tar",
    ".tgz": "gzip tar: install tar",
    ".tar.xz": "xz tar: install tar",
    ".txz": "xz tar: install tar",
    ".tar.lz4": "lz4 tar: install tar + lz4",
    ".tar.zst": "zst tar: install tar + zstd",
    ".tar.br": "brotli tar: install tar + pbzip2",
    ".tar": "tar: install tar",
    ".cbz": "zip: install unzip",
    ".epub": "zip: install unzip",
    ".zip": "zip: install unzip",
    ".cbr": "rar: install unrar",
    ".rar": "rar: install unrar",
    ".bz2": "bzip2: install bzip2",
    ".xz": "xz: install xz-utils",
    ".lz4": "lz4: install lz4",
    ".zst": "zst: install zstd",
}


def _hint_suffix(hint: str) -> str:
    """Extract the longest matching extension from a filename.

    Python's Path.suffix only returns the last extension, so "file.tar.gz"
    would yield ".gz" and be misidentified. We check compound extensions
    first so ".tar.gz" takes priority over ".gz".

    The 'hint' parameter is the original filename even when we are actually
    reading from a temp file — see cmd_preview for why.
    """
    lower = hint.lower()
    for ext in COMPOUND_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    return Path(lower).suffix


# =============================================================================
# File classification
# =============================================================================


class FileKind(Enum):
    """The categories the preview and open logic cares about.

    ARCHIVE  — a compressed or packaged file whose contents should be listed
               or searched via rga/tar/unzip/7z. Determined purely by extension.
    PDF      — a Portable Document Format file. Needs pdftotext or rga for
               text extraction; plain cat produces binary noise.
    DIRECTORY — a directory entry. Should be listed via tree/exa/ls.
    BINARY   — an unknown binary file. Should be shown as a hex dump.
    TEXT     — everything else: source code, prose, config, binary files that
               fell through the archive/PDF checks. Rendered by bat → cat, or
               searched by rga → grep.
    """

    ARCHIVE = auto()
    PDF = auto()
    DIRECTORY = auto()
    BINARY = auto()
    TEXT = auto()


def classify(hint: str, mime: str = "") -> FileKind:
    """Return the FileKind for a file given its name hint and optional MIME type.

    Classification priority (matches the original dispatch order in cmd_preview):
      1. Extension check for archives — definitive, fast, no subprocess needed.
      2. Extension check for .pdf — definitive for well-named files.
      3. MIME type for application/pdf — catches headerless or oddly-named PDFs.
      4. MIME type for inode/directory — catches folders.
      5. MIME type check for non-text/non-application binary files.
      6. Everything else → TEXT.

    Arguments:
        hint  — the original filename (not a temp-file path) used for extension
                matching. May be an absolute path; only the suffix matters.
        mime  — MIME type string from file(1), e.g. "application/pdf". Optional;
                omit when the caller has not yet run MIME detection.

    DESIGN: Centralising all "what kind of file is this?" logic here means
            cmd_preview, _dispatch_preview, and _open all ask the same question
            in the same way. Previously each site had its own inline checks
            (hint.endswith(".pdf"), _is_archive(hint), mime == "application/pdf")
            that could drift out of sync.
    """
    if mime == "inode/directory":
        return FileKind.DIRECTORY
    suffix = _hint_suffix(hint)
    if suffix in ARCHIVE_EXTENSIONS or suffix in COMPOUND_EXTENSIONS:
        return FileKind.ARCHIVE
    lower = hint.lower()
    if lower.endswith(".pdf") or mime == "application/pdf":
        return FileKind.PDF

    # If MIME is available, use it to detect binary files that shouldn't be
    # opened as text (e.g. executables, images, unknown formats).
    if mime and not _is_text_mime(mime):
        return FileKind.BINARY

    return FileKind.TEXT

def _list_archive(filepath: str, hint: str) -> None:
    """List the contents of an archive file to stdout (max 50 lines).

    Dispatches to the appropriate tool based on the file extension taken from
    'hint' (the original filename). 'filepath' may point to a temp file when
    the content arrived via stdin.

    Falls back to rga if the primary tool is unavailable, and prints a
    human-readable install hint as a last resort.

    DESIGN: Extension-to-tool dispatch lives in ARCHIVE_LIST_COMMANDS so
            adding support for a new format only requires a new dict entry,
            not a new branch in this function.
    """
    suffix = _hint_suffix(hint)

    def try_pass(cmd) -> bool:
        """Run cmd, cap at 50 lines. Return True on success."""
        return _passthrough(cmd, head_n=50) == 0

    def rga_fallback(msg: str) -> None:
        """Try rga as a universal fallback; if that fails, print an install hint."""
        if _passthrough(["rga", "--pretty", "--color=always", ".", filepath]) != 0:
            print(f"[{msg}]")

    if suffix in ARCHIVE_LIST_COMMANDS:
        # Substitute the actual filepath into the command template.
        cmd = [
            arg.replace("{filepath}", filepath) for arg in ARCHIVE_LIST_COMMANDS[suffix]
        ]
        if not try_pass(cmd):
            rga_fallback(ARCHIVE_INSTALL_HINTS.get(suffix, "unknown"))
        return

    # .gz and .lzma: try gunzip -l first (shows compressed/uncompressed sizes),
    # fall back to streaming the decompressed content via zcat.
    if suffix in (".gz", ".lzma"):
        if not try_pass(["gunzip", "-l", filepath]):
            if not try_pass(["zcat", filepath]):
                rga_fallback("gzip: install gzip")
        return

    # These formats are all handled by 7z.
    if suffix in (
        ".7z",
        ".apk",
        ".arj",
        ".cab",
        ".cb7",
        ".chm",
        ".deb",
        ".iso",
        ".lzh",
        ".msi",
        ".pkg",
        ".rpm",
        ".udf",
        ".wim",
        ".xar",
        ".vhd",
        ".dmg",
    ):
        if not try_pass(["7z", "l", filepath]):
            rga_fallback("7z: install p7zip")
        return

    if suffix == ".cpio":
        # DESIGN: cpio --list reads from stdin rather than a filename argument,
        #         so we open the file ourselves and pass it as stdin rather than
        #         adding it to the command list like the other archive tools.
        try:
            with open(filepath, "rb") as fin:
                p1 = subprocess.Popen(
                    ["cpio", "--list"],
                    stdin=fin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                p2 = subprocess.Popen(["head", "-n", "50"], stdin=p1.stdout)
                assert p1.stdout is not None
                p1.stdout.close()
                p2.wait()
                p1.wait()
        except OSError:
            rga_fallback("cpio: install cpio")
        return

    rga_fallback("unknown archive format")


