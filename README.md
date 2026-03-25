# remotely

Zero-install SSH file transport for fuzzy-finders (fzf, Television, etc.).

```sh
remotely list    user@host:/var/log          # stream file paths to stdout
remotely preview user@host:/var/log/app.log  # render file content to stdout
remotely open    user@host:/etc/nginx.conf   # edit remote file in $EDITOR
```

Plug directly into any UI that accepts a `source_command` and a
`preview_command` — no configuration on the remote host beyond `python3`
and `fd` in `$PATH`.

---

## Features

- **Zero remote installation** — the script bootstraps itself over SSH stdin
  and caches at `~/.cache/remotely/` on the remote. Only `python3` and `fd`
  are required on the remote host.
- **Multi-host listing** — merge results from multiple SSH hosts in parallel,
  each line prefixed with `host:` for transparent routing.
- **Rich file preview** — syntax-highlighted text (bat), PDF text extraction
  (pdftotext / rga), archive listings, hex dumps for binaries.
- **Edit-and-sync** — `remotely open` streams a remote file to `/dev/shm`,
  opens it in `$EDITOR`, and syncs it back on save.
- **Content search** — full-text search via `rga` (PDFs, archives, source)
  or `grep` fallback.
- **SSH multiplexing** — optional managed ControlMaster for fast parallel
  preview calls, or defer to `~/.ssh/config` (the default).

---

## Python version policy

| Context | Requirement |
|---------|-------------|
| **Remote host** (the built `remotely` script) | Python **3.6+** |
| **Local dev toolchain** (ruff, pytest, pyright) | Python **3.10+** |

The built script is intentionally kept compatible with Python 3.6 so it
runs on CentOS 7 / RHEL 7 remote hosts without any system upgrades.
Development happens on Python 3.10+. Use `make test36` to verify the built
script against a local Python 3.6 interpreter before releasing.

---

## Requirements

**On the local machine (running `remotely list` / `preview` / `open`):**

| Tool | Purpose |
|------|---------|
| Python ≥ 3.10 | Dev toolchain; the built script also runs here |
| [fd](https://github.com/sharkdp/fd) | Local file listing |
| `ssh` | Remote access |

**On the remote host (installed automatically):**

| Tool | Purpose |
|------|---------|
| Python ≥ 3.6 | Runs the bootstrapped remotely agent |
| [fd](https://github.com/sharkdp/fd) | Remote file listing |

**Optional — each adds a capability (local or remote as noted):**

| Tool | Where | Capability |
|------|-------|-----------|
| [bat](https://github.com/sharkdp/bat) | remote | Syntax-highlighted preview |
| [rga](https://github.com/phiresky/ripgrep-all) | remote | Content search inside PDFs and archives |
| [pdftotext](https://poppler.freedesktop.org/) | remote | PDF text extraction fallback |
| [tmux](https://github.com/tmux/tmux) | local | Open files in a new window |

---

## Installation

**From source (recommended):**

```sh
git clone https://github.com/djg11/remotely
cd remotely
make install
```

This copies `remotely` to `~/.local/bin` and creates symlinks for all
sub-commands. Make sure `~/.local/bin` is in your `PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

**Via pipx or pip:**

```sh
pipx install remotely-ssh
pip install remotely-ssh
```

**Uninstall:**

```sh
make uninstall
pipx uninstall remotely-ssh
```

---

## Usage

### List files

```sh
remotely list local ~/projects              # local filesystem
remotely list user@host:/var/log            # single remote host
remotely list user@host:~/projects          # tilde path
remotely list host1:/var/log host2:/var/log # multiple hosts (parallel)
remotely list user@host --hidden            # include hidden files
remotely list user@host --exclude '*.pyc'   # exclude patterns (repeatable)
remotely list user@host --format json       # JSON output with kind metadata
```

Remote results are prefixed with `host:` so `remotely preview` and
`remotely open` can route back to the correct host:

```
host1:/var/log/app.log
host1:/var/log/nginx/access.log
host2:/var/log/app.log
```

### Preview a file

```sh
remotely preview /local/path/file.py           # local
remotely preview user@host:/var/log/app.log    # remote
remotely preview user@host:/var/log/app.log "error"  # with search query
```

The path format from `remotely list` is accepted directly:

```sh
remotely list user@host:/var/log | fzf --preview 'remotely preview {}'
```

### Open / edit a file

```sh
remotely open /local/file.txt               # local
remotely open user@host:/etc/nginx.conf     # remote -- streams, edits, syncs back
```

### Television integration

```toml
# ~/.config/television/cable.toml

[[cable.channels]]
name = "ssh-files"
source_command = "remotely list user@host:~/projects"
preview_command = "remotely preview {}"

[[cable.channels]]
name = "ssh-fleet"
source_command = "remotely list host1:/var/log host2:/var/log"
preview_command = "remotely preview {}"
```

### fzf integration

```sh
# Single host
remotely list user@host:~/projects \
  | fzf --preview 'remotely preview {}'

# Multiple hosts
remotely list host1:/var/log host2:/var/log \
  | fzf --preview 'remotely preview {}'
```

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
  "show_hidden": false,
  "path_format": "relative",
  "exclude_patterns": [],
  "max_stream_mb": 100
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `ssh_multiplexing` | `false` | Enable remotely-managed ControlMaster. Set to `true` only if `~/.ssh/config` does **not** already have `ControlMaster` — two masters on the same host conflict and cause spurious auth prompts. |
| `ssh_control_persist` | `60` | Seconds the SSH socket stays open after last use. |
| `ssh_strict_host_key_checking` | `true` | Enforce host key verification. |
| `editor` | `""` | Override `$EDITOR`. Supports flags, e.g. `"code --wait"`. |
| `default_mode` | `"content"` | Default search mode: `"content"` or `"name"`. |
| `show_hidden` | `false` | Include hidden files by default. |
| `path_format` | `"relative"` | `"relative"` or `"absolute"` paths in output. |
| `exclude_patterns` | `[]` | Glob patterns always excluded, e.g. `[".git", "node_modules"]`. |
| `max_stream_mb` | `100` | Max file size (MB) for `remotely open` remote streaming. `0` disables the limit. |

---

## SSH remote search

The remote host requires no prior setup. On the first `remotely list` or
`remotely preview` call, the script uploads itself (~100 KB) via SSH stdin
and caches it at `~/.cache/remotely/<hash>.py` (or `/dev/shm/remotely/`
on Linux for RAM-backed storage). Subsequent calls send only a ~250-byte
bootstrap that checks the cache.

```sh
remotely list user@server /var/log
remotely list myserver ~/projects
```

**SSH multiplexing:** By default remotely defers entirely to `~/.ssh/config`.
Enable remotely's own ControlMaster only if you have no `ControlMaster` in
your SSH config:

```json
{ "ssh_multiplexing": true }
```

> **Warning:** Do not set `ssh_multiplexing: true` if `~/.ssh/config` already
> has `ControlMaster`. Conflicting sockets trigger auth prompts on every call.

---

## Sub-commands

remotely uses busybox-style dispatch — one script, multiple commands:

| Command | Purpose |
|---------|---------|
| `remotely list` | Stream file paths from one or more targets to stdout |
| `remotely preview` | Render a file to stdout (local or remote) |
| `remotely open` | Open a file in `$EDITOR`, sync back on save |
| `remotely-preview` | Low-level preview (called internally by preview UIs) |
| `remotely-remote-reload` | Internal: list files on a remote host |
| `remotely-remote-preview` | Internal: preview a file on a remote host |

`make install` copies the script to `~/.local/bin` and creates the symlinks.

---

## Development

**Dev toolchain requires Python 3.10+.**

```sh
python3.10 -m venv .venv && source .venv/bin/activate
make dev-install    # installs dev extras and pre-commit hooks
make build          # rebuild remotely + run tests (requires 3.10+)
make test           # run tests only
make test36         # verify built script under python3.6 (runtime target)
make lint           # ruff check + format check
make format         # ruff check --fix + ruff format
```

The distributable `remotely` script is built from source modules in
`src/remotely/` by `scripts/build_single_file.py`:

```
src/remotely/
  _script.py     VERSION, SELF, SCRIPT_BYTES and bootstrap constants
  utils.py       subprocess helpers, MIME detection, SSH path resolution
  workbase.py    session working directory (prefers /dev/shm)
  config.py      default config and user config loading
  ssh.py         SSH option construction
  session.py     host-keyed SSH session manager (socket lifecycle + locking)
  state.py       session state load/save/mutate
  cache.py       preview output cache
  archive.py     archive format detection and listing
  backends.py    LocalBackend, RemoteBackend
  preview.py     file preview rendering
  remote.py      SSH remote search and preview
  list.py        remotely list headless sub-command
  preview_cmd.py remotely preview headless sub-command
  open_cmd.py    remotely open headless sub-command
scripts/
  build_single_file.py   concatenates src/ -> remotely
  check_imports.py       detects late imports that break the build
```

The built `remotely` is gitignored — never edit it directly. Always edit
`src/remotely/` and run `make build`.

**Pre-commit workflow:**

```sh
make pre-commit   # ruff fixes files; hooks report failure (files changed)
git add -u        # stage the ruff-fixed files
make pre-commit   # second run: 0 changes, all hooks pass
make build
```

---

## Roadmap

See [TODO.md](TODO.md) for planned features (multi-host groups, minification,
`memfd_create` in-memory execution, Television reference configs).

---

## License

MIT — see [LICENSE](LICENSE).
