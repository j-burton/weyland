# PC wake listener setup

The wake system's PC channel pops the Claude window on Julian's PC and types
an escalating nudge into it when a minion's CC finishes or stalls. The PC side
is a small AutoHotkey v2 script, `docs/pc-wake-listener.ahk` — the one piece of
the fleet that lives on the PC, not on a minion. Set it up once per PC.

## What it does

- Listens on TCP **port 7777** for HTTP POSTs from minions
  (`cc-notify` and `cc-tmux-watcher` POST to `http://<this-pc>:7777`).
- Authenticates each request by an **`X-Wake-Token`** header — mismatches get
  a 401 and are ignored.
- On a valid request it activates the window titled **"Claude"** and types that
  shot's message, then Enter, so chat-Claude (in the focused Claude window)
  sees the nudge. The shot number comes from the POST body (`{"shot": N}`);
  messages escalate (shot 1 `[HAL 9000 STANDING BY]` … shot 5 "CC may be
  stuck").
- Writes a small `cc-wake.log` next to the script.

## One-time setup

1. **Install AutoHotkey v2** (<https://www.autohotkey.com/>) — v2, not v1.
2. **Copy** `docs/pc-wake-listener.ahk` to the PC (e.g.
   `%USERPROFILE%\weyland\pc-wake-listener.ahk`).
3. **Set the token.** Edit the script and replace
   `WAKE_TOKEN := "REPLACE_WITH_WAKE_TOKEN"` with the wake token you hand the
   minions. This is the **same** `X-Wake-Token` you type at the bootstrap
   identity prompt ("PC wake token"). All minions that wake this PC share this
   one token. Never commit the real value.
4. **Check the window title.** `WINDOW_TITLE := "Claude"` must match your
   Claude window's title — adjust if yours differs.
5. **Run it.** Double-click the script; a tray tip ("CC Wake — Listening on
   port 7777") confirms it's up. To start it at login, drop a shortcut into the
   Startup folder (`Win+R` → `shell:startup`).
6. **Allow port 7777.** If Windows Firewall prompts on first run, allow it on
   the Private/Tailscale network. The script binds all interfaces, so once the
   firewall permits inbound 7777 it's reachable over Tailscale.

## How the minion reaches it

At bootstrap (identity phase) each minion is given:

- **PC Tailscale hostname** → the minion POSTs to
  `http://<pc-tailscale-name>:7777`. Use the PC's Tailscale MagicDNS name so it
  works from anywhere, not just the LAN.
- **PC wake token** → must equal the script's `WAKE_TOKEN`.

These land in `/etc/weyland/wake.env` on the minion (`PC_WAKE_URL`,
`WAKE_TOKEN`). Leave them blank at bootstrap to skip the PC channel entirely
(Pushcut-only wake).

## Verifying

With the listener running and the minion configured, from the minion:

```bash
. /etc/weyland/wake.env
curl -s -X POST "$PC_WAKE_URL" -H "X-Wake-Token: $WAKE_TOKEN" \
  -d '{"shot":1,"text":"test"}'        # expect: OK
```

The Claude window on the PC should come forward and `[HAL 9000 STANDING BY]`
get typed in.

## Troubleshooting

- **401 Unauthorized** — token mismatch between the script's `WAKE_TOKEN` and
  the minion's `/etc/weyland/wake.env`.
- **Nothing gets typed** — no window matches `WINDOW_TITLE`; check
  `cc-wake.log` next to the script for an "activation failed" line.
- **Connection refused / timeout** — script not running, or Windows Firewall
  is blocking inbound 7777 on the Tailscale interface.
