# Weyland design

## Why weyland exists

Julian runs a small fleet of Raspberry Pis for various jobs. Setting
each one up by hand — installing software, configuring services,
plugging it into Cloudflare so chat-Claude can reach it — is tedious
and error-prone. Weyland is the one-paste-and-walk-away bootstrap
that turns a fresh Pi into a usable minion.

## Architecture

```
                                       Each Pi independent.
                                       No central brain.
                                       
                ┌─────────────┐
                │  Julian's   │
                │  Claude     │
                │  account    │
                └──────┬──────┘
                       │
       ┌───────────────┼───────────────┐
       │               │               │
   coffee.j           unifi.j        mum.j
   .com/mcp         .com/mcp       .com/mcp
       │               │               │
  ┌────▼────┐     ┌────▼────┐     ┌────▼────┐
  │ Pi 1    │     │ Pi 2    │     │ Pi 3    │
  │ CC + connector each. No cross-Pi communication.
  └─────────┘     └─────────┘     └─────────┘
```

Each Pi runs:

- **Claude Code** — signed into Julian's Anthropic account, sitting
  in a tmux session named after the Pi.
- **The weyland MCP connector** — a Python service on
  `localhost:5002` that gives chat-Claude tools to read/write files,
  drive the tmux session, run shell commands, manage systemd.
- **A Cloudflare tunnel** — exposes the local MCP service at
  `https://<pi-name>.julianburton.com/mcp` so chat-Claude (running in
  Anthropic's cloud) can reach it.
- **The wake system** — `cc-notify` hook + `cc-tmux-watcher` daemon.
  They wake chat-Claude by popping the Claude window on Julian's PC (over
  Tailscale) when CC finishes or stalls, escalating to a Pushcut on his
  phone only as a last resort. See "Wake model" below.
- **The vault** — fleet-wide secrets (the Pushcut webhook, etc.) are fetched
  at bootstrap from the private `j-burton/weyland-secrets` repo via the PAT,
  so no secret lives in the public weyland repo.

Julian adds one custom connector entry to Claude Desktop per Pi.
Anthropic's account-sync makes the connector available on all his
Claude surfaces (web, desktop, mobile).

## Why default-allow

The minions are playthings, not production. If a minion bricks itself,
the recovery is to reflash the SD card. Building Atlas-grade safety
into the connector would add friction without buying real security —
chat-Claude is the security boundary, and Julian decides what to tell
it.

The connector denylist is tiny: credential files only
(`/etc/shadow`, `~/.ssh/`, the per-Pi repo's `.git/config`).
Everything else is open, including full passwordless sudo. Minions
need to install software, edit `/etc/`, create systemd units, plug
in USB devices — all of which need root.

## Why per-Pi tmux session names

Could have been a fixed name on every Pi. Julian asked for per-Pi
names ("coffee", "unifi", etc.) so when he SSHs in he can see he's
in the right session. Minor cost (the connector reads `WEYLAND_PI_NAME`
and uses it as the default), real value.

## Wake model — PC first, Pushcut last (over Tailscale)

An earlier plan had minions skip the PC entirely and Pushcut Julian on
every wake, on the assumption that a distributed minion couldn't reach his
PC. Tailscale removes that constraint: the PC is reachable from any minion
by its Tailscale (MagicDNS) hostname, wherever either of them is. So the
wake system mirrors Atlas's PC channel, with phone paging as the backstop:

- `cc-notify` fires a **PC-only** ping (`[HAL 9000 STANDING BY]`) when CC
  goes idle at turn-end. It never Pushcuts Julian — chat-Claude reads the
  pane and decides whether he needs paging.
- `cc-tmux-watcher` runs a **5-shot escalation ladder** when CC sits idle
  (gaps 10s → 60s → 60s → 480s → 600s). Shots 1–4 are PC-only (they wake
  chat-Claude); only the final shot also Pushcuts Julian's phone — by then
  chat-Claude is unreachable and a human must step in.

The PC end is a small AutoHotkey listener (`docs/cc-wake-v2-FRESH.ahk`) on
port 7777, gated by a shared `X-Wake-Token`. The minion's PC hostname and
token are set during the bootstrap identity phase; leave them blank to fall
back to Pushcut-only.
