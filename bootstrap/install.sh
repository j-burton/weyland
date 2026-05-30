#!/usr/bin/env bash
# weyland — fresh-Pi bootstrap installer
#
# Pasted into a fresh Pi's SSH session as the one-liner:
#   curl -fsSL https://raw.githubusercontent.com/j-burton/weyland/main/bootstrap/install.sh | bash
#
# Phase structure: each phase is independently runnable / idempotent.
# A re-run after a partial failure should pick up where it stopped.

set -euo pipefail

# ----------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------
WEYLAND_REPO="https://github.com/j-burton/weyland.git"
OWNER="j-burton"
DEFAULT_DOMAIN_ROOT="julianburton.com"
STATE_DIR="/var/lib/weyland"   # per-Pi state (PI_NAME, DOMAIN, etc.)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
log()  { echo -e "\n\033[1;36m[weyland]\033[0m $*"; }
warn() { echo -e "\n\033[1;33m[weyland warn]\033[0m $*" >&2; }
die()  { echo -e "\n\033[1;31m[weyland fail]\033[0m $*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

# Save a key=value pair into STATE_DIR/env (sudo-protected).
save_state() {
  local key="$1" val="$2"
  sudo mkdir -p "$STATE_DIR"
  sudo touch "$STATE_DIR/env"
  sudo sed -i "/^${key}=/d" "$STATE_DIR/env" || true
  echo "${key}=${val}" | sudo tee -a "$STATE_DIR/env" >/dev/null
}

load_state() {
  if [ -f "$STATE_DIR/env" ]; then
    # shellcheck disable=SC1091
    . "$STATE_DIR/env"
  fi
}

# ----------------------------------------------------------------------
# Phase 0 — Pre-flight
# ----------------------------------------------------------------------
phase_preflight() {
  log "Phase 0: pre-flight checks"

  # TODO: confirm Debian-family OS (Raspberry Pi OS / Ubuntu / Debian).
  # TODO: confirm sudo works without prompting more than once.
  # TODO: confirm we have network egress to github.com + cloudflare.com.
  # TODO: confirm we're NOT re-running on a Pi that's already bootstrapped
  #       (check $STATE_DIR/env — if PI_NAME is set, prompt before continuing).

  warn "phase_preflight is a stub"
}

# ----------------------------------------------------------------------
# Phase 1 — Identity (who is this Pi?)
# ----------------------------------------------------------------------
phase_identity() {
  log "Phase 1: identity"

  # TODO: prompt for PI_NAME (lowercase, alnum + hyphens).
  # TODO: prompt for DOMAIN (default: <PI_NAME>.$DEFAULT_DOMAIN_ROOT).
  # TODO: save_state PI_NAME, DOMAIN.

  warn "phase_identity is a stub"
}

# ----------------------------------------------------------------------
# Phase 2 — System packages
# ----------------------------------------------------------------------
phase_packages() {
  log "Phase 2: install system packages"

  # TODO: apt update.
  # TODO: apt install -y git curl tmux python3 python3-venv ca-certificates.
  # TODO: install gh (GitHub CLI) from official apt repo.
  # TODO: install cloudflared from official cloudflare apt repo.

  warn "phase_packages is a stub"
}

# ----------------------------------------------------------------------
# Phase 3 — GitHub auth
# ----------------------------------------------------------------------
phase_github_auth() {
  log "Phase 3: GitHub auth (device flow)"

  # TODO: gh auth status — skip if already authed.
  # TODO: gh auth login --git-protocol https --hostname github.com --web.
  # TODO: confirm the authed user is $OWNER.

  warn "phase_github_auth is a stub"
}

# ----------------------------------------------------------------------
# Phase 4 — Per-Pi repo
# ----------------------------------------------------------------------
phase_per_pi_repo() {
  log "Phase 4: create per-Pi repo"

  # TODO: gh repo create $OWNER/${PI_NAME}-pi --private --description "...".
  # TODO: clone to /opt/${PI_NAME}-pi.
  # TODO: copy templates/* from this weyland checkout into the new repo.
  # TODO: fill in IDENTITY.md with PI_NAME, DOMAIN, hostname, OS, MCP URL.
  # TODO: commit + push initial state.

  warn "phase_per_pi_repo is a stub"
}

# ----------------------------------------------------------------------
# Phase 5 — Cloudflare tunnel
# ----------------------------------------------------------------------
phase_tunnel() {
  log "Phase 5: Cloudflare tunnel"

  # TODO: cloudflared tunnel login (browser dance).
  # TODO: cloudflared tunnel create $PI_NAME.
  # TODO: write config.yml pointing $DOMAIN → http://localhost:5002 (MCP).
  # TODO: cloudflared tunnel route dns $PI_NAME $DOMAIN.
  # TODO: install cloudflared as a systemd service.

  warn "phase_tunnel is a stub"
}

# ----------------------------------------------------------------------
# Phase 6 — Claude Code
# ----------------------------------------------------------------------
phase_claude_code() {
  log "Phase 6: install Claude Code"

  # TODO: install CC via the official installer.
  # TODO: arrange for CC auth (open question — see docs/DESIGN.md when written).
  # TODO: start a long-lived tmux session named 'mcp' running CC.
  # TODO: arrange for the tmux session to survive reboot.

  warn "phase_claude_code is a stub"
}

# ----------------------------------------------------------------------
# Phase 7 — Weyland MCP connector
# ----------------------------------------------------------------------
phase_connector() {
  log "Phase 7: install weyland MCP connector"

  # TODO: copy connector/ source into /opt/weyland-mcp/.
  # TODO: create a Python venv, install dependencies.
  # TODO: render the systemd unit file with PI_NAME, DOMAIN.
  # TODO: install + enable + start the service.
  # TODO: smoke test — curl localhost:5002/whoami.

  warn "phase_connector is a stub"
}

# ----------------------------------------------------------------------
# Phase 8 — Final summary
# ----------------------------------------------------------------------
phase_summary() {
  load_state
  log "Bootstrap complete."

  cat <<EOF

  Pi name:    ${PI_NAME:-?}
  Domain:     ${DOMAIN:-?}
  MCP URL:    https://${DOMAIN:-?}/mcp
  Repo:       https://github.com/${OWNER}/${PI_NAME:-?}-pi

  Next steps (manual, one-time):
    1. Open Claude Desktop.
    2. Settings → Connectors → Add custom connector.
    3. URL: https://${DOMAIN:-?}/mcp
    4. Create a Claude Project pointed at the per-Pi repo above.

  From there, talk to Claude.

EOF
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
main() {
  load_state
  phase_preflight
  phase_identity
  phase_packages
  phase_github_auth
  phase_per_pi_repo
  phase_tunnel
  phase_claude_code
  phase_connector
  phase_summary
}

main "$@"
