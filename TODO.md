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

## Channels (Major Mode Redesign)

Inspired by Television's channel concept but adapted to fzfr's architecture:
fzfr stays a wiring layer, channels are named search presets, and the SSH
remote layer applies transparently to any channel.

---

### Config Layout

fzfr configuration is split across two locations:

```
~/.config/fzfr/
  config          # core settings (stable, rarely changed)
  conf.d/
    files.json    # built-in channel definitions (auto-generated on first run)
    git.json
    dirs.json
    content.json
    *.json        # user-defined channels
```

**`~/.config/fzfr/config`** -- core settings only. No channels here.

```json
{
  "ssh_multiplexing": false,
  "ssh_control_persist": 60,
  "ssh_strict_host_key_checking": true,
  "editor": "",
  "search_history": false,
  "path_format": "relative",
  "max_stream_mb": 100,
  "default_channel": "files",
  "switch_channel_key": "ctrl-m",
  "keybindings": {
    "toggle_hidden":  "ctrl-h",
    "filter_ext":     "ctrl-f",
    "add_exclude":    "ctrl-x",
    "refresh_list":   "ctrl-r",
    "sort_list":      "ctrl-s",
    "copy_path":      "ctrl-c",
    "open_file":      "enter",
    "exit":           "esc"
  }
}
```

Note: `toggle_mode` (CTRL-T) and `toggle_ftype` (CTRL-D) are removed from
the top-level keybindings -- these concepts move into channel definitions
and source cycling respectively.

**`~/.config/fzfr/conf.d/*.json`** -- one file per channel.

Each file defines exactly one channel. The filename is the channel name
(without `.json`). Files are loaded in lexicographic order; later files
override keys of earlier ones with the same channel name.

---

### Channel Schema

```json
{
  "description": "All files, fuzzy filename search",
  "key": "f",
  "sources": [
    {
      "label": "tracked",
      "command": "fd",
      "args": { "type": "f", "hidden": false }
    },
    {
      "label": "hidden",
      "command": "fd",
      "args": { "type": "f", "hidden": true }
    }
  ],
  "mode": "name",
  "exclude_patterns": [],
  "include_extensions": [],
  "preview": "auto",
  "cycle_sources_key": "ctrl-h",
  "actions": {
    "leader": "ctrl-b",
    "groups": {}
  }
}
```

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Shown in the channel picker overlay |
| `key` | char | Single key for the channel picker (e.g. `"f"`) |
| `sources` | array | One or more source definitions (see below) |
| `mode` | `"name"` / `"content"` | fzf search mode |
| `exclude_patterns` | array | Glob patterns to exclude |
| `include_extensions` | array | Extension filter (empty = all) |
| `preview` | `"auto"` / `"none"` / shell string | Preview command override |
| `cycle_sources_key` | string | Key to cycle between sources (optional) |
| `actions` | object | Channel-scoped custom actions (same schema as global) |

**Source definition:**

```json
{
  "label": "tracked",
  "command": "fd",
  "args": { "type": "f", "hidden": false, "file_source": "auto" }
}
```

`command` is either `"fd"`, `"git"`, or a raw shell string. When a raw
shell string is given, fzfr pipes its stdout directly to fzf -- no fd/git
logic applied, `args` is ignored.

```json
{
  "label": "docker containers",
  "command": "docker ps --format '{{.Names}}'"
}
```

---

### Built-in Channel Files

fzfr ships these channel definitions. They are written to `conf.d/` on first
run if the directory is empty, so users can override individual files:

**`conf.d/files.json`**
```json
{
  "description": "All files, fuzzy filename search",
  "key": "f",
  "sources": [
    { "label": "normal",  "command": "fd", "args": { "type": "f", "hidden": false } },
    { "label": "hidden",  "command": "fd", "args": { "type": "f", "hidden": true  } }
  ],
  "mode": "name",
  "cycle_sources_key": "ctrl-h"
}
```

**`conf.d/content.json`**
```json
{
  "description": "Full-text search across file contents",
  "key": "c",
  "sources": [
    { "label": "normal",  "command": "fd", "args": { "type": "f", "hidden": false } },
    { "label": "hidden",  "command": "fd", "args": { "type": "f", "hidden": true  } }
  ],
  "mode": "content",
  "cycle_sources_key": "ctrl-h"
}
```

**`conf.d/git.json`**
```json
{
  "description": "Git-tracked files only",
  "key": "g",
  "sources": [
    { "label": "tracked",          "command": "git", "args": { "hidden": false } },
    { "label": "tracked + others", "command": "git", "args": { "hidden": true  } }
  ],
  "mode": "name",
  "cycle_sources_key": "ctrl-h"
}
```

**`conf.d/dirs.json`**
```json
{
  "description": "Directories only",
  "key": "d",
  "sources": [
    { "label": "normal", "command": "fd", "args": { "type": "d", "hidden": false } },
    { "label": "hidden", "command": "fd", "args": { "type": "d", "hidden": true  } }
  ],
  "mode": "name",
  "cycle_sources_key": "ctrl-h"
}
```

---

### User-Defined Channel Example

**`~/.config/fzfr/conf.d/logs.json`**
```json
{
  "description": "Application log files",
  "key": "l",
  "sources": [
    { "label": "all logs", "command": "fd", "args": {
        "type": "f", "hidden": false,
        "extensions": ["log", "txt"]
    }}
  ],
  "mode": "content",
  "actions": {
    "leader": "ctrl-b",
    "groups": {
      "g": {
        "label": "grep",
        "actions": {
          "e": { "cmd": "grep -n '{q}' {path}", "label": "grep query", "output": "overlay" }
        }
      }
    }
  }
}
```

---

### CLI

```sh
fzfr                          # default_channel from config
fzfr git                      # built-in git channel
fzfr logs                     # user-defined channel
fzfr user@host /path git      # remote + channel
fzfr --channel content        # explicit flag
```

Argv layout:
```
fzfr [TARGET] [BASE_PATH] [CHANNEL] [--exclude PATTERN ...]
```

Backward compatible: `name` and `content` as CHANNEL map to the `files`
and `content` built-in channels. `fzfr local ~/projects name` still works.

---

### Runtime Channel Switching

`CTRL-M` (configurable via `switch_channel_key`) opens a channel picker
using the existing SIGSTOP/overlay system:

```
CTRL-M ->
  ╭─ channels ──────────────────╮
  │  [f] files    - all files   │
  │  [c] content  - full-text   │
  │  [g] git      - tracked     │
  │  [d] dirs     - directories │
  │  [l] logs     - app logs    │
  │  [q] cancel                 │
  ╰─────────────────────────────╯
```

Reuses `_run_which_key_menu()` -- flat single-level menu, key from each
channel's `key` field. After selection fzfr reloads the source and updates
fzf prompt/header via `transform` actions. No process restart needed for
channels with the same fzf invocation structure; `become` (fzf >= 0.45)
used when the fzf args need to change fundamentally.

---

### Source Cycling

Within a channel, `cycle_sources_key` cycles through the `sources` array.
Source index stored in state. On cycle:
- update `source_index` in state
- fzf fires a `reload` with the new source command
- prompt and header update via `transform-prompt` / `transform-header`
- label shown in prompt: `[files: hidden]`

This replaces the current CTRL-H toggle and CTRL-T mode toggle with a
unified, channel-aware mechanism.

---

### State Changes

```json
{
  "channel": "files",
  "source_index": 0,
  "mode": "name"
}
```

`toggle_mode`, `toggle_ftype`, `show_hidden` state keys are deprecated in
favour of `channel` + `source_index`. Kept for one release as aliases.

---

### Config Loading

```
load_config()
  1. read ~/.config/fzfr/config  ->  core settings
  2. glob ~/.config/fzfr/conf.d/*.json in lexicographic order
  3. for each file: parse as channel definition, register by filename stem
  4. merge: user conf.d channels override built-in channels of same name
  5. validate all channels: check keys unique, sources non-empty, etc.
  6. if conf.d/ empty or missing: seed with built-in channel defaults
```

The core config and channel files are loaded separately. A syntax error in
one channel file skips that channel with a warning -- it does not prevent
fzfr from launching.

On remote SSH sessions, `SCRIPT_BYTES` already contains the full fzfr
script. Channel files from the local `conf.d/` are serialized into the
session state at launch and passed to remote callbacks via state -- no
remote filesystem access needed.

---

### Backward Compatibility

- `fzfr local /path name` -- `name` maps to `files` channel, mode=name
- `fzfr local /path content` -- `content` maps to `content` channel
- Top-level `default_mode`, `file_source`, `show_hidden` in config -- if
  present and no `default_channel` set, synthesize an implicit `files`
  channel with those values applied. Emit a deprecation warning.
- Global `custom_actions` at top level -- still valid, treated as the
  global action set applied to all channels that don't define their own.

---

### Complexity: High

Do not start until Git Integration Phase 2 is merged.

**Phase 1** -- conf.d loader, built-in channels, CLI channel arg, no UI.
  Files: `config.py`, `search.py`, `backends.py`

**Phase 2** -- CTRL-M channel switcher overlay, source cycling.
  Files: `internal.py`, `search.py`, `state.py`

**Phase 3** -- channel-scoped actions, user-defined channels in conf.d,
  remote channel serialization into state.
  Files: `config.py`, `internal.py`, `remote.py`, `search.py`

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
