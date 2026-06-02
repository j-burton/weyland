# <pi> — rebuild runbook
Goal: a freshly-flashed Pi back to its live state. Walk MANIFEST.md top-to-bottom, applying each row by its pattern.

## 0. Base (weyland)
Flash Raspberry Pi OS (see MANIFEST for version), then bind via weyland. To re-run:
- wipe:      `curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/reset.sh | sudo bash`
- bootstrap: `curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh | sudo bash`

## 1. Reinstall (P1)        — `apt install` / `pip install` / clone the listed sources
## 2. Reapply config (P2)   — copy files from provisioning/ back to their real paths
## 3. Restore state (P3)    — reinstall app, restore its native backup, reconcile against reference/ inventory, flag gaps
## 4. Re-auth (P4)          — restore secrets from the vault (see secrets.md), log in by hand

## Known manual residue
- <one-time logins, Matter re-pairing, anything that needs a human>
