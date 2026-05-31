# weyland MCP connector

The MCP service that runs on each minion Pi, giving chat-Claude a
remote control over the Pi.

## Design

- **Default-allow, not default-deny.** No prompting back to chat-Claude
  for per-command approval. The caller is trusted.
- **Full sudo via passwordless sudo for the service user.**
- **Small denylist** for credential files (`/etc/shadow`, `~/.ssh/`,
  the per-Pi repo's `.git/config`). Everything else is open.
- **OAuth 2.1** (public client + PKCE, with dynamic client registration).
  On first connect Claude Desktop is redirected to the Pi's
  `/weyland-consent` form, where the per-Pi bearer token is pasted once; the
  connector checks its sha256 against `WEYLAND_BEARER_TOKEN_HASH`. The granted
  client token is persisted at `WEYLAND_TOKEN_STORE` so it survives restarts.

## Verbs

| Group | Verb | Purpose |
|---|---|---|
| identity | `whoami` | This Pi's name/repo/dir/public URL + connector version. |
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
| wake | `restart_wake` | Re-arm the tmux watcher between tasks (flips `wake-mode` off→on); reports `watcher_alive` + `watcher_pid`. |

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
| `WEYLAND_OAUTH_CLIENT_ID` | OAuth client id (default `weyland-mcp-claude-ai`) |
| `WEYLAND_TOKEN_STORE` | persisted client tokens (default `/var/lib/weyland-mcp/tokens.json`) |

## Running

```
weyland-mcp
```

Reads env vars (typically from `/etc/weyland/mcp.env` via systemd
`EnvironmentFile=`). Listens on `WEYLAND_BIND_HOST:WEYLAND_BIND_PORT`.
The Cloudflare tunnel fronts it at `WEYLAND_PUBLIC_URL`.
