PREFIX  ?= $(HOME)/.local
BINDIR  := $(PREFIX)/bin
SCRIPT  := remotely
SYMLINKS := remotely-preview remotely-open remotely-remote-reload remotely-remote-preview remotely-copy

.PHONY: install uninstall check test build lint format pre-commit install-hooks dev-install check-imports examples

build: check-imports
	python3 scripts/build_single_file.py
	@if command -v pytest >/dev/null 2>&1; then \
	    pytest tests/ -q; \
	else \
	    python3 -m unittest discover -s tests -q; \
	fi

install: build check
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

test:
	@command -v python3 >/dev/null 2>&1 || { echo "Error: python3 not found."; exit 1; }
	@if command -v pytest >/dev/null 2>&1; then \
	    pytest tests/ -v; \
	else \
	    python3 -m unittest discover -s tests -v; \
	fi

lint:
	@command -v ruff >/dev/null 2>&1 || { \
	    echo "Error: ruff not found. Install with: pip install ruff"; exit 1; }
	ruff check src/ tests/
	ruff format --check src/ tests/

pre-commit:
	@command -v pre-commit >/dev/null 2>&1 || { \
	    echo "Error: pre-commit not found. Install with: pip install pre-commit"; exit 1; }
	pre-commit run --all-files

install-hooks:
	@command -v pre-commit >/dev/null 2>&1 || { \
	    echo "Error: pre-commit not found. Install with: pip install pre-commit"; exit 1; }
	pre-commit install
	@echo "Pre-commit hooks installed. Ruff will run automatically on git commit."

dev-install:
	@command -v python3 >/dev/null 2>&1 || { echo "Error: python3 not found."; exit 1; }
	@python3 -c "import sys; sys.exit(0 if hasattr(sys, 'real_prefix') or sys.prefix != sys.base_prefix else 1)" 2>/dev/null || { \
	    echo "Error: no virtual environment active."; \
	    echo "Create and activate one first:"; \
	    echo "  python3 -m venv venv && source venv/bin/activate"; \
	    exit 1; }
	pip install --upgrade pip setuptools wheel --quiet
	pip install -e '.[dev]' --no-build-isolation
	pre-commit install
	@echo ""
	@echo "Dev environment ready. Available commands:"
	@echo "  make build       -- rebuild remotely + run tests"
	@echo "  make test        -- run tests only"
	@echo "  make lint        -- ruff check + format check"
	@echo "  make format      -- ruff check --fix + ruff format"
	@echo "  make pre-commit  -- run all pre-commit hooks"

format:
	@command -v ruff >/dev/null 2>&1 || { \
	    echo "Error: ruff not found. Install with: pip install ruff"; exit 1; }
	ruff check --fix src/ tests/
	ruff format src/ tests/

check-imports:
	@python3 scripts/check_imports.py

examples:
	python3 scripts/generate_examples.py

check:
	@command -v python3 >/dev/null 2>&1 || { \
	    echo "Error: python3 is required but not found in PATH."; exit 1; }
	@python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)" 2>/dev/null || { \
	    echo "Error: Python 3.6 or later is required."; \
	    echo "Found: $$(python3 --version)"; exit 1; }
	@command -v fzf >/dev/null 2>&1 || \
	    echo "Warning: fzf not found -- install it before running remotely."
	@command -v fd >/dev/null 2>&1 || \
	    echo "Warning: fd not found -- install it before running remotely."
	@echo "Prerequisites OK"
