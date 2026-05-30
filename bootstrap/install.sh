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
  log "Phase 3: GitHub auth"

  # Skip if already authed for github.com as the right user.
  if gh auth status -h github.com >/dev/null 2>&1; then
    local current_user
    current_user="$(gh api user --jq .login 2>/dev/null || true)"
    if [ "$current_user" = "$OWNER" ]; then
      log "gh already authed as ${OWNER}"
      return 0
    fi
    warn "gh is authed as '${current_user:-?}', expected '${OWNER}'. Re-authing."
    gh auth logout -h github.com --hostname github.com || true
  fi

  cat <<EOF

  Next: gh will print a one-time code and open a URL.

  1. Open the URL in any browser (your phone, your laptop, anywhere
     you're already logged into GitHub as ${OWNER}).
  2. Paste the code.
  3. Approve.

  This grants this Pi access to your GitHub. The credential is stored
  by gh itself; nothing is written to a file you'd need to track.

EOF

  gh auth login \
    --hostname github.com \
    --git-protocol https \
    --web

  # Verify.
  local current_user
  current_user="$(gh api user --jq .login)" \
    || die "gh auth completed but 'gh api user' failed"
  if [ "$current_user" != "$OWNER" ]; then
    die "gh authed as '${current_user}', not '${OWNER}'. Re-run after fixing."
  fi
  log "gh authed as ${OWNER}"
}

# ----------------------------------------------------------------------
# Phase 4 — Per-Pi repo
# ----------------------------------------------------------------------
phase_per_pi_repo() {
  log "Phase 4: create per-Pi repo"

  load_state
  [ -n "${PI_NAME:-}" ] || die "PI_NAME not set; phase 1 must run first"
  [ -n "${DOMAIN:-}"  ] || die "DOMAIN not set; phase 1 must run first"

  local repo_slug="${OWNER}/${PI_NAME}-pi"
  local local_dir="/opt/${PI_NAME}-pi"

  # Create the repo on GitHub if it doesn't already exist.
  if gh repo view "$repo_slug" >/dev/null 2>&1; then
    log "repo ${repo_slug} already exists on GitHub"
  else
    log "creating ${repo_slug} on GitHub"
    gh repo create "$repo_slug" \
      --private \
      --description "Per-Pi state for ${PI_NAME} (created by weyland)" \
      --disable-wiki \
      --disable-issues
  fi

  # Clone or pull into /opt/<pi-name>-pi.
  sudo mkdir -p "$(dirname "$local_dir")"
  if [ ! -d "$local_dir/.git" ]; then
    log "cloning ${repo_slug} to ${local_dir}"
    sudo chown "$(id -u):$(id -g)" "$(dirname "$local_dir")" || true
    gh repo clone "$repo_slug" "$local_dir"
  else
    log "${local_dir} already cloned; pulling latest"
    git -C "$local_dir" pull --ff-only || warn "pull failed (maybe empty repo); continuing"
  fi

  # Seed from weyland templates if the per-Pi repo is empty.
  local weyland_dir
  weyland_dir="$(cd "$(dirname "$0")/.." && pwd)"  # bootstrap/ -> repo root

  # Decide if we need to seed: empty repo means no real files beyond .git.
  local file_count
  file_count="$(find "$local_dir" -mindepth 1 -maxdepth 1 -not -name .git | wc -l)"
  if [ "$file_count" -eq 0 ]; then
    log "seeding ${local_dir} from weyland templates"
    cp -r "${weyland_dir}/templates/." "$local_dir/"
    cp "${weyland_dir}/templates/handoffs/.gitkeep" "$local_dir/handoffs/" 2>/dev/null || true

    # Render IDENTITY.md with this Pi's facts.
    local identity_path="$local_dir/IDENTITY.md"
    if [ -f "$identity_path" ]; then
      {
        echo "# IDENTITY — ${PI_NAME}"
        echo
        echo "- **PI_NAME:** ${PI_NAME}"
        echo "- **DOMAIN:** ${DOMAIN}"
        echo "- **MCP URL:** https://${DOMAIN}/mcp"
        echo "- **Hostname:** $(hostname)"
        echo "- **OS:** $(. /etc/os-release && echo "${PRETTY_NAME:-${ID}}")"
        echo "- **Created:** $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo
        echo "_Generated by weyland bootstrap. Edit freely; not regenerated on re-runs._"
      } > "$identity_path"
    fi

    # Initial commit + push.
    git -C "$local_dir" add -A
    git -C "$local_dir" \
      -c user.name="${USER}" \
      -c user.email="${USER}@${PI_NAME}.local" \
      commit -m "chore: initial per-Pi state seeded by weyland" \
      || warn "nothing to commit (empty seed?)"
    git -C "$local_dir" push -u origin "$(git -C "$local_dir" rev-parse --abbrev-ref HEAD)" \
      || warn "push failed; check connectivity"
  else
    log "${local_dir} not empty; leaving contents as-is"
  fi

  save_state PI_REPO "$repo_slug"
  save_state PI_DIR  "$local_dir"
  log "per-Pi repo ready at ${local_dir} (${repo_slug})"
}

# ----------------------------------------------------------------------
# Phase 5 — Cloudflare tunnel
# ----------------------------------------------------------------------
phase_tunnel() {
  log "Phase 5: Cloudflare tunnel"

  load_state
  [ -n "${PI_NAME:-}" ] || die "PI_NAME not set; phase 1 must run first"
  [ -n "${DOMAIN:-}"  ] || die "DOMAIN not set; phase 1 must run first"

  local tunnel_name="$PI_NAME"
  local tunnel_dir="/etc/cloudflared"
  local config_file="${tunnel_dir}/config.yml"

  sudo mkdir -p "$tunnel_dir"

  # Step 1: cloudflared login (browser dance).
  if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
    cat <<EOF

  cloudflared needs to authenticate with Cloudflare.

  A URL will appear below. Open it in any browser, log in to Cloudflare,
  and select the zone for ${DEFAULT_DOMAIN_ROOT}.

EOF
    cloudflared tunnel login
  else
    log "cloudflared already authenticated (cert.pem present)"
  fi

  # Step 2: create the tunnel if it doesn't exist.
  local tunnel_id
  tunnel_id="$(cloudflared tunnel list -o json 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(next((t['id'] for t in d if t['name']=='${tunnel_name}'),''))" \
    2>/dev/null || true)"
  if [ -z "$tunnel_id" ]; then
    log "creating tunnel '${tunnel_name}'"
    cloudflared tunnel create "$tunnel_name"
    tunnel_id="$(cloudflared tunnel list -o json \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(t['id'] for t in d if t['name']=='${tunnel_name}'))")"
  else
    log "tunnel '${tunnel_name}' already exists (id ${tunnel_id})"
  fi
  save_state TUNNEL_ID "$tunnel_id"

  # Step 3: write config.yml routing $DOMAIN -> localhost:5002 (MCP).
  local creds_file="$HOME/.cloudflared/${tunnel_id}.json"
  if [ ! -f "$creds_file" ]; then
    die "tunnel credentials file missing at ${creds_file}"
  fi
  sudo cp "$creds_file" "${tunnel_dir}/${tunnel_id}.json"
  sudo chmod 0640 "${tunnel_dir}/${tunnel_id}.json"

  sudo tee "$config_file" >/dev/null <<EOF
tunnel: ${tunnel_id}
credentials-file: ${tunnel_dir}/${tunnel_id}.json

ingress:
  - hostname: ${DOMAIN}
    service: http://localhost:5002
  - service: http_status:404
EOF

  # Step 4: DNS route.
  log "routing ${DOMAIN} -> tunnel ${tunnel_name}"
  cloudflared tunnel route dns "$tunnel_name" "$DOMAIN" \
    || warn "DNS route may already exist (or zone not selected); continuing"

  # Step 5: install + start cloudflared as a systemd service.
  sudo cloudflared --config "$config_file" service install \
    || warn "cloudflared service install reported a warning; continuing"
  sudo systemctl enable cloudflared
  sudo systemctl restart cloudflared

  # Brief smoke: wait a few seconds and check status.
  sleep 3
  if ! sudo systemctl is-active --quiet cloudflared; then
    warn "cloudflared service is not active; check 'sudo systemctl status cloudflared'"
  else
    log "cloudflared running; tunnel ${tunnel_name} routes ${DOMAIN}"
  fi
}

# ----------------------------------------------------------------------
# Phase 6 — Claude Code
# ----------------------------------------------------------------------
phase_claude_code() {
  log "Phase 6: install Claude Code"

  load_state
  [ -n "${PI_NAME:-}" ] || die "PI_NAME not set; phase 1 must run first"

  # Step 1: install Claude Code if not present.
  if ! command -v claude >/dev/null 2>&1; then
    log "installing Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash \
      || die "Claude Code install failed"
    # The installer typically drops the binary in ~/.local/bin or /usr/local/bin.
    # Ensure ~/.local/bin is on PATH for this shell.
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v claude >/dev/null 2>&1; then
      die "claude binary not found after install"
    fi
  else
    log "Claude Code already installed: $(claude --version 2>/dev/null || echo unknown)"
  fi

  # Step 2: sign in interactively.
  if ! claude auth status >/dev/null 2>&1; then
    cat <<EOF

  Claude Code needs to sign in to your Anthropic account.

  A URL will appear below. Open it in any browser, sign in (Google or
  email), and approve.

EOF
    claude auth login \
      || die "claude auth login failed"
  else
    log "Claude Code already signed in"
  fi

  # Step 3: launch CC inside a long-lived tmux session named after the Pi.
  if ! tmux has-session -t "$PI_NAME" 2>/dev/null; then
    log "starting tmux session '${PI_NAME}' running CC"
    tmux new-session -d -s "$PI_NAME" -c "${PI_DIR:-$HOME}" \
      "claude --dangerously-skip-permissions 2>&1 | tee -a $HOME/.claude/${PI_NAME}.log"
    # Note: --dangerously-skip-permissions is the "trust the minion" mode
    # matching the connector's philosophy. The user accepts the risk.
  else
    log "tmux session '${PI_NAME}' already exists"
  fi

  # Step 4: arrange for the tmux session to survive reboot.
  local restart_unit="/etc/systemd/system/weyland-cc.service"
  if [ ! -f "$restart_unit" ]; then
    log "installing weyland-cc.service to restart tmux session on boot"
    sudo tee "$restart_unit" >/dev/null <<EOF
[Unit]
Description=Weyland Claude Code tmux session (${PI_NAME})
After=network-online.target

[Service]
Type=forking
User=${USER}
ExecStart=/usr/bin/tmux new-session -d -s ${PI_NAME} -c ${PI_DIR:-$HOME} 'claude --dangerously-skip-permissions 2>&1 | tee -a ${HOME}/.claude/${PI_NAME}.log'
ExecStop=/usr/bin/tmux kill-session -t ${PI_NAME}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable weyland-cc.service
  fi
}

# ----------------------------------------------------------------------
# Phase 7 — Weyland MCP connector
# ----------------------------------------------------------------------
phase_connector() {
  log "Phase 7: install weyland MCP connector"

  load_state
  [ -n "${PI_NAME:-}" ] || die "PI_NAME not set"
  [ -n "${DOMAIN:-}"  ] || die "DOMAIN not set"
  [ -n "${PI_DIR:-}"  ] || die "PI_DIR not set; phase 4 must run first"

  local install_dir="/opt/weyland-mcp"
  local env_dir="/etc/weyland"
  local env_file="${env_dir}/mcp.env"
  local weyland_dir
  weyland_dir="$(cd "$(dirname "$0")/.." && pwd)"

  # Step 1: copy connector source to /opt/weyland-mcp.
  sudo mkdir -p "$install_dir"
  sudo cp -r "${weyland_dir}/connector/." "$install_dir/"
  sudo chown -R "${USER}:${USER}" "$install_dir"

  # Step 2: create venv and install deps.
  if [ ! -d "${install_dir}/.venv" ]; then
    log "creating venv at ${install_dir}/.venv"
    python3 -m venv "${install_dir}/.venv"
  fi
  log "installing connector deps"
  "${install_dir}/.venv/bin/pip" install --quiet --upgrade pip
  "${install_dir}/.venv/bin/pip" install --quiet -e "${install_dir}"

  # Step 3: generate a bearer token (one per Pi, randomly).
  sudo mkdir -p "$env_dir"
  if [ ! -f "$env_file" ] || ! grep -q "^WEYLAND_BEARER_TOKEN_HASH=" "$env_file" 2>/dev/null; then
    local token
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    local token_hash
    token_hash="$(printf '%s' "$token" | sha256sum | awk '{print $1}')"

    sudo tee "$env_file" >/dev/null <<EOF
WEYLAND_BEARER_TOKEN_HASH=${token_hash}
WEYLAND_BIND_HOST=127.0.0.1
WEYLAND_BIND_PORT=5002
WEYLAND_PUBLIC_URL=https://${DOMAIN}/mcp
WEYLAND_LOG_PATH=/var/log/weyland-mcp.log
WEYLAND_PI_NAME=${PI_NAME}
WEYLAND_PI_REPO=${PI_REPO:-}
WEYLAND_PI_DIR=${PI_DIR}
EOF
    sudo chmod 0640 "$env_file"

    save_state WEYLAND_BEARER_TOKEN "$token"
    log "generated bearer token (preview in summary)"
  else
    log "env file already present at ${env_file}; preserving existing token"
  fi

  # Step 4: render systemd unit from template and install.
  local unit_path="/etc/systemd/system/weyland-mcp.service"
  sudo sed "s|{{ PI_NAME }}|${PI_NAME}|g" \
    "${install_dir}/systemd/weyland-mcp.service.template" \
    | sudo tee "$unit_path" >/dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable weyland-mcp.service
  sudo systemctl restart weyland-mcp.service

  # Step 5: smoke test.
  sleep 3
  if curl -fsS -o /dev/null --max-time 5 \
      "http://127.0.0.1:5002/mcp" 2>/dev/null; then
    log "weyland-mcp responding on localhost:5002"
  else
    warn "weyland-mcp not responding on localhost yet; check 'sudo journalctl -u weyland-mcp -n 50'"
  fi
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

  --- ADD THIS CONNECTOR TO CLAUDE DESKTOP ---

    Name:    ${PI_NAME:-?}
    URL:     https://${DOMAIN:-?}/mcp
    Bearer:  ${WEYLAND_BEARER_TOKEN:-(see /var/lib/weyland/env if you missed this)}

  --- THEN ---

    1. Create a Claude Desktop project pointed at the per-Pi repo
       (https://github.com/${OWNER}/${PI_NAME:-?}-pi).
    2. Talk to Claude — it can now drive this minion.

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
