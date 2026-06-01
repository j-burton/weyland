#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# weyland reset — strip a minion back to its pre-bootstrap state so the NEXT
# install runs truly fresh: every sign-in (Claude, GitHub, Tailscale,
# Cloudflare) happens again and the wizard starts from "name the minion".
#
# FOR DISPOSABLE / TEST PIS ONLY.
#   bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/reset.sh)
#
# What it does:  signs you out of Claude / GitHub / Tailscale / Cloudflare on
#   this Pi, and deletes weyland's state, the per-Pi repo, the CC auto-start
#   service, and the Cloudflare tunnel.
# What it leaves: the tools it downloaded (claude, gh, cloudflared, tmux) — they
#   are harmless to keep and save the re-run a long re-download. (For a truly
#   bare-metal start, re-flash the SD card instead.)
# ---------------------------------------------------------------------------
set -uo pipefail

PROTECTED="unifiviewer argos"                       # never wipe these
STATE_DIR="${WEYLAND_STATE_DIR:-/var/lib/weyland}"
HOST="$(hostname)"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
say(){ printf '  - %s\n' "$*"; }

# --- guardrails ------------------------------------------------------------
if [ -n "${TMUX:-}" ]; then
  red "You're inside tmux. Run this from a plain SSH terminal so it can clear"
  red "the on-board Claude session without killing your own. Aborting."
  exit 1
fi
for p in $PROTECTED; do
  if [ "$HOST" = "$p" ]; then
    red "Refusing to reset '$HOST' — it's on the protected list. Aborting."
    exit 1
  fi
done

# --- discover the minion name (for the repo / tunnel / tmux session) -------
PI_NAME=""
if [ -f "$STATE_DIR/state.json" ]; then
  PI_NAME="$(python3 -c "import json;print(json.load(open('$STATE_DIR/state.json')).get('pi_name','') or '')" 2>/dev/null || true)"
fi
if [ -z "$PI_NAME" ] && sudo test -f /etc/weyland/weyland.env 2>/dev/null; then
  PI_NAME="$(sudo sed -n 's/^WEYLAND_PI_NAME=//p' /etc/weyland/weyland.env 2>/dev/null | head -1)"
fi

# --- confirm ---------------------------------------------------------------
echo
red "This will RESET '$HOST' to a clean, pre-bootstrap state."
echo "It will:"
echo "  - sign out of Claude, GitHub, Tailscale, and Cloudflare on this Pi"
echo "  - delete weyland's state, the /opt/${PI_NAME:-<name>}-pi repo,"
echo "    the on-board Claude auto-start service, and the Cloudflare tunnel"
echo "  - (it leaves the installed tools in place)"
echo
printf "Type the hostname '%s' to confirm: " "$HOST"
read -r ans </dev/tty || true
if [ "$ans" != "$HOST" ]; then
  red "Didn't match — nothing was changed."
  exit 1
fi
echo
echo "-- resetting --"

# 1) on-board Claude: stop the auto-start service + kill its tmux session
sudo systemctl disable --now weyland-cc.service >/dev/null 2>&1 || true
sudo rm -f /etc/systemd/system/weyland-cc.service
tmux kill-server >/dev/null 2>&1 || true
say "stopped the on-board Claude (service + tmux)"

# 2) Cloudflare tunnel: stop service, delete the tunnel, remove creds/config
sudo systemctl disable --now cloudflared >/dev/null 2>&1 || true
if command -v cloudflared >/dev/null 2>&1 && [ -n "$PI_NAME" ]; then
  cloudflared tunnel delete -f "$PI_NAME" >/dev/null 2>&1 || true
fi
sudo rm -rf /etc/cloudflared "$HOME/.cloudflared"
sudo rm -f /etc/systemd/system/cloudflared.service
say "removed the Cloudflare tunnel + credentials"

# 3) sign out so the wizard signs in fresh
export PATH="$HOME/.local/bin:$PATH"
claude auth logout                   >/dev/null 2>&1 || true
gh auth logout --hostname github.com >/dev/null 2>&1 || true
sudo tailscale logout                >/dev/null 2>&1 || true
say "signed out of Claude, GitHub, Tailscale"

# 4) remove weyland state, env, and the per-Pi repo
sudo rm -rf "$STATE_DIR" /etc/weyland
[ -n "$PI_NAME" ] && sudo rm -rf "/opt/${PI_NAME}-pi"
sudo rm -rf /opt/*-pi 2>/dev/null || true
say "removed weyland state, env, and the per-Pi repo"

sudo systemctl daemon-reload >/dev/null 2>&1 || true
echo
echo "-- '$HOST' is back to a clean slate. Start fresh with: --"
echo "   bash <(curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh)"
