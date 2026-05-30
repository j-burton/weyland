# IDENTITY

This file is automatically populated by the weyland bootstrap when the
Pi is first set up. The bootstrap writes the Pi's facts here:

- PI_NAME (the short name, e.g. `coffee`, `unifi`)
- DOMAIN (the public DNS name, e.g. `coffee.julianburton.com`)
- MCP URL (`https://<DOMAIN>/mcp`)
- Hostname (as the OS reports it)
- OS (Debian release / Raspberry Pi OS version)
- Created (UTC timestamp of bootstrap)

If you're reading a placeholder version of this file, the bootstrap
hasn't been run on this Pi yet, or this file was reset somehow. The
bootstrap will overwrite it on the next run; or you can fill it in by
hand if you're improvising.

Edit this file freely as the Pi grows — add new roles, fleet position,
hardware details. The bootstrap only sets the initial values; it does
not regenerate the file on re-runs.
