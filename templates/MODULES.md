# MODULES

Per-app/service inventory for this Pi. Each module gets its own
section.

## Format

For each module:

```
## <module name>

**What it does:** one sentence.
**Where it lives:** path on disk.
**How to start/stop:** systemd unit or command.
**Config:** path to config files.
**Logs:** path to log files.
**Last verified working:** date.
```

(empty until the Pi gains its first module — chat-Claude adds entries
as new software is installed)

Each module should also appear in `recreate/MANIFEST.md` with its rebuild pattern. MODULES.md is the human inventory; the manifest is the rebuild view.
