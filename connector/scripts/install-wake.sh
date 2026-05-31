#!/usr/bin/env bash
# install-wake.sh — install the weyland wake system on this Pi.
# Called from install.sh phase_claude_code.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_NAME="${1:-minion}"
PC_WAKE_URL="${2:-}"   # AHK listener URL, e.g. http://<pc>:7777 (blank = skip)
WAKE_TOKEN="${3:-}"    # X-Wake-Token shared with the PC AHK listener

# 1. Install scripts to /usr/local/bin.
sudo install -m 0755 "${SCRIPT_DIR}/cc-notify"        /usr/local/bin/cc-notify
sudo install -m 0755 "${SCRIPT_DIR}/cc-tmux-watcher"  /usr/local/bin/cc-tmux-watcher

# 2. Ensure /etc/weyland exists and has the wake-mode flag.
sudo mkdir -p /etc/weyland
if [ ! -f /etc/weyland/wake-mode ]; then
  echo on | sudo tee /etc/weyland/wake-mode >/dev/null
fi
# Always (re)assert mode: world-readable so the watcher can poll it.
sudo chmod 0644 /etc/weyland/wake-mode

# 2b. Runtime state dir + log file must be writable by the service user — the
#     watcher runs as ${USER}, not root. /var/lib/weyland is root-created by
#     the bootstrap and /var/log is root-owned, so without fixing ownership
#     here the watcher fails SILENTLY (can't write its state JSON or its log).
sudo mkdir -p /var/lib/weyland
sudo chown "${USER}:${USER}" /var/lib/weyland
sudo chmod 0755 /var/lib/weyland
sudo touch /var/log/weyland-wake.log
sudo chown "${USER}:${USER}" /var/log/weyland-wake.log
sudo chmod 0644 /var/log/weyland-wake.log

# 3. Pushcut env file. Reuses Atlas's webhook secret — same notification on
#    Julian's phone fires for all minions, distinguished by [pi_name] prefix
#    in the alert text. If /etc/weyland/pushcut.env already exists, leave it.
if [ ! -f /etc/weyland/pushcut.env ]; then
  cat <<'EOF' | sudo tee /etc/weyland/pushcut.env >/dev/null
# Pushcut webhook secret. Shared across all minions — they all fire the
# same CC_Needs_Julian notification, distinguished by the [pi_name]
# prefix in the alert text.
PUSHCUT_WEBHOOK_SECRET=Uz128efYNtFwdgoGYBTuz
EOF
fi
# Readable by the service user (cc-notify / watcher run as ${USER}, not root).
sudo chmod 0644 /etc/weyland/pushcut.env

# 3b. PC AHK wake channel config → /etc/weyland/wake.env. The wake scripts
#     POST here with an X-Wake-Token header so the AHK listener on Julian's
#     PC pops the Claude window. Values come from args $2/$3 (passed by the
#     bootstrap) or are prompted when this script is run standalone. These
#     are per-install secrets — NOT committed to the repo. Blank = skip
#     (Pushcut-only). If the file already exists, leave it.
if [ ! -f /etc/weyland/wake.env ]; then
  if [ -z "$PC_WAKE_URL" ] && [ -t 0 ]; then
    read -r -p "PC wake URL (AHK listener, e.g. http://<pc-host>:7777 — blank to skip): " PC_WAKE_URL
  fi
  if [ -n "$PC_WAKE_URL" ] && [ -z "$WAKE_TOKEN" ] && [ -t 0 ]; then
    read -r -p "PC wake token (X-Wake-Token): " WAKE_TOKEN
  fi
  {
    echo "# PC AHK wake channel. cc-notify / cc-tmux-watcher POST here with an"
    echo "# X-Wake-Token header so the AHK listener on Julian's PC pops the"
    echo "# Claude window. Per-install values — never commit the token."
    echo "PC_WAKE_URL=${PC_WAKE_URL}"
    echo "WAKE_TOKEN=${WAKE_TOKEN}"
  } | sudo tee /etc/weyland/wake.env >/dev/null
fi
# Readable by the service user (cc-notify / watcher run as ${USER}, not root).
sudo chmod 0644 /etc/weyland/wake.env

# 4. Register cc-notify as CC's Notification hook.
SETTINGS_DIR="${HOME}/.claude"
SETTINGS_FILE="${SETTINGS_DIR}/settings.json"
mkdir -p "$SETTINGS_DIR"
if [ ! -f "$SETTINGS_FILE" ]; then
  echo '{}' > "$SETTINGS_FILE"
fi
python3 - "$SETTINGS_FILE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
hooks = cfg.setdefault("hooks", {})
notif = hooks.setdefault("Notification", [])
target = {"hooks": [{"type": "command", "command": "/usr/local/bin/cc-notify"}]}
if target not in notif:
    notif.append(target)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY

# 5. Install + enable the watcher systemd unit.
TEMPLATE="${SCRIPT_DIR}/../systemd/weyland-watcher.service.template"
UNIT_PATH="/etc/systemd/system/weyland-watcher.service"
sudo sed -e "s|{{ PI_NAME }}|${PI_NAME}|g" \
         "$TEMPLATE" \
  | sudo tee "$UNIT_PATH" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable weyland-watcher.service
sudo systemctl restart weyland-watcher.service

echo "wake system installed: cc-notify hook + cc-tmux-watcher service"
