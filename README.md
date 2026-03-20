# fzfr

Fuzzy file search for local and remote filesystems.

![fzfr demo](https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000)

---

## Features

- **Content search** — full-text search across files using `rga` (PDFs, archives, source) or `grep` fallback
- **Filename search** — fuzzy-filter filenames with fzf's native matching
- **Directory search** — browse and navigate directory trees
- **SSH remote search** — search and preview files on remote hosts with zero remote installation
- **Rich preview pane** — syntax-highlighted text, PDF text extraction, archive listings, hex for binaries
- **tmux integration** — opens files in a new tmux window, leaving fzfr running
- **Configurable keybindings** — every key is remappable via `~/.config/fzfr/config`
- **Path format** — display absolute or relative paths in the file list
- **Extension filter** — narrow results to specific file types at runtime

---

## Requirements

**Required:**

| Tool | Version | Purpose |
|------|---------|---------|
| Python | ≥ 3.10 | Runtime — macOS ships Python 3.9; install via `brew install python` or `pyenv` |
| [fzf](https://github.com/junegunn/fzf) | ≥ 0.38 | Fuzzy finder UI |
| [fd](https://github.com/sharkdp/fd) | any | Fast file listing — on Debian/Ubuntu install as `fd-find`; the binary is called `fdfind`, symlink it: `ln -s $(which fdfind) ~/.local/bin/fd` |

**Optional — each adds a capability:**

| Tool | Platform | Capability |
|------|----------|-----------|
| [bat](https://github.com/sharkdp/bat) | all | Syntax-highlighted preview; falls back to `cat` |
| [rga](https://github.com/phiresky/ripgrep-all) | all | Content search inside PDFs, archives, and more; falls back to `grep` |
| [git](https://git-scm.com/) | all | `git ls-files` as file source (respects `.gitignore`); git log + diff in preview |
| [pdftotext](https://poppler.freedesktop.org/) | all | PDF text extraction — install `poppler-utils` (Linux) or `brew install poppler` (macOS) |
| [tmux](https://github.com/tmux/tmux) | all | Open files in a new tmux window; falls back to `$EDITOR` in current TTY when tmux is absent |
| [eza](https://github.com/eza-community/eza) | all | Rich directory preview with icons and tree view; falls back to `exa` → `tree` → `ls` |
| `xclip` | Linux (X11) | Copy file path to clipboard |
| `wl-copy` | Linux (Wayland) | Copy file path to clipboard |
| `pbcopy` | macOS | Copy file path to clipboard |
| `xdg-open` | Linux | Open file/directory in default application |
| `open` | macOS | Open file/directory in default application (built-in, no install needed) |
| `ssh` | all | Remote search and preview |

---

## Installation

**From source (recommended):**

```sh
git clone https://github.com/djg11/fzfr
cd fzfr
make install
```

This copies `fzfr` to `~/.local/bin` and creates symlinks for all sub-commands.
Make sure `~/.local/bin` is in your `PATH`:

```sh
# bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

# zsh (macOS default since Catalina)
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc

# fish
fish_add_path ~/.local/bin
```

> **macOS note:** `~/.local/bin` may not exist by default — `make install` creates it, but if you install manually: `mkdir -p ~/.local/bin`

**Via pipx or pip:**

```sh
pipx install fzfr
```
```sh
pip install fzfr
```

**Uninstall:**

```sh
make uninstall        # if installed via make
pipx uninstall fzfr   # if installed via pipx
pip uninstall fzfr    # if installed via pip
```

---

## Usage

```sh
fzfr                                    # search current git root (or cwd)
fzfr local ~/projects name              # filename search in ~/projects
fzfr user@server ~/documents            # remote content search
fzfr myserver /var/log content          # remote content search, explicit mode
fzfr local . content --exclude '*.pyc'  # exclude patterns
```

**Arguments:**

```
fzfr [TARGET] [BASE_PATH] [MODE] [--exclude PATTERN ...]

  TARGET      local (default) | <ssh-host>
  BASE_PATH   directory to search (default: nearest git root or cwd)
  MODE        content (default) | name
  --exclude   glob pattern to exclude (repeatable)
```

---

## Keybindings

| Key | Action |
|-----|--------|
| `CTRL-T` | Toggle content ↔ filename search |
| `CTRL-D` | Toggle file ↔ directory search |
| `CTRL-H` | Toggle hidden files |
| `CTRL-F` | Filter by file extension |
| `CTRL-X` | Add exclude pattern (empty input clears all runtime excludes) |
| `CTRL-R` | Refresh file list |
| `CTRL-S` | Sort file list |
| `CTRL-C` | Copy selected path to clipboard |
| `CTRL-P` / `CTRL-N` | Navigate search history (when `search_history` is enabled) |
| `ALT-J` / `ALT-K` | Scroll preview pane down / up |
| `Enter` | Open selected file |
| `ESC` | Exit |

All keys are configurable — see [Configuration](#configuration).

---

## Configuration

`~/.config/fzfr/config` — JSON, all keys optional.

```json
{
  "ssh_multiplexing": false,
  "ssh_control_persist": 60,
  "ssh_strict_host_key_checking": true,
  "editor": "",
  "default_mode": "content",
  "search_history": false,
  "show_hidden": false,
  "path_format": "relative",
  "exclude_patterns": [],
  "max_stream_mb": 100,
  "keybindings": {
    "toggle_mode":            "ctrl-t",
    "toggle_ftype":           "ctrl-d",
    "toggle_hidden":          "ctrl-h",
    "filter_ext":             "ctrl-f",
    "add_exclude":            "ctrl-x",
    "refresh_list":           "ctrl-r",
    "sort_list":              "ctrl-s",
    "copy_path":              "ctrl-c",
    "open_file":              "enter",
    "preview_half_page_down": "alt-j",
    "preview_half_page_up":   "alt-k",
    "history_prev":           "ctrl-p",
    "history_next":           "ctrl-n",
    "exit":                   "esc"
  }
}
```

**Key options:**

- `ssh_multiplexing` — set to `true` if your `~/.ssh/config` does **not** already have `ControlMaster`. Do not enable if it does — the two sockets will conflict.
- `ssh_control_persist` — how long (seconds) the SSH socket stays open after last use. Lower values are safer on shared machines.
- `path_format` — `"relative"` shows paths relative to the search root; `"absolute"` shows full paths.
- `editor` — overrides `$EDITOR`. Supports flags, e.g. `"code --wait"`.
- `search_history` — set to `true` to persist search queries across sessions. Disabled by default as queries may contain sensitive terms (filenames, hostnames). Use `CTRL-P`/`CTRL-N` to navigate history when enabled.
- `exclude_patterns` — glob patterns always excluded from search, e.g. `[".git", "node_modules", "*.pyc"]`. Additional patterns can be added at runtime with `CTRL-X`.
- `max_stream_mb` — maximum size in MB for streaming a remote binary file locally for opening. Files larger than this are refused with an error. Set to `0` to disable the limit. Default: `100`.

---

## SSH Remote Search

fzfr requires no installation on the remote host — only `python3` and `fd` need to be in the remote `PATH`. The script is transferred automatically on first use.

**Script cache location on the remote host:**
- Linux: prefers `/dev/shm/fzfr/` (RAM-backed tmpfs, cleared on reboot) — falls back to `~/.cache/fzfr/` when `/dev/shm` is absent (macOS, BSD, some containers)
- The fallback `~/.cache/fzfr/` persists across reboots; delete it manually if you want to force a re-transfer

**Preview cache:** fzfr caches rendered preview output locally using the remote file's mtime (via `stat`). On minimal containers where `stat` is absent, mtime detection silently degrades and every preview re-fetches from the remote.

```sh
fzfr user@server /var/log
fzfr myserver ~/projects content
```

**SSH multiplexing:** By default fzfr defers entirely to your `~/.ssh/config`. If you do not have `ControlMaster` configured there, enable fzfr's built-in multiplexing for faster previews:

```json
{ "ssh_multiplexing": true }
```

> **Warning:** Do not set `ssh_multiplexing: true` if your `~/.ssh/config` already has `ControlMaster`. The conflicting sockets will trigger a new authentication prompt on every cursor movement.


---

## Sub-commands

fzfr follows the busybox pattern — one file, multiple commands via symlinks:

| Command | Purpose |
|---------|---------|
| `fzfr` | Main search UI |
| `fzfr-preview` | Preview a file (used by fzf internally) |
| `fzfr-open` | Open a selected file (used by fzf internally) |
| `fzfr-remote-reload` | List/search files on a remote host |
| `fzfr-remote-preview` | Preview a file on a remote host |
| `fzfr-copy` | Copy selected path to clipboard |

Sub-commands are created as symlinks by `make install` and can also be called directly or used in scripts.

---

## Testing

```sh
make test       # run unit tests (no dependencies beyond python3)
```

104 unit tests cover the pure-Python logic: quoting, path safety, config merging,
extension parsing, argument building, archive classification, state management,
backend dispatch, and script self-location. They run without `fzf`, `fd`, `rga`,
or SSH.

The subprocess, SSH, and fzf integration paths require live tools. Use this
manual battery to verify those:

```sh
mkdir /tmp/fzfr-test && cd /tmp/fzfr-test
touch 'normal.txt'
touch 'spaces in name.txt'
touch 'semi;colon.txt'
touch 'quote"file.txt'
touch "squote'file.txt"
touch '$(touch injected).txt'
touch '`touch injected2`.txt'
touch $'newline\nfile.txt'
touch -- '--help.txt'
fzfr local . name
```

Confirm: preview works for all files, nothing executes, filenames display correctly.

> **Known limitation:** filenames containing a literal newline character will appear as two
> separate entries in the list. This is an inherent limitation of newline-delimited tools
> (`fd`, `rga`, `grep`). Filenames with newlines are extremely rare in practice.

---

## Platform Notes

### Editor resolution

When opening a file, fzfr resolves the editor in this order:

1. `editor` key in `~/.config/fzfr/config`
2. `$EDITOR` environment variable
3. First available in: `nvim` → `vim` → `vi`
4. `vi` — unconditional last resort, POSIX-required on every Unix system

`vi` is guaranteed to exist on Linux, macOS, and BSD. If you want a friendlier
default, set `$EDITOR` or the `editor` config key.

### Opening files and directories (xdg-open / open)

For binary files and directories (when tmux is absent), fzfr calls `xdg-open`
to hand off to the system default application.

| Platform | Tool | Notes |
|----------|------|-------|
| Linux | `xdg-open` | Install `xdg-utils` if missing |
| macOS | `open` | Built-in, always present |
| BSD | neither | No universal equivalent without a desktop environment |

> **BSD note:** on BSD systems without a desktop environment, neither
> `xdg-open` nor `open` is available. fzfr will print a clear error message
> and the file path so you can open it manually.

### Remote host requirements

The remote host needs only `python3 ≥ 3.10` and `fd` (or `fdfind`) in its PATH.
No other installation is required.

| Requirement | Linux | macOS | BSD |
|-------------|-------|-------|-----|
| `python3 ≥ 3.10` | system or package manager | `brew install python` | ports/pkgsrc |
| `fd` | `apt install fd-find` + symlink, or `cargo install fd-find` | `brew install fd` | ports |
| `/dev/shm` (script cache) | ✓ present | ✗ absent — falls back to `~/.cache/fzfr/` | ✗ absent — falls back to `~/.cache/fzfr/` |
| `stat` (preview cache) | ✓ `stat -c %Y` | ✓ `stat -f %m` | ✓ `stat -f %m` |

### Clipboard

`fzfr-copy` detects the available tool at runtime: `pbcopy` (macOS) →
`wl-copy` (Linux Wayland) → `xclip` (Linux X11). If none is found the copy
action silently does nothing — no error is shown.

---

## Contributing


The distributable `fzfr` script is built from the source modules in `src/fzfr/`:

```
src/fzfr/
  _script.py    VERSION, SELF, SCRIPT_BYTES and bootstrap constants
  utils.py      subprocess helpers, MIME detection
  workbase.py   session working directory (prefers /dev/shm)
  config.py     default config and user config loading
  tty.py        /dev/tty prompt helper
  ssh.py        SSH option construction
  state.py      session state load/save/mutate
  cache.py      preview output cache
  archive.py    archive format detection and listing
  backends.py   LocalBackend / RemoteBackend
  preview.py    file preview rendering
  internal.py   fzf callback sub-commands (_internal-*)
  dispatch.py   _internal-dispatch router
  open.py       file open logic
  copy.py       clipboard copy
  remote.py     SSH remote search and preview
  search.py     main fzf UI entry point
scripts/
  build_single_file.py   concatenates src/ → fzfr
```

**Workflow:**

```sh
# Edit a source module, then:
make build      # rebuild fzfr + run tests
make install    # install to ~/.local/bin

# Run tests without rebuilding:
make test

# Run directly from source (local search only — SSH remote preview
# requires the built file):
PYTHONPATH=src python3 -m fzfr
```

The built `fzfr` is committed to the repo so `git clone && make install` works
without a Python build step. Never edit `fzfr` directly — always edit `src/fzfr/`
and run `make build`.

---

## Roadmap

See [TODO.md](TODO.md) for planned features.

---

## License

MIT — see [LICENSE](LICENSE).
