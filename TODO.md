# fzfr — TODO / Roadmap

---

## ~~Refactor: Split Source Into Modules + Build Script~~ ✓ Done

The source has been split into `src/fzfr/` modules with `scripts/build_single_file.py`
producing the distributable `fzfr` file. See the repo structure for details.

---

## Ship src/fzfr/ Package on PyPI (Low Priority)

Currently PyPI and GitHub both ship the built monolithic `fzfr` script.
This works correctly for all users including SSH remote preview (SCRIPT_BYTES
contains the full script). The `src/fzfr/` package is development-only.

When the feature set is stable, consider shipping the package instead:
- Pros: clean Python package structure, individual modules importable
- Cons: SSH remote preview needs rethinking — SCRIPT_BYTES must still contain
  the full built script, not just _script.py. Likely solution: ship both the
  package and the built script, with _find_self() finding the latter.

**Do not attempt until Docker backend, Git integration, and Interactive File
Operations are implemented and stable.**

---

## Multi-Host Search

Search across multiple remote machines simultaneously, aggregating results into a single fzf session.

**Command-line syntax:**
```sh
fzfr remote1:/path1 remote2:/path2
fzfr web01:/var/log web02:/var/log web03:/var/log
```

**Implementation notes:**
- Each `host:path` pair maps to a `RemoteBackend` instance
- Run N searches in parallel using `threading` (not asyncio — subprocess is the bottleneck)
- Prefix each result with its source host: `web01:/var/log/app.log`
- Strip the host prefix when passing the selected path to preview/open
- Reload path: every keystroke re-queries all hosts in parallel and merges stdout into fzf stdin
- Config: allow defining named host groups for frequently used combinations

**Complexity:** High

---

## Docker Backend

Search and preview files inside running Docker containers without manual `docker exec` commands.

**Implementation notes:**
- New `DockerBackend` class modelled on `RemoteBackend`
- Replace `ssh <host>` with `docker exec <container>` throughout
- Use the same script-over-stdin technique for remote preview
- Container discovery via `docker ps` for tab-completion or a container picker
- No multiplexing equivalent for docker exec (each call is independent)
- Requirement: container must have `python3.10+` and `fd` in its PATH, same as SSH

**Complexity:** Medium (≈80% code reuse from `RemoteBackend`)

---

## Git Integration

Make fzfr Git-aware for more relevant results and richer contextual previews.

**Implementation notes (incremental):**

1. **`git ls-files` mode** — use `git ls-files` as the file source instead of `fd`; faster and respects `.gitignore` exactly. Lowest effort, highest immediate value.
2. **Enhanced preview** — show `git log --oneline -5` and `git status` for the selected file alongside the content preview.
3. **Commit history search** — new mode `fzfr git-log` to search commit messages; preview shows the full diff for the highlighted commit.
4. **Open on remote** — keybinding to open the selected file on GitHub/GitLab in the browser.

**Complexity:** Medium (implement incrementally, start with `git ls-files`)

---

## Interactive File Operations

Manage files directly from the fzf interface after finding them.

**Implementation notes:**
- `rm` — `_tty_prompt` for mandatory `[y/N]` confirmation before deletion; confirmation must be non-skippable
- `mv` / `cp` — launch a nested fzfr instance in directory mode to fuzzy-find the destination folder
- All operations work on the current selection (`{+}` for multi-select)
- Start with `rm` only; add `mv`/`cp` once `rm` is proven solid

**Complexity:** Low (leverages existing fzf patterns and `_tty_prompt`)

---

## Core Features / UI

### Major Mode Switcher
*   **Goal:** Provide an intuitive way to switch between different major operational modes (e.g., `files`, `git-log`, `docker-ps`).
*   **User Benefit:** Improves navigability and makes `fzfr` more versatile without restarting the terminal.
*   **Implementation Notes:**
    *   Implement as a global keybinding (e.g., `ctrl-m`).
    *   The keybinding will launch a *new, temporary `fzf` instance* displaying a list of available major modes.
    *   Selecting a mode from this list will cause the current `fzfr` session to exit and immediately re-launch itself in the chosen mode (e.g., `fzfr git-log`).
    *   This requires changes to the configuration structure to support mode-specific keybindings, and a way to load the appropriate keybindings when `fzfr` starts in a given mode.
*   **Complexity:** Medium (Requires careful coordination of config, state, and relaunch logic.)
