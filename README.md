# remotely

Fuzzy file search for local and remote filesystems.

![remotely demo](https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000)

---

## Features

- **Content search** — full-text search across files using `rga` (PDFs, archives, source) or `grep` fallback
- **Filename search** — fuzzy-filter filenames with fzf's native matching
- **Directory search** — browse and navigate directory trees
- **SSH remote search** — search and preview files on remote hosts with zero remote installation
- **Rich preview pane** — syntax-highlighted text, PDF text extraction, archive listings, hex for binaries
- **tmux integration** — opens files in a new tmux window, leaving remotely running
- **Configurable keybindings** — every key is remappable via `~/.config/remotely/config`
- **Path format** — display absolute or relative paths in the file list
- **Extension filter** — narrow results to specific file types at runtime

---

## Requirements

**Required:**

| Tool | Version | Purpose |
|------|---------|---------|
| Python | ≥ 3.10 | Runtime |
| [fzf](https://github.com/junegunn/fzf) | ≥ 0.38 | Fuzzy finder UI |
| [fd](https://github.com/sharkdp/fd) | any | Fast file listing |

**Optional — each adds a capability:**

| Tool | Capability |
|------|-----------|
| [bat](https://github.com/sharkdp/bat) | Syntax-highlighted preview |
| [rga](https://github.com/phiresky/ripgrep-all) | Content search inside PDFs, archives, and more |
| [pdftotext](https://poppler.freedesktop.org/) | PDF text extraction fallback |
| [tmux](https://github.com/tmux/tmux) | Open files in a new window without leaving remotely |
| `xclip` / `wl-copy` / `pbcopy` | Copy file path to clipboard |
| `ssh` | Remote search and preview |

---

## Installation

**From source (recommended):**

```sh
git clone https://github.com/djg11/remotely
cd remotely
make install
```

This copies `remotely` to `~/.local/bin` and creates symlinks for all sub-commands.
Make sure `~/.local/bin` is in your `PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

**Via pipx or pip:**

```sh
pipx install remotely
```
```sh
pip install remotely
```

**Uninstall:**

```sh
make uninstall        # if installed via make
pipx uninstall remotely   # if installed via pipx
pip uninstall remotely    # if installed via pip
```

---

## Usage

```sh
remotely                                    # search current git root (or cwd)
remotely local ~/projects name              # filename search in ~/projects
remotely user@server ~/documents            # remote content search
remotely myserver /var/log content          # remote content search, explicit mode
remotely local . content --exclude '*.pyc'  # exclude patterns
```

**Arguments:**

```
remotely [TARGET] [BASE_PATH] [MODE] [--exclude PATTERN ...]

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

`~/.config/remotely/config` — JSON, all keys optional.

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

remotely requires no installation on the remote host — only `python3` and `fd` need to be in the remote `PATH`. The script is transferred automatically on first use and cached at `~/.cache/remotely/` on the remote.

```sh
remotely user@server /var/log
remotely myserver ~/projects content
```

**SSH multiplexing:** By default remotely defers entirely to your `~/.ssh/config`. If you do not have `ControlMaster` configured there, enable remotely's built-in multiplexing for faster previews:

```json
{ "ssh_multiplexing": true }
```

> **Warning:** Do not set `ssh_multiplexing: true` if your `~/.ssh/config` already has `ControlMaster`. The conflicting sockets will trigger a new authentication prompt on every cursor movement.

---

## Sub-commands

remotely follows the busybox pattern — one file, multiple commands via symlinks:

| Command | Purpose |
|---------|---------|
| `remotely` | Main search UI |
| `remotely-preview` | Preview a file (used by fzf internally) |
| `remotely-open` | Open a selected file (used by fzf internally) |
| `remotely-remote-reload` | List/search files on a remote host |
| `remotely-remote-preview` | Preview a file on a remote host |
| `remotely-copy` | Copy selected path to clipboard |

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
mkdir /tmp/remotely-test && cd /tmp/remotely-test
touch 'normal.txt'
touch 'spaces in name.txt'
touch 'semi;colon.txt'
touch 'quote"file.txt'
touch "squote'file.txt"
touch '$(touch injected).txt'
touch '`touch injected2`.txt'
touch $'newline\nfile.txt'
touch -- '--help.txt'
remotely local . name
```

Confirm: preview works for all files, nothing executes, filenames display correctly.

> **Known limitation:** filenames containing a literal newline character will appear as two
> separate entries in the list. This is an inherent limitation of newline-delimited tools
> (`fd`, `rga`, `grep`). Filenames with newlines are extremely rare in practice.

---

## Contributing

The distributable `remotely` script is built from the source modules in `src/remotely/`:

```
src/remotely/
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
  build_single_file.py   concatenates src/ → remotely
```

**Workflow:**

```sh
# Edit a source module, then:
make build      # rebuild remotely + run tests
make install    # install to ~/.local/bin

# Run tests without rebuilding:
make test

# Run directly from source (local search only — SSH remote preview
# requires the built file):
PYTHONPATH=src python3 -m remotely
```

The built `remotely` is generated by `make build` and gitignored — never edit it
directly. Always edit `src/remotely/` and run `make build`.

---

## Roadmap

See [TODO.md](TODO.md) for planned features.

---

## License

MIT — see [LICENSE](LICENSE).
