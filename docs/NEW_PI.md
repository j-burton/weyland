# Adding a new minion

The whole flow, from "I have a fresh Pi" to "I'm talking to it via Claude."

## Prerequisites

- A Raspberry Pi running fresh Raspberry Pi OS (or another Debian-family
  Linux), networked, with an SSH session into it.
- **A GitHub account** (Julian's). That's it — you sign in via the browser
  during the bootstrap; **no tokens to type or remember.** The weyland PAT is
  fetched automatically from a private gist on your account (see below).

## The one-liner

Paste this into the Pi's SSH session:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh)
```

### One-time, first Pi ever: the PAT gist

The bootstrap fetches the permanent `WEYLAND_PAT` from a **private gist** on your
GitHub account — so you never type it. Set this up **once**:

- Create a **secret** gist at <https://gist.github.com/> with a file named
  **`weyland-pat`** whose content is a fine-grained PAT for `j-burton/weyland`
  **and** `j-burton/weyland-secrets` (Contents: read & write, no expiry).

Every bootstrap after that — on this and all future Pis — fetches it
automatically via your GitHub sign-in. If the gist doesn't exist yet, the
bootstrap prints exactly these instructions and continues; create it and re-run.

## Tip: let a Claude find the Pi for you — no IP hunting

If you have a Claude with network/terminal access on your PC (e.g. a Claude
Code session), you don't need to discover the new Pi's address by hand. Ask it
to **scan the LAN for the freshly-imaged Pi** (an `arp`/`nmap` sweep for a new
Raspberry Pi MAC or the `raspberrypi` hostname) and hand back the **complete**
command — SSH in *and* kick off the bootstrap — as a single PowerShell line you
just paste:

```powershell
ssh admin@<found-ip> "bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh)"
```

Scan → one line → paste → open the wizard URL it prints. No manual IP hunting.

## What happens next

The bootstrap runs **12 phases** — preflight, identity, packages, tailscale,
github_auth, per-Pi repo, tunnel, claude_code, connector, vault, selfdoc,
summary — with a pinned checklist tracking progress. It's re-runnable:
completed phases are skipped on a re-run.

You're hands-on in just two spots: the **identity form in the wizard** and the
four browser dances. Everything else is automatic — and all of it happens in
the browser. After you open the wizard URL you never need the SSH window again.

### Identity (answered in the wizard, not the terminal)

The wizard's first screen is a form. Fill it in the browser — the bootstrap
pauses and waits for you to submit it (it is NOT asked in the SSH window):

- **Minion name** — a short lowercase name like `inkypi` or `coffee` (letters,
  digits, hyphens; 2–32 chars).
- **Domain** — e.g. `julianburton.com` (the MCP endpoint becomes
  `<name>.<domain>`).
- **PC wake hostname** *(optional)* — your PC's Tailscale (MagicDNS) name,
  e.g. `ju-laptop.tail875649.ts.net`; blank skips the PC wake channel
  (Pushcut-only).
- **PC wake token** *(optional)* — the shared `X-Wake-Token` the PC listener
  expects.

(If the wizard can't be reached, the bootstrap falls back to terminal prompts.)

### Four browser dances

Each prints a URL — open it in a browser already logged into that service,
approve, done (~10 seconds each):

1. **Tailscale** (phase 4) — joins the Pi to the tailnet.
2. **GitHub** (phase 5) — to create and push the per-Pi repo.
3. **Cloudflare** (phase 7) — select the `julianburton.com` zone for the
   tunnel.
4. **Anthropic / Claude Code** (phase 8) — sign in so CC can run on the Pi.

### The automatic phases

The rest run on their own: system packages; the per-Pi repo (created **and**
seeded with templates); the Cloudflare tunnel; the MCP connector; the
**vault** (fetches fleet secrets like the Pushcut webhook from the private
`j-burton/weyland-secrets` repo using the PAT — non-fatal if the vault is
unreachable); and **self-documentation** (CC is handed a task to document the
Pi's hardware, software, and purpose into its repo automatically). Total time:
~5 minutes.

## The summary

At the end the script prints a block with everything you need:

```
  Pi name:    coffee
  Domain:     coffee.julianburton.com
  MCP URL:    https://coffee.julianburton.com/mcp
  Repo:       https://github.com/j-burton/coffee-pi
  SSH (over Tailscale, from anywhere):
              ssh admin@coffee.tail<tailnet>.ts.net

  --- ADD THIS CONNECTOR TO CLAUDE DESKTOP ---
    Name:            coffee
    URL:             https://coffee.julianburton.com/mcp
    OAuth Client ID: weyland-mcp-claude-ai
    Client Secret:   (leave blank — public client, PKCE)

  --- ON FIRST CONNECT ---
    Open the consent URL and paste the bearer token there ONE time:
      https://coffee.julianburton.com/weyland-consent
    Fallback (same network only):
      http://<local-ip>:5002/weyland-consent
    Bearer token:  <random token>   ← shown only once
```

## Adding to Claude Desktop

The connector uses **OAuth 2.1**, so the bearer isn't pasted straight into
Claude Desktop — it goes through a one-time consent page:

1. Claude Desktop → Settings → Connectors → Add custom connector.
2. Enter the **URL** and **OAuth Client ID** (`weyland-mcp-claude-ai`); leave
   the Client Secret blank.
3. On first connect, Claude Desktop redirects to the Pi's **consent page**.
   Open the consent URL from the summary and **paste the bearer token there
   once**. If the tunnel URL won't load, use the local-IP fallback (same
   network), or put your laptop on a phone hotspot and use the tunnel URL.

That connector is then available on web, desktop, and mobile Claude.

## Reaching the Pi directly

After bootstrap the Pi is on Tailscale, so it's reachable **from anywhere** —
not just the local network:

```bash
ssh admin@<pi-name>.tail<tailnet>.ts.net
```

## The project

1. In Claude Desktop, create a project named after the Pi.
2. Add the per-Pi GitHub repo (`j-burton/<pi-name>-pi`) as a source — it's
   already seeded **and** self-documented by the bootstrap.
3. Open a new chat and say "hi" — fresh chat-Claude reads the README and
   orients itself.

You're done. Talk to Claude about what you want the Pi to do.

## If something goes wrong

The bootstrap is re-runnable — run the one-liner again and it picks up where it
stopped, skipping completed phases. If it's truly stuck, reflash the SD card
and start over. The recovery model for minions is reflash, not repair.
