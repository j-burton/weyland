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

## Reset a Pi to start fresh

To wipe a **disposable / test** Pi back to a clean, pre-bootstrap state and
run again from scratch — signs it out of Claude, GitHub, Tailscale and
Cloudflare, and removes weyland's state, the per-Pi repo, the Claude
auto-start service and the Cloudflare tunnel (installed tools are left in
place):

```
bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/reset.sh)
bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh)
```

Safety: reset refuses to run on protected Pis (`unifiviewer`, `argos`), refuses
to run from inside tmux, and won't touch anything until you type the Pi's
hostname to confirm.

## Set up a Claude Desktop project for it

After the bootstrap finishes, create a new Claude Desktop project
named after the Pi, add the connector printed in the bootstrap
summary, then paste this into the project's **Instructions** field:

```
WEYLAND PROJECT INSTRUCTIONS — v2 (2026-06-03)

You are working in Julian's Raspberry Pi fleet ("weyland").

These Instructions are a hand-pasted copy of a canonical block kept in
the docs (weyland/README.md). The docs are authoritative — not this
copy. On landing, check the version stamp above against the canonical
block's stamp in README.md. If they differ, this paste is stale: stop,
tell Julian, and let him decide how to reconcile (usually re-paste)
before doing other work.

FIRST, unconditionally — whether or not this chat is about a specific
Pi: all documentation is governed by the Doc Maintenance Manual at
weyland/docs/DOC-MAINTENANCE.md, the single source for which docs
exist, which are canonical vs copied, and what to update when. Before
changing ANY doc, fetch and read it in full:

  https://raw.githubusercontent.com/j-burton/weyland/main/docs/DOC-MAINTENANCE.md

You do not get to invent doc rules. Per-Pi docs describe one box and
are cheaply fixed. Fleet-standard / duplicated docs (this block, the
README, the fleet registries) must stay identical everywhere: edit the
canonical copy, then propagate the SAME change to every copy. Fleet-
wide drift is unacceptable. Close every doc task by reproducing the
manual's Definition of Done (§5) with the real files and commits.

This project's connector talks to one specific Pi. When the task
concerns that Pi, ask Julian which Pi it is, then read in order from
/opt/<pi-name>-pi/: README.md, IDENTITY.md, CURRENT_STATE.md,
MODULES.md. The README carries Julian's communication rules and how to
drive the Pi — follow them.

When you finish a task, fire Pushcut to Julian's phone.
```

The same block goes in every project. It's a hand-pasted copy of the
canonical above, so when you change it here, bump the version stamp and
re-paste it into the live projects — a stale paste flags itself via the
stamp. See docs/DOC-MAINTENANCE.md §9.

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
- `connector/` — the Python MCP service that runs on each Pi
  (`connector/README.md` documents its verbs, config vars, and auth).
- `connector/scripts/` — the wake system (`cc-notify` hook +
  `cc-tmux-watcher` daemon, `install-wake.sh` installer).
- `connector/systemd/` — systemd unit templates.
- `templates/` — files seeded into each per-Pi repo at bootstrap.
- `docs/` — design rationale (`DESIGN.md`), operating notes
  (`OPERATING.md`), the new-Pi walkthrough (`NEW_PI.md`), the PC wake-listener
  setup (`PC_WAKE.md`) and its AutoHotkey script (`pc-wake-listener.ahk`).

## When something breaks

The bootstrap is re-runnable. Run the curl command again — completed
phases are skipped. If it's truly stuck, reflash the SD card and start
over. The recovery model for minions is reflash, not repair.

## Editing weyland

After the first minion is up, weyland edits itself. Open a Claude
project pointed at any minion's repo, ask for a bootstrap change, and
that minion's CC will clone weyland, make the change, and push.
Future minions get the improvement automatically.
