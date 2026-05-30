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
- **The wake system** — `cc-notify` hook + `cc-tmux-watcher` daemon
  that fires Pushcut notifications to Julian's phone when CC stalls
  or finishes a task.

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

## Why Pushcut-only wake (no PC/AHK)

Atlas's wake system pings Julian's PC, which types a HAL message into
his Claude Desktop chat input. That works for Atlas because it's one
Pi he's actively collaborating with at his desk.

For a fleet of distributed Pis (some at his house, some at his
mother's, some travelling with him), the PC path doesn't generalise.
The minion sitting in his mother's house can't reach his PC over the
tailnet.

So minions skip the PC entirely. Every wake fires Pushcut directly to
his phone. Simpler, universal, no tailnet dependency.
