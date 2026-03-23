#!/usr/bin/env python3
"""Scan src/remotely/ for late imports (imports inside function/class bodies).

Late imports are stripped by the build script and leave undefined names
in the flat built file. Run this before committing.

Usage: python3 scripts/check_imports.py
       make check-imports
"""

import ast
import sys
from pathlib import Path


SRC = Path(__file__).parent.parent / "src" / "remotely"


def check_file(path: Path) -> list[tuple[int, str]]:
    src = path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(0, f"SyntaxError: {e}")]

    # Collect all module-level import line numbers
    module_level = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_level.add(node.lineno)
        # try/except ImportError at module level (intra-package pattern)
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    module_level.add(child.lineno)

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if child is node:
                continue
            if not isinstance(child, (ast.Import, ast.ImportFrom)):
                continue
            if child.lineno in module_level:
                continue
            # Skip intra-package imports (handled by build script)
            if isinstance(child, ast.ImportFrom) and child.level and child.level > 0:
                continue
            names = ", ".join(a.name for a in child.names)
            violations.append((child.lineno, f"late import: {names}"))
    return violations


def main() -> int:
    failed = False
    for path in sorted(SRC.glob("*.py")):
        violations = check_file(path)
        for lineno, msg in violations:
            print(f"  {path.name}:{lineno}: {msg}", file=sys.stderr)
            failed = True
    if failed:
        print(
            "\nError: late imports found. Move them to module top level.\n"
            "See AGENTS.md section 3.2 for why this matters.",
            file=sys.stderr,
        )
        return 1
    print("✓ no late imports found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
