#!/usr/bin/env python3
"""
generate_demo.py — Generate the remotely demo directory.

Run from the repository root:
    python3 generate_demo.py

This creates demo/ with a small directory tree that exercises every
remotely feature: content search, filename search, archive preview, PDF
preview, binary/hex preview, and tricky filenames.

All files are generated from source — no binary blobs are stored in the
repository. Re-running this script is idempotent (existing files are
overwritten).
"""

import textwrap
import zipfile
from pathlib import Path


ROOT = Path(__file__).parent.parent / "demo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))
    print(f"  {path.relative_to(ROOT.parent)}")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    print(f"  {path.relative_to(ROOT.parent)}")


# ---------------------------------------------------------------------------
# src/ — source files (syntax-highlighted preview, content search)
# ---------------------------------------------------------------------------


def make_src() -> None:
    write(
        ROOT / "src" / "search_engine.py",
        """
        \"\"\"A simple inverted index search engine.

        Keywords: search, index, query, token, document
        \"\"\"

        from collections import defaultdict
        from typing import Iterator


        class InvertedIndex:
            \"\"\"Maps tokens to the documents that contain them.\"\"\"

            def __init__(self) -> None:
                self._index: dict[str, set[int]] = defaultdict(set)
                self._docs: dict[int, str] = {}

            def add(self, doc_id: int, text: str) -> None:
                \"\"\"Index a document by tokenising its text.\"\"\"
                self._docs[doc_id] = text
                for token in self._tokenise(text):
                    self._index[token].add(doc_id)

            def search(self, query: str) -> list[str]:
                \"\"\"Return documents matching all query tokens (AND semantics).\"\"\"
                tokens = list(self._tokenise(query))
                if not tokens:
                    return []
                result_ids = self._index.get(tokens[0], set()).copy()
                for token in tokens[1:]:
                    result_ids &= self._index.get(token, set())
                return [self._docs[i] for i in sorted(result_ids)]

            @staticmethod
            def _tokenise(text: str) -> Iterator[str]:
                for word in text.lower().split():
                    yield word.strip(".,;:!?\\"'")


        if __name__ == "__main__":
            idx = InvertedIndex()
            idx.add(1, "fuzzy search over remote filesystems")
            idx.add(2, "fast file search with fd and fzf")
            idx.add(3, "search and preview files via SSH")
            print(idx.search("search"))   # [1, 2, 3]
            print(idx.search("remote"))   # [1]
    """,
    )

    write(
        ROOT / "src" / "notes.txt",
        """\
        Meeting Notes - Project Kickoff
        ================================
        Date: 2024-03-15
        Attendees: Alice, Bob, Carol, Dave

        Agenda
        ------
        1. Project overview and goals
        2. Timeline discussion
        3. Resource allocation
        4. Risk assessment

        Summary
        -------
        The team agreed on a 12-week delivery timeline. Alice will lead the
        backend development, Bob handles frontend, Carol manages QA and testing,
        Dave coordinates deployment and infrastructure.

        Action Items
        ------------
        - Alice: Set up repository and CI pipeline by end of week
        - Bob: Deliver initial wireframes by Friday
        - Carol: Define test strategy document
        - Dave: Provision staging environment

        Next meeting scheduled for 2024-03-22 at 10:00.

        Notes
        -----
        Budget approved for additional tooling. Cloud costs to be monitored
        weekly. Any blockers should be escalated immediately to project lead.
    """,
    )

    write(
        ROOT / "src" / "app.log",
        """\
        2024-03-15T08:01:02 INFO  service started on port 8080
        2024-03-15T08:01:03 INFO  database connection pool initialized (size=10)
        2024-03-15T08:01:03 INFO  cache backend connected at cache.internal:6379
        2024-03-15T08:03:21 INFO  GET /api/v1/status 200 4ms
        2024-03-15T08:05:44 INFO  GET /api/v1/users 200 12ms
        2024-03-15T08:07:11 WARN  slow query detected: 320ms (threshold=200ms)
        2024-03-15T08:07:11 DEBUG query: SELECT * FROM events WHERE created_at > $1 ORDER BY created_at DESC
        2024-03-15T08:12:05 INFO  POST /api/v1/jobs 201 8ms
        2024-03-15T08:15:33 ERROR failed to deliver webhook: connection refused (url=https://hooks.example.com/notify)
        2024-03-15T08:15:33 INFO  webhook retry scheduled in 60s
        2024-03-15T08:16:33 INFO  webhook delivered on retry (attempt=2)
        2024-03-15T09:00:00 INFO  scheduled job started: daily-report
        2024-03-15T09:00:04 INFO  scheduled job completed: daily-report (duration=4.1s rows=18432)
        2024-03-15T09:45:17 WARN  memory usage high: 78% (threshold=75%)
        2024-03-15T10:00:00 INFO  health check passed
        2024-03-15T11:22:08 ERROR unhandled exception in worker-3
        2024-03-15T11:22:08 DEBUG traceback: ValueError: invalid literal for int() with base 10: 'N/A'
        2024-03-15T11:22:09 INFO  worker-3 restarted
        2024-03-15T12:00:00 INFO  health check passed
        2024-03-15T14:30:55 INFO  deployment started: v1.4.2
        2024-03-15T14:31:10 INFO  deployment complete: v1.4.2 (duration=15s)
        2024-03-15T14:31:11 INFO  service restarted on port 8080
    """,
    )


# ---------------------------------------------------------------------------
# docs/ — documentation files (Markdown + minimal PDF)
# ---------------------------------------------------------------------------


def make_docs() -> None:
    write(
        ROOT / "docs" / "architecture.md",
        """
        # Architecture Overview

        ## Search Pipeline

        remotely uses a two-phase pipeline for content search:

        1. **File discovery** — `fd` lists candidate files respecting `.gitignore`
           and any configured exclude patterns.
        2. **Content matching** — `rga` searches inside each file, including PDFs,
           archives, and Office documents.

        ## Remote Search

        When a remote host is specified, remotely transfers itself via SSH stdin on
        first use and caches it at `~/.cache/remotely/`. Subsequent calls use the
        cached copy — only a 200-byte bootstrap script is sent each time.

        ## Session State

        All runtime state (mode, extension filter, path format) is stored in a
        JSON file under `/dev/shm/remotely/session-*/state.json`. Each fzf callback
        reads this file to reconstruct the current context.

        ## Keywords

        preview, search, index, remote, ssh, pipeline, session, state, cache
    """,
    )

    # Minimal valid PDF — constructed entirely from printable text tokens.
    # No external tools required; no binary blobs stored in the repository.
    # The xref offsets must be exact for strict PDF readers.
    objects = [
        b"",  # placeholder for object 0 (free)
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]\n"
        b"   /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        None,  # stream object — built below
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    stream_content = (
        b"BT\n"
        b"/F1 16 Tf\n"
        b"72 740 Td\n"
        b"(Quarterly Infrastructure Report) Tj\n"
        b"0 -20 Td\n"
        b"/F1 12 Tf\n"
        b"(Q1 2024 - Internal Summary) Tj\n"
        b"0 -28 Td\n"
        b"/F1 13 Tf\n"
        b"(Executive Summary) Tj\n"
        b"0 -18 Td\n"
        b"/F1 11 Tf\n"
        b"(This report summarizes infrastructure performance, incident activity,) Tj\n"
        b"0 -16 Td\n"
        b"(and resource utilization for Q1 2024. Overall system availability) Tj\n"
        b"0 -16 Td\n"
        b"(reached 99.94%, exceeding the target SLA of 99.9%. Two incidents) Tj\n"
        b"0 -16 Td\n"
        b"(were recorded, both resolved within the defined response window.) Tj\n"
        b"0 -26 Td\n"
        b"/F1 13 Tf\n"
        b"(Availability) Tj\n"
        b"0 -18 Td\n"
        b"/F1 11 Tf\n"
        b"(All production services maintained uptime above the 99.9% threshold.) Tj\n"
        b"0 -16 Td\n"
        b"(Scheduled maintenance windows accounted for 0.04% of total downtime.) Tj\n"
        b"0 -16 Td\n"
        b"(No unplanned outages exceeded 15 minutes.) Tj\n"
        b"0 -26 Td\n"
        b"/F1 13 Tf\n"
        b"(Incident Summary) Tj\n"
        b"0 -18 Td\n"
        b"/F1 11 Tf\n"
        b"(INC-041  2024-01-18  P2  8 min   Resolved) Tj\n"
        b"0 -16 Td\n"
        b"(INC-057  2024-02-29  P3  22 min  Resolved) Tj\n"
        b"0 -26 Td\n"
        b"/F1 13 Tf\n"
        b"(Resource Utilization) Tj\n"
        b"0 -18 Td\n"
        b"/F1 11 Tf\n"
        b"(Average CPU utilization across the cluster was 41%. Memory usage) Tj\n"
        b"0 -16 Td\n"
        b"(peaked at 78% on March 15 during the daily batch job, triggering a) Tj\n"
        b"0 -16 Td\n"
        b"(warning alert. Storage capacity remains at 54% with projected) Tj\n"
        b"0 -16 Td\n"
        b"(headroom of 8 months at current growth rates.) Tj\n"
        b"0 -26 Td\n"
        b"/F1 13 Tf\n"
        b"(Recommendations) Tj\n"
        b"0 -18 Td\n"
        b"/F1 11 Tf\n"
        b"(- Increase memory threshold alert to 80% to reduce alert noise.) Tj\n"
        b"0 -16 Td\n"
        b"(- Review slow query log - three recurring queries identified.) Tj\n"
        b"0 -16 Td\n"
        b"(- Evaluate horizontal scaling for the job worker pool ahead of Q2.) Tj\n"
        b"ET\n"
    )
    objects[4] = (
        b"4 0 obj\n<< /Length " + str(len(stream_content)).encode() + b" >>\n"
        b"stream\n" + stream_content + b"endstream\nendobj\n"
    )

    body = b"%PDF-1.4\n"
    offsets = [0] * len(objects)
    for i, obj in enumerate(objects):
        if i == 0:
            continue
        offsets[i] = len(body)
        body += obj

    startxref = len(body)
    xref = b"xref\n0 6\n"
    xref += b"0000000000 65535 f \n"
    for i in range(1, 6):
        xref += f"{offsets[i]:010d} 00000 n \n".encode()

    trailer = (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n" + str(startxref).encode() + b"\n%%EOF\n"
    )

    write_bytes(ROOT / "docs" / "report.pdf", body + xref + trailer)


# ---------------------------------------------------------------------------
# config/ — configuration demo
# ---------------------------------------------------------------------------


def make_config() -> None:
    write(
        ROOT / "config" / "remotely.toml",
        """
        # remotely example configuration
        # Place at: ~/.config/remotely/config (use JSON format, not TOML)

        [ssh]
        multiplexing = true
        control_persist = 60

        [search]
        default_mode = "content"
        show_hidden = false
        path_format = "relative"
        exclude_patterns = [".git", "node_modules", "__pycache__", "*.pyc"]

        [keybindings]
        toggle_mode   = "ctrl-t"
        toggle_ftype  = "ctrl-d"
        toggle_hidden = "ctrl-h"
        filter_ext    = "ctrl-f"
        exit          = "esc"
    """,
    )


# ---------------------------------------------------------------------------
# data/ — various data formats
# ---------------------------------------------------------------------------


def make_data() -> None:
    write(
        ROOT / "data" / "metadata.json",
        """
        {
          "name": "remotely",
          "description": "Fuzzy file search for local and remote filesystems",
          "version": "1.2.0",
          "keywords": ["search", "fuzzy", "remote", "ssh", "preview", "terminal"],
          "dependencies": {
            "required": ["fzf", "fd"],
            "optional": ["bat", "rga", "pdftotext", "tmux"]
          }
        }
    """,
    )

    # ZIP archive — contents are plain text, created entirely in Python.
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "readme.txt",
            "remotely archive example\nKeywords: archive, zip, search, preview\n",
        )
        zf.writestr(
            "notes.txt",
            "This file is inside a zip archive.\n"
            "remotely can search inside archives using rga.\n",
        )
        zf.writestr(
            "config.json", '{"key": "value", "search": true, "remote": false}\n'
        )
    write_bytes(ROOT / "data" / "archive.zip", buf.getvalue())

    # Binary file — ELF magic bytes followed by a repeating byte pattern.
    # remotely detects this as binary and shows a hex dump preview.
    # Constructed entirely from known byte sequences — no external binary.
    binary = (
        b"\x7fELF"  # ELF magic number
        + b"\x02\x01\x01"  # 64-bit, little-endian, ELF version 1
        + b"\x00" * 9  # padding / ABI version
        + bytes(range(256))  # all 256 byte values (0x00–0xff)
        + b"\xde\xad\xbe\xef\xca\xfe\xba\xbe"  # recognisable markers
    )
    write_bytes(ROOT / "data" / "sample.bin", binary)


# ---------------------------------------------------------------------------
# tricky names/ — edge-case filenames
# ---------------------------------------------------------------------------


def make_tricky() -> None:
    tricky_dir = ROOT / "tricky names"
    files = {
        "spaces in name.txt": "File with spaces.\nKeywords: spaces, filename\n",
        "semi;colon.txt": "File with a semicolon.\nKeywords: semicolon, special\n",
        'quote"file.txt': "File with a double quote.\nKeywords: quote, special\n",
        "squote'file.txt": "File with a single quote.\nKeywords: quote, special\n",
        "$(echo safe).txt": "File with shell metacharacters.\nKeywords: injection, safe\n",
        "`echo safe`.txt": "File with backticks.\nKeywords: backtick, safe\n",
        "--help.txt": "File whose name looks like a flag.\nKeywords: flag, dashes\n",
    }
    for name, content in files.items():
        write(tricky_dir / name, content)


# ---------------------------------------------------------------------------
# demo/README.md
# ---------------------------------------------------------------------------


def make_readme() -> None:
    write(
        ROOT / "README.md",
        """
        # remotely demo

        A sample directory tree for trying out remotely features.
        Generated by `python3 generate_demo.py` — no binary blobs stored.

        ```
        demo/
          src/
            search_engine.py      Python — inverted index implementation
            notes.txt             Text   — meeting notes
            app.log               Log    — application log with errors/warnings
          docs/
            architecture.md       Markdown — remotely architecture overview
            report.pdf            PDF      — quarterly report (preview with pdftotext / rga)
          config/
            remotely.toml             TOML     — example configuration
          data/
            metadata.json         JSON     — project metadata
            archive.zip           ZIP      — search contents with rga
            sample.bin            Binary   — triggers hex dump preview
          tricky names/
            spaces in name.txt    Filename with spaces
            semi;colon.txt        Filename with semicolon
            quote"file.txt        Filename with double quote
            squote'file.txt       Filename with single quote
            $(echo safe).txt      Filename with shell metacharacters
            `echo safe`.txt       Filename with backticks
            --help.txt            Filename that looks like a flag
        ```

        ## Try it

        ```sh
        # Content search — find files mentioning "deployment"
        remotely local demo content

        # Filename search — fuzzy-filter by name
        remotely local demo name

        # Filter by extension — press CTRL-F and type "py"
        remotely local demo

        # Browse directories — press CTRL-D
        remotely local demo

        # Tricky filenames — verify nothing executes
        remotely local "demo/tricky names" name
        ```

        ## Expected preview behaviour

        | File | Preview |
        |------|---------|
        | `*.py`, `*.rs`, `*.sh` | Syntax-highlighted via `bat` |
        | `architecture.md` | Syntax-highlighted via `bat` |
        | `example.pdf` | Extracted text via `pdftotext` or `rga` |
        | `archive.zip` | File listing via `unzip -l` |
        | `sample.bin` | Hex dump via `xxd` or `hexdump` |
        | `tricky names/*` | All display and preview correctly |
    """,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Generating demo in {ROOT}/")
    make_src()
    make_docs()
    make_config()
    make_data()
    make_tricky()
    make_readme()
    print("\nDone. Run: remotely local demo")
