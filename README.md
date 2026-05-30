# weyland

The forge for Julian's Pi fleet — bootstrap and shared infrastructure.

## What this repo is

`weyland` is the **factory** that produces fleet-ready Pis. It is identical for every Pi, forever. It contains:

- `bootstrap/` — the install script a fresh Pi runs to join the fleet.
- `connector/` — source for the MCP connector that each Pi runs so chat-Claude can reach it.
- `templates/` — per-Pi document templates copied into each new per-Pi repo at bootstrap time.
- `docs/` — design docs about the fleet itself.

## What this repo is NOT

Each Pi has its **own separate repo** for its identity, state, history, and work. That repo is named after the Pi (e.g. `coffee-pi`, `unifi-pi`) and is created automatically by the bootstrap script.

If you're a chat-Claude opened to work on a specific Pi, you are in the wrong place — go read that Pi's own repo.

## The bootstrap ritual

Julian's perspective:

1. Install fresh Pi OS on a Pi. Get it on the network. Find its IP.
2. SSH in.
3. Paste **one command** (see `docs/NEW_PI.md`).
4. Answer one question: what should the Pi be called?
5. Do two browser Authorize dances (GitHub, Cloudflare). Each is one click.
6. When the script finishes, create a new Claude Desktop project pointed at the per-Pi repo it created. This is the only GUI step.
7. Done. From here on, everything for that Pi happens by talking to Claude in its project.

## What the bootstrap does

When the install script runs on a fresh Pi:

- Installs Claude Code, signs in.
- Installs the weyland MCP connector (from `connector/` in this repo) and registers it.
- Sets up a Cloudflare tunnel so chat-Claude can reach the Pi from anywhere.
- Creates a new GitHub repo `<pi-name>-pi` under `j-burton`, seeded from `templates/`.
- Clones the new per-Pi repo onto the Pi.
- Starts a long-lived tmux session for CC.
- Prints the per-Pi repo URL and the Claude-project-creation reminder.

## How a chat-Claude on the weyland project should orient

If you're opened in a chat scoped to `weyland` (not a specific Pi), read in order:

1. This README.
2. `docs/DESIGN.md` — fleet architecture, why we made certain choices.
3. `docs/OPERATING.md` — how chat-Claude actually runs sessions for weyland itself.

You will not be solving Pi-specific problems here. You will be improving the factory: editing the install script, the connector, the templates, the docs.

## Status

Early. Bootstrap script is in development. First Pi to be onboarded: TBD.
