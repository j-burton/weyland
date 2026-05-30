# weyland MCP connector

The MCP service that runs on each minion Pi, giving chat-Claude a
remote control over the Pi.

## Design

- **Default-allow, not default-deny.** No prompting back to chat-Claude
  for per-command approval. The caller is trusted.
- **Full sudo via passwordless sudo for the service user.**
- **Small denylist** for credential files (`/etc/shadow`, `~/.ssh/`,
  the per-Pi repo's `.git/config`). Everything else is open.
- **One bearer token** per Pi. The token's hash is the only auth
  check.

## Verbs

| Group | Verb | Purpose |
|---|---|---|
| fs | `read_file` | Read a text file. Caps at 1 MiB. |
| fs | `write_file` | Write a text file. Caps at 1 MiB. |
| fs | `list_dir` | List directory entries. |
| fs | `glob` | Glob a pattern (supports `**`). |
| shell | `run_command` | Exec one command with args, capture stdout/stderr. |
| shell | `run_shell` | Run a `bash -c '<cmd>'` shell line. |
| tmux | `tmux_send_keys` | Inject keys into a tmux session. |
| tmux | `tmux_capture_pane` | Capture a pane's contents. |
| tmux | `tmux_list` | List sessions/windows/panes. |
| systemd | `systemctl_status` | Get unit status. |
| systemd | `systemctl_restart` | Restart a unit (`sudo systemctl restart`). |
| systemd | `install_unit` | Write a unit file to `/etc/systemd/system/` and enable. |
| net | `http_get` | GET a URL, return body. |
| net | `http_post` | POST a URL with body. |
| github | `git_status` | `git status --porcelain=v1 --branch` in a repo. |
| github | `git_log` | Recent commits. |
| github | `git_pull` | Fast-forward pull. |
| github | `git_commit_push` | Stage, commit, push. |

## Config (env vars)

| Var | Purpose |
|---|---|
| `WEYLAND_BEARER_TOKEN_HASH` | sha256 of the bearer token (lowercase hex) |
| `WEYLAND_BIND_HOST` | default `127.0.0.1` |
| `WEYLAND_BIND_PORT` | default `5002` |
| `WEYLAND_PUBLIC_URL` | e.g. `https://coffee.julianburton.com/mcp` |
| `WEYLAND_LOG_PATH` | default `/var/log/weyland-mcp.log` |
| `WEYLAND_PI_NAME` | this Pi's name |
| `WEYLAND_PI_REPO` | e.g. `j-burton/coffee-pi` |
| `WEYLAND_PI_DIR` | local path of the per-Pi repo clone |

## Running

```
weyland-mcp
```

Reads env vars (typically from `/etc/weyland/mcp.env` via systemd
`EnvironmentFile=`). Listens on `WEYLAND_BIND_HOST:WEYLAND_BIND_PORT`.
The Cloudflare tunnel fronts it at `WEYLAND_PUBLIC_URL`.
