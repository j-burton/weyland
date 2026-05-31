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

# Permanent PAT for weyland repo + secrets-vault access — lets minions update
# their own bootstrap and read the vault. Never committed to this repo.
#
# Normally the operator never sets this: phase_connector fetches it from a
# PRIVATE gist on the authenticated GitHub account (a 'weyland-pat' file) using
# the gh session from phase_github_auth. Exporting WEYLAND_PAT before running is
# only an optional override (e.g. first-ever Pi before the gist exists).
WEYLAND_PAT="${WEYLAND_PAT:-}"

# Where to find the weyland repo on disk. Normally $0 resolves to a
# real path (script was downloaded + run). When piped via
# `bash <(curl ...)`, $0 is /dev/fd/<N> so we have to clone weyland
# to a temp dir to get the templates/, connector/, etc.
WEYLAND_REPO_DIR=""

resolve_weyland_dir() {
  if [ -n "$WEYLAND_REPO_DIR" ] && [ -d "$WEYLAND_REPO_DIR/templates" ]; then
    echo "$WEYLAND_REPO_DIR"
    return 0
  fi

  local script_path="${BASH_SOURCE[0]:-$0}"
  local candidate
  candidate="$(cd "$(dirname "$script_path")/.." 2>/dev/null && pwd)" || true

  if [ -n "$candidate" ] && [ -d "$candidate/templates" ] && [ -d "$candidate/connector" ]; then
    WEYLAND_REPO_DIR="$candidate"
    echo "$WEYLAND_REPO_DIR"
    return 0
  fi

  # Fallback: clone weyland to a temp dir.
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  log "fetching weyland repo to ${tmp_dir}"
  git clone --depth 1 https://github.com/j-burton/weyland.git "$tmp_dir" >/dev/null 2>&1 \
    || die "could not clone weyland repo to ${tmp_dir}"
  WEYLAND_REPO_DIR="$tmp_dir"
  echo "$WEYLAND_REPO_DIR"
}

# Ordered phase list, used by the progress checklist + the main runner.
# (name, label) pairs. Update both lists if you add or remove a phase.
PHASES=(
  "preflight:Pre-flight checks"
  "identity:Identity"
  "packages:System packages"
  "tailscale:Tailscale"
  "github_auth:GitHub auth"
  "per_pi_repo:Per-Pi repo"
  "tunnel:Cloudflare tunnel"
  "claude_code:Claude Code + wake"
  "connector:Weyland MCP connector"
  "vault:Vault consultation"
  "selfdoc:Self-documentation"
  "summary:Summary"
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
log()  { echo -e "\n\033[1;36m[weyland]\033[0m $*" >&2; }
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

# Fetch the weyland PAT from a PRIVATE gist on the authenticated GitHub account,
# so the operator never types or remembers it. Convention: a gist (secret is
# fine) containing a file named 'weyland-pat' whose content is the token. Uses
# the gh session from phase_github_auth. Prints the token, or nothing if not
# found. Tolerates a raw token or a KEY=value line with surrounding whitespace.
fetch_weyland_pat_from_gist() {
  command -v gh >/dev/null 2>&1 || return 1
  gh auth status -h github.com >/dev/null 2>&1 || return 1
  local gid content
  gid="$(gh api gists --paginate \
          --jq '.[] | select(.files | has("weyland-pat")) | .id' 2>/dev/null \
        | head -n1)"
  [ -n "$gid" ] || return 1
  content="$(gh api "gists/${gid}" --jq '.files["weyland-pat"].content' 2>/dev/null || true)"
  printf '%s' "$content" \
    | grep -oE 'github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+' \
    | head -n1
}

# Renders a checklist of all phases, marking each based on $1 (the "current"
# phase name) and $2 ("running" or "done"). Completed phases are tracked in a
# file under $STATE_DIR so the marks survive the many render calls in main().
#
# On a real terminal (stdout is a tty) the checklist is PINNED to the top of
# the screen with a DECSTBM scroll region: the header holds the top rows and
# all phase output scrolls in the region below it, so the checklist stays put
# and is rewritten in place as phases complete. When stdout is not a tty
# (piped/redirected), when WEYLAND_PLAIN_CHECKLIST is set, or when the
# terminal is too short, we fall back to the original plain scrolling list.
CHECKLIST_RULE="────────────────────────────────────────"
CHECKLIST_TTY=0      # 1 once we've taken over a real terminal
CHECKLIST_INIT=0     # 1 after the first in-place render
CHECKLIST_DOWN=0     # 1 after teardown (idempotent guard)
CHECKLIST_HEIGHT=$(( ${#PHASES[@]} + 4 ))   # rule,title,rule,<phases>,rule

# Emit the checklist body (header + one line per phase). Markers use direct
# ANSI colour codes rather than tput setaf for portability.
_checklist_body() {
  local current="$1" current_state="$2" completed="$3"
  local entry name label marker
  printf '%s\n' "$CHECKLIST_RULE"
  printf ' weyland bootstrap\n'
  printf '%s\n' "$CHECKLIST_RULE"
  for entry in "${PHASES[@]}"; do
    name="${entry%%:*}"
    label="${entry#*:}"
    if printf '%s\n' "$completed" | grep -qx "$name"; then
      marker=$'\033[0;32m✓\033[0m'    # done — green
    elif [ "$name" = "$current" ] && [ "$current_state" = "running" ]; then
      marker=$'\033[0;36m▶\033[0m'    # running — cyan
    else
      marker=' '                       # pending
    fi
    printf '  %s %s\n' "$marker" "$label"
  done
  printf '%s\n' "$CHECKLIST_RULE"
}

# Plain scrolling checklist — the safe fallback when we can't drive the tty.
_render_checklist_plain() {
  printf '\n'
  _checklist_body "$1" "$2" "$3"
  printf '\n'
}

# Restore the terminal: drop the scroll region, show the cursor, move below
# the header. Idempotent and safe to call from an EXIT/INT/TERM trap.
_checklist_teardown() {
  [ "$CHECKLIST_DOWN" = "1" ] && return 0
  CHECKLIST_DOWN=1
  if [ "$CHECKLIST_TTY" = "1" ]; then
    local rows
    rows="$(tput lines 2>/dev/null || echo 24)"
    printf '\033[r'                 # reset scroll region to the full screen
    printf '\033[%d;1H' "$rows"     # cursor to the bottom row
    tput cnorm 2>/dev/null || true  # ensure the cursor is visible again
    printf '\n'
  fi
}

render_checklist() {
  local current="$1" current_state="$2"
  local done_file="$STATE_DIR/run-progress"
  sudo mkdir -p "$STATE_DIR" 2>/dev/null || true
  sudo touch "$done_file" 2>/dev/null || true

  # Mark $current done if state=done.
  if [ "$current_state" = "done" ]; then
    echo "$current" | sudo tee -a "$done_file" >/dev/null 2>&1 || true
  fi

  # Read completed set.
  local completed=""
  if [ -r "$done_file" ]; then
    completed="$(cat "$done_file" 2>/dev/null || true)"
  fi

  local rows
  rows="$(tput lines 2>/dev/null || echo 24)"

  # Fallback to plain scrolling when not a terminal, when explicitly
  # requested, or when the terminal is too short to host a scroll region.
  if [ ! -t 1 ] || [ -n "${WEYLAND_PLAIN_CHECKLIST:-}" ] \
     || [ "$rows" -le "$(( CHECKLIST_HEIGHT + 2 ))" ]; then
    _render_checklist_plain "$current" "$current_state" "$completed"
    return 0
  fi

  if [ "$CHECKLIST_INIT" = "0" ]; then
    # First render: take over the terminal.
    CHECKLIST_TTY=1
    tput civis 2>/dev/null || true                  # hide cursor during setup
    printf '\033[2J\033[H'                           # clear screen, cursor home
    _checklist_body "$current" "$current_state" "$completed"
    printf '\033[%d;%dr' "$(( CHECKLIST_HEIGHT + 1 ))" "$rows"  # region below header
    printf '\033[%d;1H' "$(( CHECKLIST_HEIGHT + 1 ))"          # park at region top
    tput cnorm 2>/dev/null || true
    CHECKLIST_INIT=1
  else
    # Update in place: rewrite the header rows; the scroll region's log
    # output is left untouched.
    tput civis 2>/dev/null || true
    printf '\0337'                                   # save cursor (DECSC)
    printf '\033[H'                                  # home to top of header
    _checklist_body "$current" "$current_state" "$completed"
    printf '\0338'                                   # restore cursor (DECRC)
    tput cnorm 2>/dev/null || true
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
  # Default to the Pi's current hostname if it's a valid name AND isn't the
  # generic 'raspberrypi'. Otherwise no default — make the user pick.
  local current_host
  current_host="$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  local default_name=""
  if [[ "$current_host" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]] \
     && [ "$current_host" != "raspberrypi" ]; then
    default_name="$current_host"
  fi
  while :; do
    if [ -n "$default_name" ]; then
      read -r -p "What should this Pi be called? [${default_name}] " name
      name="${name:-$default_name}"
    else
      read -r -p "What should this Pi be called? " name
    fi
    if [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
      break
    fi
    warn "invalid name. Use lowercase letters, digits, hyphens. 2-32 chars. No leading/trailing hyphen."
  done
  PI_NAME="$name"
  save_state PI_NAME "$PI_NAME"

  # Set the system hostname to match PI_NAME so mDNS (.local) resolves to
  # this Pi from the start. Patch /etc/hosts' 127.0.1.1 line to match.
  if [ "$(hostname)" != "$PI_NAME" ]; then
    sudo hostnamectl set-hostname "$PI_NAME" || warn "could not set hostname"
    sudo sed -i "s/127.0.1.1.*/127.0.1.1\t${PI_NAME}/" /etc/hosts || true
    log "hostname set to ${PI_NAME} — reconnect SSH if needed"
  fi

  # DOMAIN: default to <PI_NAME>.$DEFAULT_DOMAIN_ROOT, accept override.
  local default_domain="${PI_NAME}.${DEFAULT_DOMAIN_ROOT}"
  read -r -p "Domain for this Pi's MCP endpoint? [${default_domain}] " dom
  DOMAIN="${dom:-$default_domain}"
  save_state DOMAIN "$DOMAIN"

  # PC AHK wake channel (optional). The watcher/notify scripts POST here so
  # the AHK listener on Julian's PC pops the Claude window. Use the PC's
  # Tailscale (MagicDNS) hostname — not a raw IP — so it keeps resolving if
  # the PC's address changes. Leave blank to skip (Pushcut-only). Saved to
  # state and written to /etc/weyland/wake.env by install-wake.sh; the token
  # is never committed to the repo.
  local pc_host wake_tok
  read -r -p "PC Tailscale hostname for wake (e.g. ju-laptop.tail875649.ts.net — blank to skip): " pc_host
  if [ -n "$pc_host" ]; then
    PC_WAKE_URL="http://${pc_host}:7777"
    save_state PC_WAKE_URL "$PC_WAKE_URL"
    read -r -p "PC wake token (X-Wake-Token): " wake_tok
    WAKE_TOKEN="${wake_tok:-}"
    save_state WAKE_TOKEN "$WAKE_TOKEN"
    log "PC wake channel: ${PC_WAKE_URL}"
  else
    save_state PC_WAKE_URL ""
    save_state WAKE_TOKEN ""
    log "PC wake channel skipped (Pushcut-only)"
  fi

  # Note: nothing secret is prompted here. The Pushcut secret comes from the
  # vault (phase_vault); the WEYLAND_PAT is fetched from a private gist on
  # Julian's GitHub account during phase_connector, using the gh session from
  # phase_github_auth. So the operator types no tokens at provisioning — just
  # the GitHub browser sign-in.

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

  # cloudflared — install from official .deb download, not apt repo
  # (the apt repo doesn't have releases for every Debian codename;
  # the .deb is built per-architecture and tracks latest).
  if ! command -v cloudflared >/dev/null 2>&1; then
    log "installing cloudflared"
    local arch deb_url tmp_deb
    arch="$(dpkg --print-architecture)"
    case "$arch" in
      amd64|arm64|armhf|386) ;;
      *) die "cloudflared: unsupported arch $arch" ;;
    esac
    deb_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
    tmp_deb="$(mktemp --suffix=.deb)"
    curl -fsSL "$deb_url" -o "$tmp_deb" \
      || die "cloudflared: download failed from $deb_url"
    sudo dpkg -i "$tmp_deb" \
      || die "cloudflared: dpkg install failed"
    rm -f "$tmp_deb"
  else
    log "cloudflared already installed"
  fi

  log "packages installed"
}

# ----------------------------------------------------------------------
# Phase 2.5 — Tailscale
# ----------------------------------------------------------------------
phase_tailscale() {
  log "Phase 2.5: Tailscale"

  # Install Tailscale from the official repo if absent.
  if ! command -v tailscale >/dev/null 2>&1; then
    log "installing tailscale"
    curl -fsSL https://tailscale.com/install.sh | sudo sh \
      || die "tailscale install failed"
  else
    log "tailscale already installed"
  fi

  # Join the tailnet. Skip if already authed.
  local status
  status="$(sudo tailscale status --json 2>/dev/null | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d.get("BackendState",""))' \
    2>/dev/null || true)"
  if [ "$status" = "Running" ]; then
    log "tailscale already up"
  else
    cat <<EOF

  Tailscale needs to join your tailnet so this Pi is reachable
  remotely. A URL will appear below; open it in any browser logged
  into your Tailscale account and approve.

EOF
    sudo tailscale up --ssh \
      || die "tailscale up failed"
  fi

  # Record the tailnet hostname for the summary.
  local ts_host
  ts_host="$(sudo tailscale status --json 2>/dev/null | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("HostName",""))' \
    2>/dev/null || true)"
  if [ -n "$ts_host" ]; then
    save_state TAILSCALE_HOST "$ts_host"
    log "tailscale up; ssh as: ssh admin@${ts_host}"
  fi
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
    git -C "$local_dir" pull --ff-only 2>/dev/null || true
  fi

  # Seed from weyland templates if the per-Pi repo is empty.
  local weyland_dir
  weyland_dir="$(resolve_weyland_dir)"

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
    # Run claude directly — no `| tee`. Piping CC's stdout breaks its
    # interactive TTY ("Input must be provided either through stdin or as a
    # prompt argument when using --print"). CC keeps its own logs; the tmux
    # session is the live view.
    tmux new-session -d -s "$PI_NAME" -c "${PI_DIR:-$HOME}" \
      "claude --dangerously-skip-permissions"
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
Type=oneshot
RemainAfterExit=yes
# KillMode=process: on stop, only signal this unit's own (already-exited)
# main process — never the tmux server/claude that may share the cgroup.
# Together with no-ExecStop this keeps the session alive across stop/restart
# even once it is parented under this unit's cgroup.
KillMode=process
User=${USER}
# Create the CC tmux session only if it isn't already running. A service
# (re)start must never recreate — and so never clobber — a live session.
ExecStart=/bin/sh -c '/usr/bin/tmux has-session -t ${PI_NAME} 2>/dev/null || /usr/bin/tmux new-session -d -s ${PI_NAME} -c "${PI_DIR:-$HOME}" "claude --dangerously-skip-permissions"'
# No ExecStop: stopping or restarting this service must NOT kill the tmux
# session — that would terminate the running Claude Code. The session is
# long-lived and intentionally outlives the service.
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable weyland-cc.service
  fi

  # Step 5: install the wake system (Notification hook + tmux watcher).
  local weyland_dir
  weyland_dir="$(resolve_weyland_dir)"
  log "installing wake system"
  bash "${weyland_dir}/connector/scripts/install-wake.sh" \
    "$PI_NAME" "${PC_WAKE_URL:-}" "${WAKE_TOKEN:-}" "${PUSHCUT_WEBHOOK_SECRET:-}"
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
  weyland_dir="$(resolve_weyland_dir)"

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
  # Token-store dir for OAuth-issued access tokens (mode 0700, owned by the
  # service user so the connector can write atomically without sudo).
  sudo install -d -m 0700 -o "$USER" -g "$USER" /var/lib/weyland-mcp
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
WEYLAND_OAUTH_CLIENT_ID=weyland-mcp-claude-ai
WEYLAND_TOKEN_STORE=/var/lib/weyland-mcp/tokens.json
EOF
    save_state WEYLAND_BEARER_TOKEN "$token"
    log "generated bearer token (preview in summary)"
  else
    log "env file already present at ${env_file}; preserving existing token"
  fi

  # The connector runs as ${USER}, not root, so it must be able to READ its
  # env file. root-owned + admin-group + 0640 keeps the bearer hash off the
  # world while letting the service user read it. Applied every run so an
  # already-installed Pi with a root:root 0640 file is repaired.
  sudo chown root:"${USER}" "$env_file"
  sudo chmod 0640 "$env_file"

  # Step 3b: provision the weyland PAT so this minion can update its own
  # bootstrap and read the secrets vault. The operator never types it: it lives
  # in a PRIVATE gist on Julian's GitHub account and is fetched through the gh
  # session authenticated in phase_github_auth. Precedence: a PAT already on
  # this Pi > a $WEYLAND_PAT env override > the gist.
  #
  # SECURITY: this PAT is permanent and shared across all minions. Stored at
  # ${env_dir}/weyland.env (root:${USER} 0640).
  local weyland_env="${env_dir}/weyland.env" have_pat="" pat=""
  if [ -f "$weyland_env" ]; then
    have_pat="$(sudo grep -E '^WEYLAND_PAT=.+' "$weyland_env" 2>/dev/null | cut -d= -f2- || true)"
  fi
  if [ -n "$have_pat" ]; then
    log "weyland PAT already present at ${weyland_env}; preserving"
  else
    pat="${WEYLAND_PAT:-}"
    if [ -z "$pat" ]; then
      log "fetching weyland PAT from your private gist (via gh)…"
      pat="$(fetch_weyland_pat_from_gist || true)"
    fi
    if [ -n "$pat" ]; then
      printf 'WEYLAND_PAT=%s\n' "$pat" | sudo tee "$weyland_env" >/dev/null
      sudo chown root:"${USER}" "$weyland_env"; sudo chmod 0640 "$weyland_env"
      log "weyland PAT provisioned to ${weyland_env}"
    else
      # Create the slot so the file exists; tell Julian how to enable auto-fetch.
      printf 'WEYLAND_PAT=\n' | sudo tee "$weyland_env" >/dev/null
      sudo chown root:"${USER}" "$weyland_env"; sudo chmod 0640 "$weyland_env"
      warn "weyland PAT not found — ONE-TIME setup needed:"
      warn "  Create a PRIVATE (secret) gist on your GitHub account with a file"
      warn "  named 'weyland-pat' whose content is a fine-grained PAT for"
      warn "  j-burton/weyland + weyland-secrets (Contents: read & write, no"
      warn "  expiry). New gist: https://gist.github.com/  — then re-run the"
      warn "  bootstrap. Every future Pi then fetches the PAT automatically."
    fi
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
# Phase — Vault (fleet secret distribution)
# ----------------------------------------------------------------------
# Fetch fleet-wide secrets from the private j-burton/weyland-secrets repo
# ("the vault") and write them to their destination files. Runs after the
# connector phase, so the WEYLAND_PAT is already stored at
# /etc/weyland/weyland.env.
#
# Secrets NEVER live in the public weyland repo; the vault is the single
# private source of truth, and the repo being private + PAT-gated is the
# security boundary (plaintext, no separate encryption to manage). Non-fatal by
# design: if the PAT is missing or the vault is unreachable, we warn and
# continue — the wake system just stays inert until a secret is present.
phase_vault() {
  log "Phase: vault consultation"

  local pat tmp secrets_file
  pat="$(sudo grep -E '^WEYLAND_PAT=' /etc/weyland/weyland.env 2>/dev/null | cut -d= -f2- || true)"
  if [ -z "$pat" ]; then
    warn "vault skipped — no WEYLAND_PAT on this Pi; fleet secrets not fetched"
    return 0
  fi

  tmp="$(mktemp -d)"
  # Stderr → /dev/null so the PAT embedded in the clone URL never lands in logs.
  if ! git clone --quiet --depth 1 \
        "https://${pat}@github.com/${OWNER}/weyland-secrets.git" "$tmp" 2>/dev/null; then
    warn "vault unreachable (clone failed) — fleet secrets not fetched; wake inert until present"
    rm -rf "$tmp"
    return 0
  fi

  secrets_file="${tmp}/secrets.env"
  if [ ! -f "$secrets_file" ]; then
    warn "vault has no secrets.env — nothing written"
    rm -rf "$tmp"
    return 0
  fi

  # Source the vault (trusted private repo). Relax errexit around the source so
  # a single malformed line can't abort the whole bootstrap.
  # shellcheck disable=SC1090
  set +e; set -a; . "$secrets_file"; set +a; set -e

  # Map each known secret to its destination file. Empty/unknown keys are
  # skipped; adding a new destination requires a mapping here.
  if [ -n "${PUSHCUT_WEBHOOK_SECRET:-}" ]; then
    {
      echo "# Pushcut webhook secret — written by the bootstrap vault phase from"
      echo "# the private weyland-secrets repo. Never committed to the public repo."
      echo "PUSHCUT_WEBHOOK_SECRET=${PUSHCUT_WEBHOOK_SECRET}"
    } | sudo tee /etc/weyland/pushcut.env >/dev/null
    sudo chmod 0644 /etc/weyland/pushcut.env
  fi

  rm -rf "$tmp"
  log "vault consulted — secrets written"
}

# ----------------------------------------------------------------------
# Phase 8 — Self-documentation
# ----------------------------------------------------------------------
phase_selfdoc() {
  log "Phase 8: self-documentation"

  load_state
  [ -n "${PI_NAME:-}" ] || die "PI_NAME not set"
  [ -n "${PI_DIR:-}"  ] || die "PI_DIR not set; phase 4 must run first"

  if ! tmux has-session -t "$PI_NAME" 2>/dev/null; then
    warn "tmux session '${PI_NAME}' not found; skipping self-documentation task"
    return 0
  fi

  # The first task we hand the freshly-bootstrapped CC: document this Pi.
  # Sent as a single line so the TUI receives it as one prompt; Enter submits.
  local task
  task="You have just been bootstrapped as a new weyland minion. Your first task is to document this Pi. Investigate what hardware is attached, what software is installed and running, and what this Pi appears to be for. Then fill in these files in ${PI_DIR}: HARDWARE.md (physical hardware, attached devices, display, GPIO etc — use lsusb, lspci, vcgencmd, aplay -l, hostname -I, df -h, free -h); CURRENT_STATE.md (what services are running, anything broken, recent changes — first entry: 'bootstrapped by weyland'); MODULES.md (one section per installed service/app, following the template); README.md (one paragraph describing what this Pi is and does). Commit and push all four files when done. If the Pi appears to be a fresh install with no purpose yet, say so in README.md and leave MODULES.md sparse."

  log "sending self-documentation task to CC in tmux session '${PI_NAME}'"
  tmux send-keys -t "$PI_NAME" -l "$task"
  sleep 1
  tmux send-keys -t "$PI_NAME" Enter

  log "Self-documentation task sent to CC — check the repo in a few minutes."
}

# ----------------------------------------------------------------------
# Phase 9 — Final summary
# ----------------------------------------------------------------------
phase_summary() {
  load_state
  log "Bootstrap complete."

  local LOCAL_IP
  LOCAL_IP="$(hostname -I | awk '{print $1}')"

  # Is the weyland PAT present on this Pi yet? (Never print the value.)
  local pat_status
  if sudo grep -qE '^WEYLAND_PAT=.+' /etc/weyland/weyland.env 2>/dev/null; then
    pat_status="present — fetched from your private gist (or already on this Pi)."
  else
    pat_status="NOT set — create a private gist with a 'weyland-pat' file (see below), then re-run; future Pis fetch it automatically."
  fi

  cat <<EOF

  Pi name:    ${PI_NAME:-?}
  Domain:     ${DOMAIN:-?}

  MCP URL:

    https://${DOMAIN:-?}/mcp

  Repo:

    https://github.com/${OWNER}/${PI_NAME:-?}-pi

  SSH (over Tailscale, from anywhere):

    ssh admin@${TAILSCALE_HOST:-${PI_NAME:-?}}

  --- ADD THIS CONNECTOR TO CLAUDE DESKTOP ---

    Name:            ${PI_NAME:-?}

    URL:

    https://${DOMAIN:-?}/mcp

    OAuth Client ID: weyland-mcp-claude-ai
    Client Secret:   (leave blank — public client, PKCE)

  --- ON FIRST CONNECT ---

    Claude Desktop will redirect you to a consent page. Open this
    consent URL and paste the bearer token there ONE TIME:

    https://${DOMAIN:-?}/weyland-consent

    If the tunnel URL doesn't load, try the local IP URL (same network only):

    http://${LOCAL_IP}:5002/weyland-consent

    If neither URL loads, connect your laptop to a phone hotspot and use the tunnel URL.

    Bearer token:

    ${WEYLAND_BEARER_TOKEN:-(see /var/lib/weyland/env if you missed this)}

    The bearer is hashed on disk; this is your only chance to copy it.

  --- WEYLAND PAT (lets minions update their own bootstrap + read the vault) ---

    Auto-fetched from a PRIVATE gist on your GitHub account (file: weyland-pat)
    via your gh sign-in — you never type it. Stored at /etc/weyland/weyland.env.

    Status: ${pat_status}

    SECURITY: this PAT is permanent and shared across all minions.
    Do not revoke it unless you are rotating every minion.

  --- THEN ---

    1. Create a Claude Desktop project pointed at the per-Pi repo:

       https://github.com/${OWNER}/${PI_NAME:-?}-pi

    2. Talk to Claude — it can now drive this minion.

EOF
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
main() {
  # Always restore the terminal (scroll region + cursor) on exit, even if a
  # phase dies or the user hits Ctrl-C mid-run.
  trap _checklist_teardown EXIT INT TERM

  sudo rm -f "$STATE_DIR/run-progress" 2>/dev/null || true
  load_state
  render_checklist preflight running
  phase_preflight
  render_checklist preflight done; render_checklist identity running
  phase_identity
  render_checklist identity done; render_checklist packages running
  phase_packages
  render_checklist packages done; render_checklist tailscale running
  phase_tailscale
  render_checklist tailscale done; render_checklist github_auth running
  phase_github_auth
  render_checklist github_auth done; render_checklist per_pi_repo running
  phase_per_pi_repo
  render_checklist per_pi_repo done; render_checklist tunnel running
  phase_tunnel
  render_checklist tunnel done; render_checklist claude_code running
  phase_claude_code
  render_checklist claude_code done; render_checklist connector running
  phase_connector
  render_checklist connector done; render_checklist vault running
  phase_vault
  render_checklist vault done; render_checklist selfdoc running
  phase_selfdoc
  render_checklist selfdoc done; render_checklist summary done
  # Restore the terminal before printing the summary so it lands on a clean,
  # full screen; the frozen checklist above shows every phase complete.
  _checklist_teardown
  phase_summary
}

main "$@"
