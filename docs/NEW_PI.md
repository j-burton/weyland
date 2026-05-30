# Adding a new minion

The whole flow, from "I have a fresh Pi" to "I'm talking to it via
Claude."

## Prerequisites

- A Raspberry Pi running fresh Raspberry Pi OS (or another
  Debian-family Linux).
- Network connected (wired or wifi configured during imaging).
- An SSH connection from your laptop into the Pi.

## The one-liner

Paste this into the Pi's SSH session:

```bash
curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh | bash
```

## What happens next

The bootstrap script runs through 8 phases. The interactive bits:

1. **Phase 1 (identity)**: It asks "What should this Pi be called?"
   Pick a short lowercase name like `coffee` or `unifi`. It also
   asks for the domain — default is `<name>.julianburton.com`,
   accept it.

2. **Phase 3 (GitHub)**: A URL appears. Open it in any browser
   you're logged into GitHub on. Approve.

3. **Phase 5 (Cloudflare)**: Another URL appears. Open it in any
   browser logged into Cloudflare. Select the `julianburton.com`
   zone. Approve.

4. **Phase 6 (Anthropic)**: Another URL appears. Open it, sign in
   with Google (or whatever you normally use for Anthropic),
   approve.

Three browser dances total. Each takes ~10 seconds.

The rest of the bootstrap (system packages, building the connector,
setting up Cloudflare tunnel, installing the wake system) is
automatic. Total time: maybe 5 minutes.

## The summary

At the end, the script prints something like:

```
  Pi name:    coffee
  Domain:     coffee.julianburton.com
  MCP URL:    https://coffee.julianburton.com/mcp
  Repo:       https://github.com/j-burton/coffee-pi
  
  --- ADD THIS CONNECTOR TO CLAUDE DESKTOP ---
  
    Name:    coffee
    URL:     https://coffee.julianburton.com/mcp
    Bearer:  <random token>
```

Copy those three lines (Name, URL, Bearer).

## Adding to Claude Desktop

1. Open Claude Desktop.
2. Settings → Connectors → Add custom connector.
3. Paste the URL and Bearer token.
4. Save.

That connector is now available on web, desktop, and mobile Claude.

## The project

1. In Claude Desktop, create a new project named after the Pi.
2. Add the per-Pi GitHub repo (`j-burton/<pi-name>-pi`) as a
   source.
3. Open a new chat in the project.
4. Say "hi" — fresh chat-Claude will read the README and orient.

You're done. Talk to Claude about what you want the Pi to do.

## If something goes wrong

The bootstrap is designed to be re-runnable. Run it again. It will
skip steps that already succeeded and pick up where it stopped. If
it's truly stuck, reflash the SD card and start over.
