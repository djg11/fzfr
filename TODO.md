# remotely — Roadmap

The goal is narrow and specific: be a **zero-install SSH transport** that
any fuzzy-finder (fzf, Television, etc.) can call as shell commands:

```sh
remotely list    user@host /path          # → newline-delimited file paths on stdout
remotely preview user@host /path          # → rendered file content on stdout
remotely list    host1:/path host2:/path  # → merged results from multiple hosts
```

The UI (Television, fzf) owns everything else: fuzzy matching, keybindings,
git channels, docker, history, syntax highlighting. remotely is the pipe that
makes remote files visible to those UIs — including across multiple hosts
simultaneously, because the host prefix on each output line is transport
metadata that only remotely understands. A UI cannot invent that prefix.

---

## What is done

- **Remote search and preview** over SSH with hash-based agent caching —
  bootstrap is ~250 bytes; full upload only on cold cache.
- **Session management** — ControlMaster multiplexing, per-session directories
  under `/dev/shm/remotely/`, atomic state files, cleanup on exit.
- **Preview cache** — LRU cache keyed on `(path, mtime, query)` to avoid
  repeated SSH round-trips for unchanged files.
- **Remote agent caching** — cached at `/dev/shm/remotely/<hash>.py` (RAM)
  or `~/.cache/remotely/<hash>.py` (disk fallback).
- **Security hardening** — `shlex.quote`/`shlex.join` throughout, Semgrep
  rules, path boundary checks, symlink-safety on `/dev/shm`.
- **Single-file build** — `scripts/build_single_file.py` produces the
  distributable `remotely` script; CI publishes to PyPI on release.
- **fzf TUI** — the original standalone fzf UI still works; it is now a
  legacy consumer of the same internal backend.

---

## Phase 1 — Clean Headless API (top priority)

The internal plumbing (`cmd_remote_reload`, `cmd_remote_preview`) already does
the right thing. This phase exposes it behind a stable, UI-agnostic interface.

### 1.1 `remotely list <target> <path> [options]`

Stream file paths from a remote (or local) host to stdout and exit.

```sh
remotely list user@host ~/projects
remotely list user@host ~/projects --relative
remotely list user@host ~/projects --hidden
remotely list user@host ~/projects --exclude '*.pyc'
remotely list local ~/projects
```

- Establishes (or reuses) a ControlMaster socket, bootstraps the remote agent
  if not cached, runs `fd` on the remote, streams results, exits.
- No state file, no session directory, no TUI.
- `--format=json` emits `{"path": "...", "kind": "text|binary|archive|pdf"}` per
  line for UIs that want metadata.
- `target=local` runs `fd` directly — no SSH.

### 1.2 `remotely preview <target> <path> [query]`

Render a single file to stdout and exit.

```sh
remotely preview user@host /var/log/app.log
remotely preview user@host /var/log/app.log "error"
remotely preview local ~/projects/main.py
```

- Reuses the existing bootstrap/cache logic from `cmd_remote_preview`.
- `target=local` calls `cmd_preview` directly — no SSH.
- Output is raw bytes with ANSI colour codes (bat, rga, xxd as available).
- The `<target>` prefix in the path argument is parsed back out here so the
  UI can pass a multi-host result line directly:
  `remotely preview user@host:/var/log/app.log`.

### 1.3 `remotely open <target> <path>`

Open a remote file in `$EDITOR` and sync back on save.

```sh
remotely open user@host /etc/nginx/nginx.conf
```

- Streams file to a local temp path in `/dev/shm`, launches `$EDITOR`, watches
  for save, syncs back via `scp`/`cat`.
- Already implemented in `open.py`; needs extraction as a standalone headless
  entry point independent of the fzf session.

### 1.4 Backward compatibility

If no sub-command is given and argv matches the old `remotely [target] [path]`
pattern, fall through to `cmd_search` (the fzf TUI). Print a one-line
deprecation hint to stderr pointing at the new sub-commands.

---

## Phase 2 — Multi-Host Support

Television and fzf have a single `source_command` field — they run one command
and read its stdout. They cannot natively aggregate multiple SSH hosts. Because
the host prefix on each output line is transport metadata that only remotely
understands, multi-host merging belongs here.

### 2.1 `remotely list` with multiple targets

Accept multiple `host:/path` arguments. Run all SSH sessions in parallel via
`threading`, prefix each result line with its source host, and stream the
merged output to stdout.

```sh
remotely list host1:/var/log host2:/var/log host3:/var/log
remotely list host1:~/projects host2:~/projects --hidden
```

**Output format** (newline-delimited, host-prefixed):
```
host1:/var/log/app.log
host1:/var/log/nginx/access.log
host2:/var/log/app.log
...
```

With `--format=json`:
```json
{"host": "host1", "path": "/var/log/app.log", "kind": "text"}
```

**Implementation notes:**
- Each `host:/path` pair maps to one `RemoteBackend` instance.
- `threading.Thread` per host (not asyncio — `subprocess` is the bottleneck,
  not the event loop).
- A shared `queue.Queue` collects lines from all threads; the main thread drains
  it to stdout in arrival order.
- Each host gets its own ControlMaster socket under `/dev/shm/remotely/` so
  connections are independent and failures on one host do not block others.
- A failed host prints a single error line to stderr and the thread exits; the
  remaining hosts continue streaming.

### 2.2 `remotely preview` with host-prefixed paths

Once `remotely list` emits `host:/path` lines, the UI passes them verbatim to
`remotely preview`. Parse the prefix to route to the correct host:

```sh
remotely preview host1:/var/log/app.log
remotely preview host1:/var/log/app.log "error"
```

- Split on the first `:` that is followed by `/` to extract host and path.
- Fall back to treating the whole argument as a local path if no valid host
  prefix is found (backward compatible with single-host usage).

### 2.3 Named host groups (config)

For frequently used combinations, allow naming groups in
`~/.config/remotely/config`:

```json
{
  "host_groups": {
    "web": ["web01", "web02", "web03"],
    "db":  ["db-primary", "db-replica"]
  }
}
```

Then:
```sh
remotely list @web /var/log
remotely list @db ~/data
```

`@name` expands to the configured list of hosts. The `@` sigil is unambiguous
with SSH host strings.

---

## Phase 3 — Connection Reliability

### 3.1 Locking on socket creation

Multiple preview calls fire in parallel (Television's async previewer, fzf's
preview window). Two processes racing to create the ControlMaster socket cause
one to fail. Add `fcntl.flock` on a `.lock` file alongside the socket so only
one process runs `ssh -M`; others wait and reuse the socket.

This is especially important for multi-host sessions where N sockets are created
simultaneously.

### 3.2 `ssh` timeout

`subprocess.run(["ssh", ...])` with no `timeout=` hangs forever on a broken
connection. Add `ConnectTimeout=5` to `_ssh_opts()` unconditionally (no effect
when the socket already exists). The Semgrep rule `remotely-ssh-call-no-timeout`
already flags this.

### 3.3 Stale socket detection

If a remote host reboots mid-session, the ControlMaster socket exists locally
but is dead. Detect `ssh -O check` failure and recreate the socket rather than
returning an error to the UI.

---

## Phase 4 — Agent Size & Execution

### 4.1 Minification

Add `--minify` to `build_single_file.py`: strip comments and docstrings from
the built monolith. Target: ~60 KB → <30 KB. Required before `memfd_create`
because without a persistent remote cache, script size equals preview latency
on every cold call.

### 4.2 In-memory execution (`memfd_create`)

Replace the on-disk remote cache with a truly anonymous RAM fd on Linux:

```python
fd = ctypes.CDLL(None).memfd_create(b"remotely", 0)
os.write(fd, script_bytes)
os.execve(f"/proc/self/fd/{fd}", [sys.executable] + sys.argv, os.environ)
```

Priority on the remote: `memfd_create` → `/dev/shm` mkstemp+unlink → `tmpdir`
mkstemp+unlink. Bypasses `noexec` mounts on `/tmp` common on hardened servers.

**Dependency**: minification (4.1) must land first.

### 4.3 Python 3.6 compatibility

The remote agent must run on CentOS 7 / RHEL 7 (Python 3.6). The built
monolith currently uses `str | None` union syntax (3.10+).

- Add `--remote-python 3.6` flag to `build_single_file.py` that rewrites
  `X | Y` → `Optional[X]`, etc., or fails loudly on violations.
- Add a CI step that runs the built monolith under a `python:3.6-slim` Docker
  image and asserts exit code 0.

---

## Phase 5 — Reference Channel Configs

Once the headless and multi-host APIs are stable, provide copy-paste configs
for the two target UIs in `docs/`.

**`docs/television-cable.toml`** (single host):
```toml
[metadata]
name = "ssh-files"

[source]
command = "remotely list user@host ~/projects"

[preview]
command = "remotely preview {}"
```

**`docs/television-multihost.toml`**:
```toml
[metadata]
name = "ssh-fleet"

[source]
command = "remotely list host1:~/projects host2:~/projects host3:~/projects"

[preview]
command = "remotely preview {}"
```

**`docs/fzf-example.sh`**:
```sh
# single host
remotely list user@host ~/projects \
  | fzf --preview 'remotely preview {}'

# multiple hosts
remotely list host1:/var/log host2:/var/log \
  | fzf --preview 'remotely preview {}'
```

Note that `remotely preview {}` works for both single and multi-host because
`{}` expands to the full `host:/path` line from `remotely list`.

---

## What remotely will NOT implement

The following are explicitly out of scope because fzf and Television handle
them natively:

- Fuzzy matching and ranking
- Keybindings and UI actions
- Git channels (`git ls-files` piped into fzf)
- Docker / Kubernetes channels
- Search history
- Syntax highlighting in the list pane
- Channel switching UI
- Interactive file operations (rename, delete)

The fzf TUI (`cmd_search`, `build_fzf_invocation`, custom actions) will not
receive new features. It remains as a legacy consumer and integration test
harness but is no longer the primary interface.

---

## Recommended implementation order

1. `remotely list` single-host (Phase 1.1)
2. `remotely preview` single-host with `host:/path` parsing (Phase 1.2)
3. `remotely list` multi-host (Phase 2.1)
4. `remotely preview` multi-host routing (Phase 2.2)
5. Connection locking + timeout (Phase 3.1, 3.2)
6. Named host groups (Phase 2.3)
7. Minification (Phase 4.1)
8. `memfd_create` (Phase 4.2)
9. Reference channel configs (Phase 5)
10. Python 3.6 compat (Phase 4.3) — only if CentOS 7 support is needed
11. `remotely open` headless (Phase 1.3) — low priority; most UIs handle
    open via their own action config
