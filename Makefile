PREFIX  ?= $(HOME)/.local
BINDIR  := $(PREFIX)/bin
SCRIPT  := remotely

PYTHON  ?= python3
PIP     := $(PYTHON) -m pip
TESTDIR := tests
COVERAGE_PACKAGE := remotely

# Symlinks for sub-commands that are invoked by name.
SYMLINKS := remotely-preview remotely-remote-reload remotely-remote-preview

# Dev toolchain requirement: Python 3.10+ must be active in the venv.
# Runtime target:            Python 3.6+ (the built script runs on remote hosts).
#
# Workflow for runtime testing:
#   make build           -- build + run tests under dev python (3.10+)
#   make test            -- run tests (uses pytest-cov if available)
#   make coverage        -- run tests with coverage report
#   make test36          -- verify the built script under python3.6
#   make install         -- install to $(BINDIR)

.PHONY: all build test test36 coverage coverage-html lint format pre-commit \
        install-hooks dev-install install uninstall check check-dev \
        check-imports examples

all: build

# ---------------------------------------------------------------------------
# Primary targets
# ---------------------------------------------------------------------------

build: check-dev check-imports
	$(PYTHON) scripts/build_single_file.py
	@$(MAKE) test TEST_COVERAGE=0

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

# TEST_COVERAGE=1 enables pytest-cov when available.
# build/test36 override this as needed.
TEST_COVERAGE ?= 1

test:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
	    echo "Error: $(PYTHON) not found."; \
	    exit 1; }
	@if $(PYTHON) -c "import pytest" >/dev/null 2>&1; then \
	    if [ "$(TEST_COVERAGE)" = "1" ] && $(PYTHON) -c "import pytest_cov" >/dev/null 2>&1; then \
	        $(PYTHON) -m pytest $(TESTDIR)/ -v --cov=$(COVERAGE_PACKAGE) --cov-report=term-missing; \
	    else \
	        $(PYTHON) -m pytest $(TESTDIR)/ -v; \
	    fi; \
	else \
	    $(PYTHON) -m unittest discover -s $(TESTDIR) -v; \
	fi

coverage:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
	    echo "Error: $(PYTHON) not found."; \
	    exit 1; }
	@$(PYTHON) -c "import pytest" >/dev/null 2>&1 || { \
	    echo "Error: pytest not found. Install with: pip install pytest"; \
	    exit 1; }
	@$(PYTHON) -c "import pytest_cov" >/dev/null 2>&1 || { \
	    echo "Error: pytest-cov not found. Install with: pip install pytest-cov"; \
	    exit 1; }
	$(PYTHON) -m pytest $(TESTDIR)/ -v --cov=$(COVERAGE_PACKAGE) --cov-report=term-missing --cov-report=html

coverage-html: coverage
	@echo "Coverage HTML written to htmlcov/index.html"

# Verify the BUILT SCRIPT (not the src package) runs correctly under the
# minimum supported remote Python. Requires python3.6 in PATH.
# Switch to your 3.6 interpreter before running this target, e.g.:
#   pyenv local 3.6.15 && make test36
test36:
	@command -v python >/dev/null 2>&1 || { \
	    echo "Error: python3.6 not found in PATH."; \
	    echo "Install it or use: pyenv local 3.6.15"; \
	    exit 1; }
	@test -f $(SCRIPT) || { \
	    echo "Error: built script '$(SCRIPT)' not found. Run 'make build' first."; \
	    exit 1; }
	@echo "Verifying built script under Python 3.6..."
	python -m unittest discover -s $(TESTDIR) -v
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
	@$(PYTHON) scripts/check_imports.py

# ---------------------------------------------------------------------------
# Security scanning
# ---------------------------------------------------------------------------

semgrep:
	@command -v semgrep >/dev/null 2>&1 || { \
	    echo "Error: semgrep not found. Install with: pip install semgrep"; exit 1; }
	semgrep scan --config .semgrep/semgrep.yml --error .

# ---------------------------------------------------------------------------
# Dev environment bootstrap
# ---------------------------------------------------------------------------

dev-install: check-dev
	@$(PYTHON) -c "import sys; sys.exit(0 if hasattr(sys, 'real_prefix') or sys.prefix != sys.base_prefix else 1)" 2>/dev/null || { \
	    echo "Error: no virtual environment active."; \
	    echo "Create and activate one first:"; \
	    echo "  $(PYTHON) -m venv .venv && source .venv/bin/activate"; \
	    exit 1; }
	$(PIP) install --upgrade pip setuptools wheel --quiet
	$(PIP) install -e '.[dev]' --no-build-isolation
	pre-commit install
	@echo ""
	@echo "Dev environment ready (Python $$($(PYTHON) --version))."
	@echo ""
	@echo "Available commands:"
	@echo "  make build       -- rebuild remotely + run tests (requires 3.10+)"
	@echo "  make test        -- run tests only"
	@echo "  make coverage    -- run tests with coverage"
	@echo "  make test36      -- verify built script under python3.6"
	@echo "  make lint        -- ruff check + format check"
	@echo "  make format      -- ruff check --fix + ruff format"
	@echo "  make semgrep     -- run custom security rules"
	@echo "  make pre-commit  -- run all pre-commit hooks"

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

# check-dev: verify the ACTIVE python meets the dev toolchain requirement (3.10+).
check-dev:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
	    echo "Error: $(PYTHON) not found in PATH."; exit 1; }
	@$(PYTHON) -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null || { \
	    echo "Error: dev toolchain requires Python 3.10+."; \
	    echo "Found: $$($(PYTHON) --version)"; \
	    echo ""; \
	    echo "Activate a 3.10+ virtualenv, e.g.:"; \
	    echo "  python3.10 -m venv .venv && source .venv/bin/activate"; \
	    echo ""; \
	    echo "To test against Python 3.6 (runtime target), use: make test36"; \
	    exit 1; }

# check: legacy alias kept for backwards compatibility.
check: check-dev
	@command -v fd >/dev/null 2>&1 || \
	    echo "Warning: fd not found -- required for 'remotely list' local mode."
	@echo "Prerequisites OK"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

examples:
	$(PYTHON) scripts/generate_examples.py
