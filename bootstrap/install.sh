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

  # OS family check (Debian-family only).
  if [ ! -f /etc/os-release ]; then
    die "cannot detect OS — /etc/os-release missing"
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID_LIKE:-$ID}" in
    *debian*) : ;;
    *) die "weyland targets Debian-family only; saw ID=${ID:-?} ID_LIKE=${ID_LIKE:-?}" ;;
  esac

  # sudo must be available and password-less (or already cached).
  require_cmd sudo
  if ! sudo -n true 2>/dev/null; then
    log "sudo will prompt for your password once now; future calls cache."
    sudo true || die "sudo is required for the install"
  fi

  # Network egress sanity — github and cloudflare are the must-haves.
  for host in github.com api.cloudflare.com; do
    if ! curl -fsS --max-time 5 -o /dev/null "https://${host}"; then
      die "no network egress to ${host}"
    fi
  done

  # Re-run guard: if PI_NAME is already set in state, ask before continuing.
  if [ -f "$STATE_DIR/env" ]; then
    load_state
    if [ -n "${PI_NAME:-}" ]; then
      warn "This Pi is already named '${PI_NAME}'."
      read -r -p "Continue anyway (re-run)? [y/N] " ans
      case "${ans,,}" in
        y|yes) : ;;
        *) die "aborted by user" ;;
      esac
    fi
  fi

  log "pre-flight OK"
}

# ----------------------------------------------------------------------
# Phase 1 — Identity (who is this Pi?)
# ----------------------------------------------------------------------
phase_identity() {
  log "Phase 1: identity"

  # Skip if already set (re-run case).
  load_state
  if [ -n "${PI_NAME:-}" ] && [ -n "${DOMAIN:-}" ]; then
    log "identity already set: PI_NAME=${PI_NAME} DOMAIN=${DOMAIN}"
    return 0
  fi

  # PI_NAME: lowercase, alnum + hyphens, 2-32 chars, no leading/trailing hyphen.
  while :; do
    read -r -p "What should this Pi be called? " name
    if [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
      break
    fi
    warn "invalid name. Use lowercase letters, digits, hyphens. 2-32 chars. No leading/trailing hyphen."
  done
  PI_NAME="$name"
  save_state PI_NAME "$PI_NAME"

  # DOMAIN: default to <PI_NAME>.$DEFAULT_DOMAIN_ROOT, accept override.
  local default_domain="${PI_NAME}.${DEFAULT_DOMAIN_ROOT}"
  read -r -p "Domain for this Pi's MCP endpoint? [${default_domain}] " dom
  DOMAIN="${dom:-$default_domain}"
  save_state DOMAIN "$DOMAIN"

  log "identity set: PI_NAME=${PI_NAME} DOMAIN=${DOMAIN}"
}

# ----------------------------------------------------------------------
# Phase 2 — System packages
# ----------------------------------------------------------------------
phase_packages() {
  log "Phase 2: install system packages"

  # Base packages.
  sudo apt-get update -qq
  sudo apt-get install -y -qq \
    git curl tmux ca-certificates gnupg lsb-release \
    python3 python3-venv python3-pip

  # GitHub CLI (gh) — official apt repo.
  if ! command -v gh >/dev/null 2>&1; then
    log "installing gh"
    local keyring="/usr/share/keyrings/githubcli-archive-keyring.gpg"
    sudo install -m 0755 -d /usr/share/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | sudo gpg --dearmor --yes -o "$keyring"
    sudo chmod a+r "$keyring"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=${keyring}] https://cli.github.com/packages stable main" \
      | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq gh
  else
    log "gh already installed"
  fi

  # cloudflared — official Cloudflare apt repo.
  if ! command -v cloudflared >/dev/null 2>&1; then
    log "installing cloudflared"
    local cf_keyring="/usr/share/keyrings/cloudflare-main.gpg"
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
      | sudo tee "$cf_keyring" >/dev/null
    echo "deb [signed-by=${cf_keyring}] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
      | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq cloudflared
  else
    log "cloudflared already installed"
  fi

  log "packages installed"
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
