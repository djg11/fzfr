PREFIX  ?= $(HOME)/.local
BINDIR  := $(PREFIX)/bin
SCRIPT  := remotely
# Symlinks for sub-commands that are invoked by name.
# remotely-open and remotely-copy are removed (headless API uses sub-command
# dispatch: `remotely open`, `remotely list`, etc.).
SYMLINKS := remotely-preview remotely-remote-reload remotely-remote-preview

# Dev toolchain requirement: Python 3.10+ must be active in the venv.
# Runtime target:            Python 3.6+ (the built script runs on remote hosts).
#
# Workflow for runtime testing:
#   make build           -- build + run tests under dev python (3.10+)
#   make test36          -- verify the built script under python3.6
#   make install         -- install to $(BINDIR)

.PHONY: build test test36 lint format pre-commit install-hooks dev-install \
        install uninstall check check-dev check-imports examples

# ---------------------------------------------------------------------------
# Primary targets
# ---------------------------------------------------------------------------

build: check-dev check-imports
	python3 scripts/build_single_file.py
	@if command -v pytest >/dev/null 2>&1; then \
	    pytest tests/ -q; \
	else \
	    python3 -m unittest discover -s tests -q; \
	fi

install: build
	@mkdir -p $(BINDIR)
	install -m 755 $(SCRIPT) $(BINDIR)/$(SCRIPT)
	@for name in $(SYMLINKS); do \
	    ln -sf $(SCRIPT) $(BINDIR)/$$name; \
	    echo "  symlink: $(BINDIR)/$$name -> $(SCRIPT)"; \
	done
	@echo "Installed $(BINDIR)/$(SCRIPT)"
	@echo ""
	@echo "Make sure $(BINDIR) is in your PATH:"
	@echo "  echo 'export PATH=\"$$HOME/.local/bin:\$$PATH\"' >> ~/.bashrc"

uninstall:
	@rm -f $(BINDIR)/$(SCRIPT)
	@for name in $(SYMLINKS); do \
	    rm -f $(BINDIR)/$$name; \
	    echo "  removed: $(BINDIR)/$$name"; \
	done
	@echo "Removed $(BINDIR)/$(SCRIPT)"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test:
	@command -v python3 >/dev/null 2>&1 || { echo "Error: python3 not found."; exit 1; }
	@if command -v pytest >/dev/null 2>&1; then \
	    pytest tests/ -v; \
	else \
	    python3 -m unittest discover -s tests -v; \
	fi

# Verify the BUILT SCRIPT (not the src package) runs correctly under the
# minimum supported remote Python. Requires python3.6 in PATH.
# Switch to your 3.6 interpreter before running this target, e.g.:
#   pyenv local 3.6.15 && make test36
test36:
	@command -v python >/dev/null 2>&1 || { \
	    echo "Error: python3.6 not found in PATH."; \
	    echo "Install it or use: pyenv local 3.6.15"; \
	    exit 1; }
	@test -f remotely || { \
	    echo "Error: built script 'remotely' not found. Run 'make build' first."; \
	    exit 1; }
	@echo "Verifying built script under Python 3.6..."
	python -m unittest discover -s tests -v
	@echo "Python 3.6 verification passed."

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint:
	@command -v ruff >/dev/null 2>&1 || { \
	    echo "Error: ruff not found. Install with: pip install ruff"; exit 1; }
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	@command -v ruff >/dev/null 2>&1 || { \
	    echo "Error: ruff not found. Install with: pip install ruff"; exit 1; }
	ruff check --fix src/ tests/
	ruff format src/ tests/

pre-commit:
	@command -v pre-commit >/dev/null 2>&1 || { \
	    echo "Error: pre-commit not found. Install with: pip install pre-commit"; exit 1; }
	pre-commit run --all-files

install-hooks:
	@command -v pre-commit >/dev/null 2>&1 || { \
	    echo "Error: pre-commit not found. Install with: pip install pre-commit"; exit 1; }
	pre-commit install
	@echo "Pre-commit hooks installed. Ruff will run automatically on git commit."

check-imports:
	@python3 scripts/check_imports.py

# ---------------------------------------------------------------------------
# Dev environment bootstrap
# ---------------------------------------------------------------------------

dev-install: check-dev
	@python3 -c "import sys; sys.exit(0 if hasattr(sys, 'real_prefix') or sys.prefix != sys.base_prefix else 1)" 2>/dev/null || { \
	    echo "Error: no virtual environment active."; \
	    echo "Create and activate one first:"; \
	    echo "  python3 -m venv .venv && source .venv/bin/activate"; \
	    exit 1; }
	pip install --upgrade pip setuptools wheel --quiet
	pip install -e '.[dev]' --no-build-isolation
	pre-commit install
	@echo ""
	@echo "Dev environment ready (Python $$(python3 --version))."
	@echo ""
	@echo "Available commands:"
	@echo "  make build       -- rebuild remotely + run tests (requires 3.10+)"
	@echo "  make test        -- run tests only"
	@echo "  make test36      -- verify built script under python3.6"
	@echo "  make lint        -- ruff check + format check"
	@echo "  make format      -- ruff check --fix + ruff format"
	@echo "  make pre-commit  -- run all pre-commit hooks"

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

# check-dev: verify the ACTIVE python meets the dev toolchain requirement (3.10+).
# Used by build and dev-install to catch accidental use of a 3.6 interpreter
# for development tasks that require 3.10+ (ruff, modern typing syntax, etc.).
check-dev:
	@command -v python3 >/dev/null 2>&1 || { \
	    echo "Error: python3 not found in PATH."; exit 1; }
	@python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null || { \
	    echo "Error: dev toolchain requires Python 3.10+."; \
	    echo "Found: $$(python3 --version)"; \
	    echo ""; \
	    echo "Activate a 3.10+ virtualenv, e.g.:"; \
	    echo "  python3.10 -m venv .venv && source .venv/bin/activate"; \
	    echo ""; \
	    echo "To test against Python 3.6 (runtime target), use: make test36"; \
	    exit 1; }

# check: legacy alias kept for backwards compatibility (e.g. called by old CI).
check: check-dev
	@command -v fd >/dev/null 2>&1 || \
	    echo "Warning: fd not found -- required for 'remotely list' local mode."
	@echo "Prerequisites OK"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

examples:
	python3 scripts/generate_examples.py
