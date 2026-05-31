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
bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh)
```

Expect ~5 minutes and four browser dances (GitHub, Tailscale,
Cloudflare, Anthropic). Detailed walkthrough in `docs/NEW_PI.md`.

## Set up a Claude Desktop project for it

After the bootstrap finishes, create a new Claude Desktop project
named after the Pi, add the connector printed in the bootstrap
summary, then paste this into the project's **Instructions** field:

```
You are working on a single Raspberry Pi minion in Julian's fleet.

This project's MCP connector talks to one specific Pi. Before doing
anything else, ask Julian which Pi this project is for (e.g.
"unifi", "coffee"), then use the connector to read these files in
order from /opt/<pi-name>-pi/ on the Pi:

  README.md
  IDENTITY.md
  CURRENT_STATE.md
  MODULES.md

The README has Julian's communication rules and how to drive the
Pi. Follow those rules. When you finish a task, fire Pushcut to
Julian's phone so he knows.
```

Same instructions for every minion's project — chat-Claude asks for
the Pi name when you first open a chat.

## What you get

When the bootstrap finishes:

- A new private GitHub repo at `j-burton/<pi-name>-pi`, **seeded with
  templates and already self-documented** by CC (hardware, software, and
  purpose) before you open the first chat.
- The Pi joined to Tailscale — reachable from anywhere via
  `ssh admin@<pi-name>.tail<tailnet>.ts.net`, not just the LAN.
- A Cloudflare tunnel exposing the Pi's MCP service at
  `https://<pi-name>.julianburton.com/mcp`.
- Claude Code running in a tmux session named after the Pi.
- A wake system: PC pings to chat-Claude when CC finishes or stalls,
  escalating to a Pushcut on Julian's phone only as a last resort. The PC
  wake hostname is configured during the identity phase.
- Fleet secrets (Pushcut webhook, etc.) fetched from the private
  `weyland-secrets` vault during bootstrap — no per-Pi secret management.
- An OAuth 2.1 connector — add the URL + Client ID in Claude Desktop, then
  paste the one-time bearer token into the Pi's consent page (all printed in
  the summary).

## Repo layout

- `bootstrap/install.sh` — the one-liner. 12 phases, each idempotent.
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
