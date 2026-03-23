#!/usr/bin/env python3
# build_single_file.py - Concatenate src/remotely/ modules into a single remotely script.
#
# Run from the repository root:
#     python3 scripts/build_single_file.py
#
# The output file (remotely) is the sole distributable artefact. Never edit it
# directly -- always edit the source modules in src/remotely/ and rebuild.

import ast
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "remotely"
OUT = REPO_ROOT / "remotely"

MODULE_ORDER = [
    "_script",
    "utils",
    "workbase",
    "config",
    "tty",
    "ssh",
    "session",
    "state",
    "cache",
    "archive",
    "backends",
    "preview",
    "internal",
    "dispatch",
    "open",
    "copy",
    "remote",
    "list",
    "preview_cmd",
    "open_cmd",
    "search",
]

# Multi-line intra-package imports (from .X import ... or from remotely.X import ...)
INTRA_IMPORT_RE = re.compile(
    r"^from \.([\w]+) import \(.*?\)|^from \.([\w]+) import [^\n]+|^from remotely\.[\w]+ import [^\n]+",
    re.MULTILINE | re.DOTALL,
)

# Single-line stdlib/third-party imports
STDLIB_IMPORT_RE = re.compile(
    r"^(?:import [^\n]+|from (?!\.)[^\n]+)$",
    re.MULTILINE,
)


def _strip_docstring(source):
    # Remove the module-level docstring using ast.
    try:
        tree = ast.parse(source)
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            doc_end = tree.body[0].end_lineno
            source = "\n".join(source.splitlines()[doc_end:]).lstrip("\n")
    except SyntaxError:
        pass
    return source


def _read_module(name):
    # Read a source module, strip its docstring and all imports.
    # Imports are collected separately and placed once at the top.
    source = (SRC / f"{name}.py").read_text()
    source = _strip_docstring(source)
    source = INTRA_IMPORT_RE.sub("", source)  # strip intra-package (multi-line safe)
    source = STDLIB_IMPORT_RE.sub("", source)  # strip stdlib (single-line)
    source = re.sub(r"\n{3,}", "\n\n", source)  # collapse excess blank lines
    return source.lstrip("\n")


def _collect_imports(raw_sources):
    # Collect and deduplicate stdlib imports from raw (unstripped) source files.
    # Uses ast.parse to extract only real import statements -- not lines inside
    # docstrings or comments that happen to start with "from" or "import".
    seen = set()
    imports = []
    for src in raw_sources:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            # Skip intra-package imports
            if isinstance(node, ast.ImportFrom) and (
                node.level
                and node.level > 0  # relative: from .x import y
                or (node.module or "").startswith(
                    "remotely."
                )  # from remotely.x import y
            ):
                continue
            # Reconstruct the import line
            if isinstance(node, ast.Import):
                line = "import " + ", ".join(
                    (f"{a.name} as {a.asname}" if a.asname else a.name)
                    for a in node.names
                )
            else:
                names = ", ".join(
                    (f"{a.name} as {a.asname}" if a.asname else a.name)
                    for a in node.names
                )
                line = f"from {node.module} import {names}"
            if line not in seen:
                seen.add(line)
                imports.append(line)
    return sorted(imports)


def _get_module_doc():
    # Read the module docstring from src/remotely/__init__.py using ast.
    # This is the single source of truth -- no circular dependency on the built file.
    src = (SRC / "__init__.py").read_text()
    try:
        tree = ast.parse(src)
        doc = ast.get_docstring(tree)
        if doc:
            tq = chr(34) * 3
            return tq + "\n" + doc + "\n" + tq
    except SyntaxError:
        pass
    tq = chr(34) * 3
    return tq + "remotely - Fuzzy file search for local and remote filesystems." + tq


def build():
    print(f"Building {OUT} from {SRC}/")

    # Read raw sources for import collection
    raw_sources = [(SRC / f"{mod}.py").read_text() for mod in MODULE_ORDER]
    raw_sources.append((SRC / "__init__.py").read_text())
    stdlib_imports = _collect_imports(raw_sources)

    # Strip and prepare __init__.py (VERSION, SCRIPT_*, COMMAND_MAP, main)
    init_src = _strip_docstring((SRC / "__init__.py").read_text())
    init_src = INTRA_IMPORT_RE.sub("", init_src)
    init_src = STDLIB_IMPORT_RE.sub("", init_src)
    init_src = re.sub(r"\n{3,}", "\n\n", init_src).lstrip("\n")

    # Strip and prepare each source module
    module_sources = [(mod, _read_module(mod)) for mod in MODULE_ORDER]

    module_doc = _get_module_doc()

    # Version guard as concatenated string to avoid any triple-quote literals
    version_guard = (
        "import sys as _sys\n\n"
        "if _sys.version_info < (3, 10):  "
        "# type: ignore[comparison-overlap, unreachable]\n"
        "    print(  # type: ignore[unreachable]\n"
        '        f"Error: remotely requires Python 3.10 or later "\n'
        '        f"(found {_sys.version_info.major}.{_sys.version_info.minor}).",\n'
        "        file=_sys.stderr,\n"
        "    )\n"
        "    _sys.exit(1)\n\n"
    )

    sections = [
        "#!/usr/bin/env python3\n",
        module_doc + "\n\n",
        version_guard,
        "\n".join(stdlib_imports) + "\n\n",
    ]

    for mod_name, src in module_sources:
        sections.append(f"# {'=' * 77}\n# {mod_name}.py\n# {'=' * 77}\n\n" + src + "\n")

    sections.append(f"# {'=' * 77}\n# entry point\n# {'=' * 77}\n\n" + init_src)

    output = "".join(sections)

    # In the flat built file, relative imports don't exist -- everything is
    # already in the global scope. Replace the try/except ImportError pattern
    # used in backends.py for circular-import resolution with a direct
    # globals() lookup, which is what the except branch already does.
    output = re.sub(
        r'( *)try:\n\1    from \.[^\n]+\n\1except ImportError:\n\1    (\w+ = globals\(\)\["\w+"\])  # flat built file\n(?:\n)?',
        r"\1\2\n",
        output,
    )

    # Safety pass: strip any remaining relative imports that slipped through
    output = re.sub(r"^[ \t]*from \.[^\n]+\n", "", output, flags=re.MULTILINE)

    try:
        ast.parse(output)
    except SyntaxError as e:
        print(f"ERROR: Generated file has a syntax error: {e}", file=sys.stderr)
        sys.exit(1)

    OUT.write_text(output)
    # executable script, world-read intentional
    OUT.chmod(0o755)  # nosemgrep: remotely-world-readable-chmod

    lines = output.count("\n")
    size_kb = OUT.stat().st_size / 1024
    print(f"  Written: {OUT} ({lines} lines, {size_kb:.1f} KB)")
    print(f"  Modules: {len(MODULE_ORDER)} source modules concatenated")
    print("  Done.")


if __name__ == "__main__":
    build()
