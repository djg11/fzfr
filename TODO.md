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

Make fzfr Git-aware for more relevant results, richer contextual previews, and
git-specific browsing modes. Inspired by
[forgit](https://github.com/wfxr/forgit) and
[fzf-git.sh](https://github.com/junegunn/fzf-git.sh), but scoped to fzfr's
identity as a file search and preview tool. All modes must work over SSH (same
script-over-stdin architecture as file search).

Implemented in three phases. Phase 1 is safe to merge immediately — no new
modes, no actions, no keybinding changes. Phases 2 and 3 each build on the
previous.

---

### Phase 1 — File source and preview enrichment

Zero-risk changes. No new modes, no actions, no new keybindings. Falls back
gracefully when `git` is unavailable or the directory is not a repo.

#### `git ls-files` as file source

Use `git ls-files` as the file listing backend when inside a git repository
instead of `fd`. Respects `.gitignore` exactly and is faster than `fd` for
large repos with many ignored files.

**Implementation notes:**
- Detect repo root via `_find_git_root()` (already exists in `search.py`).
- Replace `fd` in `LocalBackend.initial_list_cmd()` and `LocalBackend.reload()`
  when a git root is found and `file_source` is `"auto"` or `"git"`.
- Remote hosts: run `git ls-files` on the remote via the existing SSH reload
  mechanism, same as `fd`.
- Add a config key `"file_source": "auto" | "fd" | "git"`. `"auto"` (default)
  uses `git ls-files` inside a repo, `fd` everywhere else.
- Content search still uses `rga`/`grep` — `git ls-files` only affects the
  file listing, not the search itself.
- Hidden-file toggle (`CTRL-H`): in git mode, adds untracked files via
  `git ls-files --others --exclude-standard`.
- Add `git` to `_ALL_TOOLS` in `config.py` and to the optional tools table
  in `README.md`.

**Complexity:** Low

#### Enhanced file preview (git context)

When previewing a file inside a git repo, append a git context block below the
file content in the preview pane: recent commits touching the file and the
current uncommitted diff.

**Implementation notes:**
- In `_preview_text()`: after the bat/cat output, append:
  - A separator line
  - `git log --oneline --color -5 -- <file>` — last 5 commits for this file
  - `git diff HEAD --color=always -- <file>` — current uncommitted changes
- Only appended when `git` is in `AVAILABLE_TOOLS` and the file is tracked.
- Falls back silently (no output, no error) outside a repo.

**Complexity:** Low

---

### Phase 2 — Read-only git modes

New invocation modes. Still no destructive actions. Enter opens content in
`$EDITOR` via a new tmux window, leaving fzfr running — same pattern as file
open today.

#### `fzfr git-log` — commit browser

```sh
fzfr git-log [base_path]
fzfr user@server git-log /var/log   # works over SSH
```

Lists commits via `git log --oneline --graph --color`. Preview pane shows the
full diff via `git show <hash> --stat --patch --color`.

**Implementation notes:**
- New target type in `cmd_search()` alongside `local` and `<ssh-host>`.
- Hash extracted from the selected line (first hex token).
- `CTRL-T` toggles `--all` (all branches) vs current branch only.
- Enter: opens diff in `$EDITOR` in a new tmux window
  (`git show <hash> | $EDITOR -`), or prints the hash if tmux is absent.
- `CTRL-Y`: copy commit hash to clipboard (reuses existing `fzfr-copy`
  infrastructure).
- Inspired by forgit's `glo` and fzf-git.sh's `CTRL-G CTRL-H`.

**Complexity:** Medium

#### `fzfr git-refs` — branch and tag picker

Lists local branches, remote branches, and tags via `git for-each-ref
--sort=-committerdate --color` (requires git ≥ 2.42). Preview shows recent
commits on the highlighted ref via `git log --oneline --color -10 <ref>`.

**Implementation notes:**
- `CTRL-T` cycles: local branches → all branches + remotes → tags only.
- Enter: `git switch <branch>` / `git checkout <tag>` — single safe action,
  no conflict risk.
- Requires git ≥ 2.42 for `--color` in `for-each-ref`; degrade gracefully on
  older versions.
- Most useful as an entry point from the Major Mode Switcher (Phase 3).

**Complexity:** Low–Medium

---

### Phase 3 — Git actions and mode-aware keybindings

Depends on Phase 2 being merged. Also depends on the **Mode-Aware Keybinding
System** (see Core Features / UI) being designed first — adding actions across
multiple git modes without that infrastructure leads to keybinding conflicts and
an unmaintainable `build_fzf_invocation()`.

**Do not start Phase 3 until:**
1. Phase 2 is merged and stable.
2. The mode-aware keybinding system is designed (can be specced on a separate
   branch before implementation).

#### `fzfr git-status` — changed files with staging

Lists files from `git status --short` with status prefix. Preview shows the
diff for the highlighted file (`git diff --color` for unstaged,
`git diff --cached --color` for staged).

**Actions (require mode-aware keybindings):**
- `CTRL-A`: stage selected file(s) (`git add`).
- `CTRL-U`: unstage (`git restore --staged`).
- `CTRL-D`: discard unstaged changes (`git restore`) — requires `[y/N]`
  confirmation via `_tty_prompt`, same guard as Interactive File Ops `rm`.
- After any action, reload the list automatically.
- Inspired by forgit's `ga` (staging) + `gd` (diff viewer) combined.

**Complexity:** Medium

#### Stash viewer

Lists stashes via `git stash list`. Preview shows `git stash show -p <ref>
--color`.

**Actions:**
- `CTRL-A`: apply stash (`git stash apply`).
- `CTRL-P`: pop stash (`git stash pop`).
- `CTRL-D`: drop stash (`git stash drop`) — `[y/N]` confirmation.

**Complexity:** Low

#### Multi-step git actions via tmux (git-log and git-refs)

Actions that require interactive editors or conflict resolution are handed off
to a new tmux window, leaving fzfr running. Without tmux, prints the equivalent
git command with a hint to run it manually.

**Actions on `fzfr git-log`:**
- `CTRL-R`: interactive rebase from selected commit
  (`git rebase -i <hash>~1` in new tmux window).
- `CTRL-P`: cherry-pick selected commit
  (`git cherry-pick <hash>` in new tmux window).
- `CTRL-F`: fixup — `git commit --fixup <hash> && git rebase -i
  --autosquash <hash>~1` in new tmux window.
- `CTRL-V`: revert — `git revert <hash>` (non-destructive, no tmux needed).

**Actions on `fzfr git-refs`:**
- `CTRL-D`: delete branch (`git branch -D <branch>`) — `[y/N]` confirmation.

**Complexity:** Medium–High

#### Open on GitHub / GitLab / Gitea

A keybinding (default `CTRL-O`) that opens the selected file or commit on the
remote git host in the browser. Available in all git modes.

**Implementation notes:**
- Construct URL from `git remote get-url origin` + branch/hash + file path.
- Support GitHub, GitLab, Gitea URL schemes. Config override for self-hosted.
- Uses `xdg-open` / `open`.
- Works in remote SSH mode: git config queried on the remote host.
- Inspired by fzf-git.sh's `CTRL-O`.

**Complexity:** Low


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

---

## ~~Remote Agent: Transience Improvements — Phase 1~~ ✓ Done

### ~~Process rename~~ ✓ Done

The remote agent now renames itself via `prctl(PR_SET_NAME)` so it appears
as `python3 fzfr` in `ps`/`top` rather than `python3 -`.

### ~~/dev/shm bootstrap cache~~ ✓ Done

The remote script cache now prefers `/dev/shm/fzfr/` (tmpfs, RAM-backed,
nothing persists on disk after reboot) and falls back to `~/.cache/fzfr/`
on systems where `/dev/shm` is absent or not writable (macOS, some containers).
Exactly one location is written — never both. The bootstrap checks `/dev/shm`
first, then `~/.cache`, matching the upload priority.
---

## Remote Agent: Transience Improvements — Phase 2

The remaining transience work. Requires Phase 1 to be merged first.

### In-Memory Execution via `memfd_create`

Replace the `/dev/shm` file with a truly anonymous RAM fd that leaves no
trace on disk even within a session:

```python
import ctypes, os, sys
fd = ctypes.CDLL(None).memfd_create(b"fzfr", 0)
os.write(fd, script_bytes)
os.execve(f"/proc/self/fd/{fd}", [sys.executable] + sys.argv, os.environ)
```

Priority order on the remote:
1. `memfd_create` — Linux only, truly anonymous
2. `/dev/shm` via `mkstemp` + immediate unlink — Linux/BSD, RAM-backed
3. `tempfile.gettempdir()` via `mkstemp` + immediate unlink — universal fallback

**Performance note:** once `memfd_create` is implemented without a persistent
file cache, every preview call that misses the local output cache pays the
full ~60KB transfer cost. Minification (see below) is therefore required
alongside this change.

### Minification

Strip comments and collapse whitespace in `scripts/build_single_file.py`.
Target: 20–30 KB from the current ~60 KB. Required alongside `memfd_create`
because without a persistent remote cache, script size directly determines
preview latency on every local output cache miss.

### Session-scoped in-memory script cache

Once `memfd_create` is in place, add a session-scoped in-memory dict so
repeated previews within one fzfr session avoid re-transferring the script.
The local output cache (`_PreviewCache`) already avoids re-running previews
for files seen this session; this is the complementary cache for the script
transfer itself.

**Complexity:** Medium. Do not attempt until `memfd_create` and minification
are complete and stable.
