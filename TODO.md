# remotely — Roadmap

The goal is narrow and specific: be a **zero-install SSH transport** that
any fuzzy-finder (fzf, Television, etc.) can call as shell commands:

```sh
remotely list    user@host /path          # -> newline-delimited file paths on stdout
remotely preview user@host /path          # -> rendered file content on stdout
remotely list    host1:/path host2:/path  # -> merged results from multiple hosts
```

The UI (Television, fzf) owns everything else: fuzzy matching, keybindings,
git channels, docker, history, syntax highlighting. remotely is the pipe that
makes remote files visible to those UIs — including across multiple hosts
simultaneously, because the host prefix on each output line is transport
metadata that only remotely understands.

---

## What is done

- **Remote search and preview** over SSH with hash-based agent caching —
  bootstrap is ~250 bytes; full upload only on cold cache.
- **Headless transport API** — `remotely list`, `remotely preview`,
  `remotely open` work as standalone shell commands with no fzf dependency.
- **Multi-host listing** — multiple `host:/path` targets streamed in parallel,
  results prefixed with `host:` for transparent routing.
- **Session management** — anchor-PID session dirs under `/dev/shm/remotely/`,
  reaper process for automatic cleanup, `remotely gc` for stragglers.
- **Preview cache** — LRU cache keyed on `(path, mtime, query)` to avoid
  repeated SSH round-trips for unchanged files.
- **Remote agent caching** — cached at `/dev/shm/remotely/<hash>.py` (RAM)
  or `~/.cache/remotely/<hash>.py` (disk fallback).
- **Binary open** — `remotely open` detects MIME type; text files go to
  `$EDITOR` with sync-back; binary files stream to session cache and launch
  `xdg-open`/`open` with OOM guard.
- **Security hardening** — `shlex.quote`/`shlex.join` throughout, Semgrep
  rules, path boundary checks, symlink-safety on `/dev/shm`.
- **Single-file build** — `scripts/build_single_file.py` produces the
  distributable `remotely` script; CI publishes to PyPI on release.

---

## Phase 1 — Polish headless API (top priority)

### 1.1 `--version` flag

```sh
remotely --version   # -> remotely 0.9.5
```

### 1.2 `remotely open` — binary OOM guard for local files

Currently `_open_local` does not apply the `max_stream_mb` / free-space
checks before launching `xdg-open`. Mirror the remote path's OOM guard so
large local binaries are refused with a helpful message rather than
silently opened.

### 1.3 Backward compatibility hint

If no sub-command is given and argv does not match any known pattern,
print a one-line usage hint to stderr pointing at `remotely list`, then
exit 1. Currently the fallback silently runs `cmd_list` which may confuse
users who mistyped a sub-command.

---

## Phase 2 — Connection Reliability

### 2.1 Locking on socket creation

Multiple preview callbacks fire in parallel (Television's async previewer,
fzf's preview window). Two processes racing to create the ControlMaster
socket cause one to fail. Add `fcntl.flock` on a `.lock` file alongside the
socket so only one process runs `ssh -M`; others wait and reuse the socket.
This is already implemented in `session.py:acquire_socket` — verify it
covers all code paths in `list.py` and `open_cmd.py`.

### 2.2 `ssh` timeout

`subprocess.run(["ssh", ...])` with no `timeout=` hangs forever on a broken
connection. Add `ConnectTimeout=5` to `_ssh_opts()` unconditionally (no
effect when the socket already exists). The Semgrep rule
`remotely-ssh-call-no-timeout` already flags remaining cases.

### 2.3 Stale socket detection

If a remote host reboots mid-session, the ControlMaster socket exists locally
but is dead. Detect `ssh -O check` failure and recreate the socket rather
than returning an error to the UI.

---

## Phase 3 — Named Host Groups

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

`@name` expands to the configured list of hosts. The `@` sigil is
unambiguous with SSH host strings.

---

## Phase 4 — Agent Size & Execution

### 4.1 Minification

Add `--minify` to `build_single_file.py`: strip comments and docstrings from
the built monolith. Target: ~60 KB -> <30 KB. Required before `memfd_create`
because without a persistent remote cache, script size equals preview latency
on every cold call.

### 4.2 In-memory execution (`memfd_create`)

Replace the on-disk remote cache with a truly anonymous RAM fd on Linux:

```python
fd = ctypes.CDLL(None).memfd_create(b"remotely", 0)
os.write(fd, script_bytes)
os.execve(f"/proc/self/fd/{fd}", [sys.executable] + sys.argv, os.environ)
```

Priority on the remote: `memfd_create` -> `/dev/shm` mkstemp+unlink ->
`tmpdir` mkstemp+unlink. Bypasses `noexec` mounts on `/tmp` common on
hardened servers.

**Dependency**: minification (4.1) must land first.

### 4.3 Python 3.6 compatibility audit

The remote agent must run on CentOS 7 / RHEL 7 (Python 3.6). The built
monolith currently uses `str | None` union syntax (3.10+) in type comments.

- Add `--remote-python 3.6` flag to `build_single_file.py` that rewrites
  `X | Y` -> `Optional[X]`, etc., or fails loudly on violations.
- Add a CI step that runs the built monolith under a `python:3.6-slim`
  Docker image and asserts exit code 0.

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

**`docs/fzf-example.sh`** (see also EXAMPLES.md):

```sh
# Single host
remotely list user@host ~/projects \
  | fzf --preview 'remotely preview {}'

# Multiple hosts
remotely list host1:/var/log host2:/var/log \
  | fzf --preview 'remotely preview {}'
```

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

---

## Recommended implementation order

1. `--version` flag and usage hint on bad sub-command (Phase 1.1, 1.3)
2. Local binary OOM guard (Phase 1.2)
3. SSH timeout + stale socket detection (Phase 2.2, 2.3)
4. Named host groups (Phase 3)
5. Minification (Phase 4.1)
6. `memfd_create` (Phase 4.2)
7. Reference channel configs (Phase 5)
8. Python 3.6 compat CI (Phase 4.3) — only if CentOS 7 support is needed
