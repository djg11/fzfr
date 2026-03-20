# fzfr — TODO / Roadmap

---

## Design Principles

### Architecture

fzfr is a **wiring layer**, not a feature accumulator. Its job is exactly three things:

1. **Wire core tools together** — build the fzf invocation, manage the subprocess
   chain, own the state file, handle the fallback hierarchy between tools
2. **Add the SSH layer** — bootstrap the remote agent, cache the script, make
   remote search feel identical to local search
3. **Provide the custom action escape hatch** — so anything outside the core stack
   can be composed by the user without touching fzfr internals

If a feature can be expressed as `"cmd": "tool {path}"` in the custom action
config, it does not belong in fzfr core.

If a feature requires access to fzfr's internal state — the session, the backend,
the SSH tunnel, the state file — it belongs in core.

### Graceful degradation

fzfr degrades gracefully. Nothing beyond Tier 1 is hard-required. The experience
improves as more tools are available; it never hard-fails because an optional tool
is missing. Every Tier 3, 4, and 5 tool has a coded fallback path.

### Core tool stack

These are every external tool fzfr shells out to, organised by how essential they
are to the core experience.

**Tier 1 — Required** (fzfr will not function without these):

| Tool | Role |
|------|------|
| `fzf` | The UI engine — the entire interface is fzf |
| `python3` | Runtime for the fzfr agent (local and remote) |
| `ssh` | Remote transport layer |
| `fd` / `find` | File listing — `fd` preferred, `find` fallback |
| `grep` | Content search fallback when `rga` is absent |

**Tier 2 — Core experience** (expected to be present on any developer machine):

| Tool | Role |
|------|------|
| `rga` | Content search with filetype awareness and match highlighting |
| `bat` | Syntax-highlighted file preview |
| `git` | `git ls-files` file source; log and diff in preview pane |
| `tmux` | Window/pane management; TTY handoff for interactive actions |
| `file` | MIME detection for ambiguous file types |
| `xargs` | Argument passing in reload pipelines |
| `tar` | Archive listing and extraction (primary archive tool) |
| `cat` | Plain file preview fallback when `bat` is absent |

**Tier 3 — Preview enhancers** (each has a fallback; absent = degraded preview):

| Tool | Role | Fallback |
|------|------|----------|
| `pdftotext` | Extract text from PDF files | `rga` with OCR plugins |
| `xxd` | Hex dump for binary files | `hexdump` → `od` |
| `hexdump` | Hex dump fallback | `od` |
| `od` | Hex dump last resort | error message |
| `eza` | Directory listing with icons and tree view | `exa` → `tree` → `ls` |
| `exa` | Directory listing fallback | `tree` → `ls` |
| `tree` | Directory tree fallback | `ls` |

**Tier 4 — Archive handlers** (each covers different formats; absence = that format unsupported):

| Tool | Formats |
|------|---------|
| `7z` | `.7z`, `.zip`, `.rar`, `.iso` and many others |
| `unrar` | `.rar` (fallback to `7z`) |
| `unzip` | `.zip` (fallback to `7z`) |
| `tar` | `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz` |
| `gunzip` / `zcat` | `.gz` |
| `bzcat` | `.bz2` |
| `xzcat` | `.xz` |
| `lz4` | `.lz4` |
| `zstd` | `.zst` |
| `cpio` | `.cpio` |

**Tier 5 — Platform-specific** (OS-dependent; fzfr detects which is available):

| Tool | Platform | Role |
|------|----------|------|
| `xclip` | Linux (X11) | Clipboard write |
| `wl-copy` | Linux (Wayland) | Clipboard write |
| `pbcopy` | macOS | Clipboard write |
| `xdg-open` | Linux | Open file/directory in default application |
| `open` | macOS | Open file/directory in default application (built-in) |

### The custom action boundary

A tool belongs in the **custom action config** (not in fzfr core) if:

- fzfr has no fallback for it and no business knowing it exists
- It operates on the *result* of a search rather than powering the search itself
- Its absence does not degrade the core find/preview/open workflow

Examples: `pylint`, `black`, `delta`, `scp` to a custom destination, `du`, any
linter, formatter, or project-specific tool.

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

**Do not attempt until Custom Action System Phase 1, Docker backend, and Git
Integration Phase 2 are implemented and stable.**

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

**Depends on:** Custom Action System Phase 2 — `RemoteBackend.run_command()` built there is a required primitive for routing aggregated-host actions back to the correct host.

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

**Depends on:** Custom Action System Phase 2 — `RemoteBackend.run_command()` built there is reused directly by `DockerBackend.run_command()`.

**Interim (before this ships):** Single-container one-off commands can be approximated via custom actions:
```json
{ "cmd": "docker exec my-container cat {path}", "output": "tmux" }
```
This does not give you file listing or preview inside the container — it only runs a command against a known container name with a host-side path.

---

## Git Integration

Make fzfr Git-aware for more relevant results and richer contextual previews.

### ~~Phase 1 — `git ls-files` file source + preview context~~ ✓ Done

`git ls-files` as the file source (respects `.gitignore` exactly), `file_source`
config key (`"auto"` / `"fd"` / `"git"`), and `git log` + `git diff HEAD` context
appended in the preview pane. Merged to main.

### Phase 2 — `fzfr git-log` mode

New fzfr mode where the file list is populated by commit hashes rather than
file paths. Requires internal state changes — the selected item is a commit,
not a file, so preview, open, and copy all need mode-aware dispatch.

- `fzfr git-log` — search commit messages via `git log --oneline`
- Preview pane shows full `git show <hash>` diff for the highlighted commit
- Open action: `git show <hash>` in tmux or `$EDITOR`
- Cannot be a custom action — the file *source* is commits, not files

**Complexity:** Medium

### Phase 3 — git-status staging view

Interactive staging: file list sourced from `git status --short`, preview shows
`git diff` for the highlighted file, actions for stage/unstage/discard.

- Cannot be a custom action — requires a different file source and mode-aware
  keybindings for stage/unstage

**Complexity:** Medium. Do not start until Phase 2 is merged.

### Removed: open on GitHub/GitLab

Originally planned as Phase 3. Removed from core — too forge-specific and
platform-dependent. Implement as a custom action instead:

```json
{
  "label": "open on GitHub",
  "cmd": "xdg-open \"$(git remote get-url origin | sed 's/git@github.com:/https:\/\/github.com\//;s/\.git$//')/blob/HEAD/{path}\"",
  "output": "silent"
}
```

---

---

## Custom Action System

Allow users to define arbitrary shell commands in `~/.config/fzfr/config` triggered
via a two-level which-key leader menu. Turns fzfr into an extensible workflow engine
without growing the core feature set.

### Design

**Leader key + group key + action key.** Pressing the leader shows all configured
groups in the fzf header. Pressing a group key narrows to that group's actions.
Pressing an action key runs the command and restores the normal header. At every
level, `esc` cancels and restores the header. All three steps use fzf's native
`key1+key2+key3` chained bind syntax — no timeouts, no interception, no platform
differences.

**Config schema:**

```json
{
  "custom_actions": {
    "leader": "ctrl-space",
    "groups": {
      "g": {
        "label": "git",
        "actions": {
          "a": { "cmd": "git add {paths}",     "label": "add",     "output": "silent" },
          "r": { "cmd": "git restore {path}",  "label": "restore", "output": "silent" }
        }
      },
      "f": {
        "label": "file",
        "actions": {
          "c": { "cmd": "cat {path} | fzfr-copy", "label": "copy content", "output": "silent" },
          "d": { "cmd": "du -sh {path}",           "label": "disk usage",   "output": "preview"  }
        }
      }
    }
  }
}
```

**Placeholder contract** — fzfr guarantees every placeholder is `shlex.quote()`'d
before substitution. Users cannot break the tool with filenames containing spaces,
quotes, or semicolons.

| Placeholder | Value | fzf equivalent |
|-------------|-------|----------------|
| `{path}`    | Single highlighted file (absolute or relative per config) | `{}` |
| `{paths}`   | All TAB-selected files; falls back to `{path}` if none selected | `{+}` |
| `{dir}`     | Directory containing `{path}` | — |
| `{base}`    | Search root (BASE_PATH) | — |
| `{q}`       | Current fzf query string | `{q}` |

**`inputs` / `widgets` / `output` — what each key does:**

These three keys cover two separate phases of a custom action. Keeping them
distinct is important: `inputs` and `widgets` control what happens *before* the
command runs (input collection); `output` controls what happens *after* it runs
(where the command's stdout/stderr goes). They are orthogonal — every combination
is valid.

```
widgets/inputs  →  collect inputs  →  cmd runs  →  output handles result
```

**`output` modes** (→ README):

| Mode | Behaviour | Fallback (no tmux) |
|------|-----------|-------------------|
| `"tmux"` | Stream full output in a new tmux window | suspend fzf, run in current TTY |
| `"preview"` | Pipe stdout into the fzf preview pane as a one-shot replacement | same |
| `"silent"` | Suppress all output — fire and forget | same |

**Header UX (using existing fzf header — no new UI system):**

```
# idle — leader hint baked into normal header at session start
 CTRL-SPACE  actions

# after leader
 [g] git  [f] file  [esc] cancel

# after leader + g
 git ›  [a] add  [r] restore  [esc] cancel

# after action fires — header restored to normal via change-header
```

Header strings are baked at session start from the config. No subprocess is
spawned for header transitions — all state lives in fzf's own bind chain.
ESC at any level chains `change-header` back to the normal header string
(also baked at session start).

**Security:**

- `{path}` and all other placeholders map directly to fzf's own quoted
  placeholders (`{}`, `{+}`) — fzf shell-quotes them before substitution.
- The `cmd` string is user-controlled. Document clearly: fzfr does not sandbox
  the command itself, only the placeholder values. Same threat model as
  `~/.bashrc`.
- Validate at startup: reject any `leader` value that conflicts with fzfr's
  own reserved bindings. Emit a clear config error, do not silently override.

**Leader default:** `ctrl-space`. Note in README: macOS users and ChromeOS users
may need to remap to `alt-x` or `ctrl-g` if the OS intercepts `ctrl-space` for
input switching.

---

### Phase 1 — Local execution

**Files touched:** `config.py`, `internal.py`, `search.py`

**`config.py`:**
- Add `custom_actions` to `_DEFAULTS` with empty `groups` and `"leader": "ctrl-space"`
- Add nested validation in `_merge_config_key`: check group keys are single chars,
  action keys are single chars, `output` is one of the four valid modes,
  `cmd` contains only known placeholders
- Emit a clear `ConfigError` (not a crash) for any invalid entry so fzfr still
  launches with the bad action skipped

**`internal.py`:**
- Add `cmd_internal_exec(argv)` entry point
- `argv`: `[action_id, path, ...]` where `action_id` is `"group_key.action_key"`
- Load config, look up the action, substitute placeholders, run via
  `subprocess.run(shell=True)` on `LocalBackend`
- `output` mode determines whether output goes to the preview pane or stdout (preview/tmux) or
  is suppressed (silent)
- On non-zero exit: capture stderr, print to stdout prefixed with `[fzfr error]`
  so it surfaces in the header/preview rather than silently disappearing

**`search.py` — `build_fzf_invocation()`:**
- After the existing bind loop, iterate `custom_actions.groups`
- Generate three layers of `--bind` strings per action:
  1. `leader` → `change-header(group list)`
  2. `leader+group_key` → `change-header(action list for that group)`
  3. `leader+group_key+action_key` → `execute-silent(fzfr _internal-exec id {+})+change-header(normal header)`
- Generate ESC bindings at each level:
  1. After leader: `leader+esc` → `change-header(normal header)`
  2. After group: `leader+group_key+esc` → `change-header(group list)`
- Bake all header strings at session start — no runtime subprocess for header

**Complexity:** Low–Medium. ~150 lines across three files.

---

### Phase 2 — Remote bridge

**Files touched:** `backends.py`, `remote.py`, `internal.py`

`cmd_internal_exec` reconstructs the backend from the state file (same pattern
as all other internal commands). If the backend is `RemoteBackend`, the command
is routed through the existing SSH tunnel — no new SSH connection is opened.

```python
# internal.py — Phase 2 addition
backend = _backend_from_state(state)
if isinstance(backend, RemoteBackend):
    backend.run_command(cmd, display=action["output"])
else:
    subprocess.run(cmd, shell=True, ...)
```

`RemoteBackend.run_command()` wraps the substituted command as:
```sh
ssh <host> "cd <base> && <cmd>"
```
using the already-open multiplexed connection (`-o ControlMaster=auto`).
No second handshake. No bootstrap required — the command runs in a plain shell,
not via the fzfr agent.

This makes custom actions location-aware automatically. A user's `pylint {path}`
runs on the remote host when searching a remote path, locally when searching
locally. The user writes the command once.

**Complexity:** Medium. Requires `run_command()` on both backend classes and
careful testing on an actual SSH session.

---

---

### Phase 3 — Multi-step pipeline with sequential input

**Depends on:** Phase 1 (local execution) + Interactive File Ops nested picker
machinery (both share the same underlying session suspend/resume logic).

**The problem Phase 1 doesn't solve:**

Some actions need more than one input. The source file is already selected in
the active fzfr session, but the destination — a directory, a remote path, a
new filename — needs to be collected via a second interactive step before the
command can fire.

**Extended config schema:**

The `inputs` and `widgets` keys are parallel sequences. Each index defines a named
variable and the UI widget used to collect it. The collected values become
additional placeholders in `cmd`, quoted identically to `{path}` and `{paths}`.

```json
{
  "label": "Copy to local",
  "inputs": ["source",  "destination"],
  "widgets":       ["current", "directory_selector"],
  "cmd":      "cp {source} {destination}",
  "output":  "silent"
}
```

```json
{
  "label": "Download from remote",
  "inputs": ["source",  "destination"],
  "widgets":       ["current", "directory_selector"],
  "cmd":      "scp {source} {destination}",
  "output":  "silent"
}
```

```json
{
  "label": "Rename",
  "inputs": ["source",  "new_name"],
  "widgets":       ["current", "text_prompt"],
  "cmd":      "mv {source} {dir}/{new_name}",
  "output":  "silent"
}
```

```json
{
  "label": "Diff against",
  "inputs": ["source",  "target"],
  "widgets":       ["current", "file_selector"],
  "cmd":      "delta {source} {target}",
  "output":  "preview"
}
```

```json
{
  "label": "Upload to remote",
  "inputs": ["source",  "destination"],
  "widgets":       ["current", "remote_file_selector"],
  "cmd":      "scp {source} {destination}",
  "output":  "silent"
}
```

**`widgets` vocabulary** (→ README):

| Widget | Behaviour |
|--------|-----------|
| `"current"` | Use already-selected file(s) from the active fzfr session — no new picker |
| `"file_selector"` | Suspend current session, launch nested fzfr in file mode |
| `"directory_selector"` | Suspend current session, launch nested fzfr in directory mode |
| `"text_prompt"` | Suspend fzf, collect a single string via `_tty_prompt` |
| `"remote_file_selector"` | Suspend current session, launch nested fzfr against the active remote host |

**Cancellation contract:**

If the user hits `esc` at any step after the first, the entire pipeline is
aborted — no partial execution. The active fzfr session resumes unchanged.
Partial state (already-collected selector values) is discarded.

**Schema validation rules (enforced at startup):**

- `inputs` and `widgets` must be the same length
- Every name in `inputs` must appear as `{name}` in `cmd`
- `"current"` must always be at index 0 — you cannot defer the active selection
- `"remote_file_selector"` is only valid when the active backend is `RemoteBackend`;
  emit a clear config error at session start if used against a local path
- Single-step actions (`inputs` and `widgets` absent) remain valid — Phase 3 is
  additive, not a breaking change to Phase 1 configs

**Implementation notes:**

- The sequencing engine lives in `internal.py` alongside `cmd_internal_exec`
- Each non-`"current"` step suspends fzf via `execute(...)`, launches the
  nested picker, collects the result to a temp state key, then resumes
- `"directory_selector"` and `"file_selector"` reuse the nested fzfr instance
  pattern already built for Interactive File Ops `mv`/`cp` — do not duplicate
- `"text_prompt"` reuses `_tty_prompt` already used by `rm` confirmation
- `"remote_file_selector"` reuses `RemoteBackend` — same SSH tunnel, no new
  connection

**What this replaces from the original TODO:**

Once Phase 3 ships, the following are user-configurable rather than built-in:
- Interactive File Ops `mv` / `cp` (keep `rm` as hardcoded — safety-critical)
- SSH download / upload
- Side-by-side diff
- Surgical archive extraction
- Rename

**Complexity:** High. Do not start until Phase 1 and Phase 2 are merged and
the Interactive File Ops nested picker exists as a tested primitive.

---

## Interactive File Operations — `rm`

The only file operation that belongs in fzfr core is deletion. `mv`, `cp`, rename,
and transfer are handled by Custom Action System Phase 3 (`widgets`/`inputs`
multi-step pipeline) which provides the nested picker primitive they all share.

`rm` stays hardcoded because its safety guarantee — a mandatory, non-skippable
`[y/N]` confirmation — must be owned by fzfr, not left to user config. A user
misconfiguring a custom action that deletes files without confirmation is
unacceptable.

**Implementation notes:**
- `_tty_prompt` for mandatory `[y/N]` confirmation — non-skippable, no `--force` flag
- Works on the current selection (`{+}` for multi-select with per-file confirmation)
- Single keybinding, no mode switch required

**Note:** `mv` and `cp` are not implemented here. They are covered by Custom
Action System Phase 3 — the nested picker machinery built for Phase 3 is the
primitive they would have used anyway. Example config once Phase 3 ships:

```json
{ "label": "move",  "inputs": ["source", "destination"], "widgets": ["current", "directory_selector"], "cmd": "mv {source} {destination}",  "output": "silent" }
{ "label": "copy",  "inputs": ["source", "destination"], "widgets": ["current", "directory_selector"], "cmd": "cp {source} {destination}",   "output": "silent" }
{ "label": "rename","inputs": ["source", "new_name"],    "widgets": ["current", "text_prompt"],        "cmd": "mv {source} {dir}/{new_name}", "output": "silent" }
```

**Complexity:** Low (leverages existing `_tty_prompt`)

---

## ~~`open.py` — Platform Fixes~~ ✓ Done

### ~~Replace hardcoded `xdg-open` with platform-aware helper~~ ✓ Done

`xdg-open` is hardcoded in three places in `_open()`:
- Binary local files
- Binary remote files (after streaming to session dir)
- Directories when tmux is absent (fall-through)

None of these work on macOS, where the equivalent is `open` (built-in).

**Fix:** add a `_xdg_open(path)` helper in `open.py`:

```python
def _xdg_open(path: str) -> None:
    """Open path with the platform file opener.
    Uses xdg-open on Linux, open on macOS. Falls back to xdg-open
    if neither platform is detected — better an informative error
    than silent failure.
    """
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [opener, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
```

Replace all three `subprocess.Popen(["xdg-open", ...])` calls with
`_xdg_open(...)`. No other changes needed.

**Files touched:** `open.py` only.

### ~~Remove `nano` from `_find_editor()` fallback chain~~ ✓ Done

`nano` is currently between `vim` and `vi` in the compiled-in fallback chain.
It does not belong there — it is not universally present and adds nothing to
the reliability guarantee that `vi` already provides.

**Fix:** change the fallback tuple from `("nvim", "vim", "nano", "vi")` to
`("nvim", "vim", "vi")`.

**Files touched:** `open.py` only.

**Complexity:** Trivial. Both fixes are one-liner changes.

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

---

## UI / Header Template System

Allow users to customise the fzf header string per mode using a template
language with fzfr-provided variables.

**Depends on:** Custom Action System Phase 1 (the header baking infrastructure
built there is the foundation this feature extends).

**Design notes:**
- Template variables: `{mode}`, `{base}`, `{host}`, `{branch}`, `{file_count}`,
  `{query}`, `{action_hint}` (shows leader key hint when custom actions configured)
- Per-mode overrides: `header.default`, `header.git`, `header.remote`,
  `header.leader_active`, `header.leader_group`
- ESC fallback behaviour configurable per mode
- Header is re-baked on mode switch (already happens via `transform-header`)
- Keep template evaluation in Python at session start and on mode switch —
  no new subprocess

**Complexity:** Medium. Do not implement until Custom Action System Phase 1
is merged and stable — the two features share header baking infrastructure
and should not be developed in parallel.

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
