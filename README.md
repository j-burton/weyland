# weyland

Wayland, the legendary smith of Norse and Anglo-Saxon myth, once
forged enchanted weapons for kings and gods. He serves a new master
now: Julian, and the Claude-driven Raspberry Pi minions he forges
for him.

Each Pi ("minion") is set up by one curl command, after which it
lives in its own GitHub repo and is reachable from any Claude surface
(web, desktop, mobile) via a custom connector.

## Bootstrap a new Pi

SSH into a fresh Pi running Raspberry Pi OS (or any Debian-family
Linux), then paste:

```
curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh | bash
```

That's the whole bootstrap. Expect ~5 minutes and three browser
dances (GitHub, Cloudflare, Anthropic). Detailed walkthrough in
`docs/NEW_PI.md`.

## What you get

When the bootstrap finishes:

- A new private GitHub repo at `j-burton/<pi-name>-pi` to hold this
  Pi's state, modules, and handoffs.
- A Cloudflare tunnel exposing the Pi's MCP service at
  `https://<pi-name>.julianburton.com/mcp`.
- Claude Code running in a tmux session named after the Pi.
- A wake system that pings Julian's phone via Pushcut when CC stalls
  or finishes a task.
- A bearer token + URL printed at the end — paste those into Claude
  Desktop's "Add custom connector" dialog to make the Pi reachable.

## Repo layout

- `bootstrap/install.sh` — the one-liner. 8 phases, each idempotent.
- `connector/` — the Python MCP service that runs on each Pi.
- `connector/scripts/` — the wake system (`cc-notify` hook +
  `cc-tmux-watcher` daemon, `install-wake.sh` installer).
- `connector/systemd/` — systemd unit templates.
- `templates/` — files seeded into each per-Pi repo at bootstrap.
- `docs/` — design rationale (`DESIGN.md`), operating notes
  (`OPERATING.md`), the full new-Pi walkthrough (`NEW_PI.md`).

## When something breaks

The bootstrap is re-runnable. Run the curl command again — completed
phases are skipped. If it's truly stuck, reflash the SD card and start
over. The recovery model for minions is reflash, not repair.

## Editing weyland

After the first minion is up, weyland edits itself. Open a Claude
project pointed at any minion's repo, ask for a bootstrap change, and
that minion's CC will clone weyland, make the change, and push.
Future minions get the improvement automatically.
