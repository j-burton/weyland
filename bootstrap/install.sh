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
STATE_DIR="${WEYLAND_STATE_DIR:-/var/lib/weyland}"   # per-Pi state (PI_NAME, DOMAIN, etc.)

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

# ----------------------------------------------------------------------
# Live-dashboard state (/var/lib/weyland/state.json)
# ----------------------------------------------------------------------
# The bash side is the SINGLE writer; dashboard.py only reads it (+ /save-pat).
# All mutations go through one python3 helper that loads, patches, and writes
# atomically (tmp + os.replace). jq may be absent; python3 is a hard dep.
#
# Cosmetic by design: every wrapper is best-effort (|| true) and a no-op when
# state.json doesn't exist (i.e. dashboard never started — plain/terminal mode),
# so the wizard can never break the bootstrap.
STATE_FILE="${STATE_DIR}/state.json"

_state_op() {
  STATE_FILE="$STATE_FILE" python3 - "$@" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile, time
sf = os.environ["STATE_FILE"]; op = sys.argv[1]; a = sys.argv[2:]
if op != "init" and not os.path.exists(sf):
    sys.exit(0)  # dashboard not active -> no-op
PHASES = [
    ("preflight","The forge is inspected"), ("identity","The minion receives its name"),
    ("packages","Tools of war are gathered"), ("tailscale","The minion enters the realm"),
    ("github_auth","GitHub demands tribute"), ("per_pi_repo","The chronicles are opened"),
    ("tunnel","The passage through the void is opened"), ("claude_code","The intelligence is summoned"),
    ("connector","The connector is forged"), ("vault","The ancient secrets are retrieved"),
    ("selfdoc","The minion speaks its name"), ("summary","The induction is sealed"),
]
try:
    with open(sf) as f: s = json.load(f)
except Exception:
    s = {}
if op == "init":
    pi, dom, ip = (a + ["", "", ""])[:3]
    s = {"pi_name": pi, "domain": dom, "local_ip": ip,
         "phases": [{"name": n, "label": l, "status": "pending"} for n, l in PHASES],
         "action": {"active": False},
         "result": {"ready": False, "bearer": "", "mcp_url": "", "consent_tunnel": "",
                    "consent_local": "", "client_id": "weyland-mcp-claude-ai",
                    "repo": "", "project_instructions": ""}}
elif op == "meta":
    pi, dom = (a + ["", ""])[:2]
    if pi: s["pi_name"] = pi
    if dom: s["domain"] = dom
elif op == "phase":
    # Stamp activity timestamps so the dashboard can tell working from stalled:
    # started_at the first time a phase goes running, updated_at on any change.
    now = int(time.time())
    for p in s.get("phases", []):
        if p.get("name") == a[0]:
            new = a[1]
            if new == "running" and p.get("status") != "running":
                p["started_at"] = now
            if p.get("status") != new:
                p["updated_at"] = now
            p["status"] = new
elif op == "action":
    s["action"] = {"provider": a[0],
                   "url": a[1] if len(a) > 1 else "",
                   "code": a[2] if len(a) > 2 else "",
                   "active": True}
elif op == "action_clear":
    s.setdefault("action", {})["active"] = False
elif op == "result_set":
    s.setdefault("result", {})[a[0]] = a[1] if len(a) > 1 else ""
elif op == "ready":
    s.setdefault("result", {})["ready"] = True
d = os.path.dirname(sf) or "."
fd, tmp = tempfile.mkstemp(dir=d)
with os.fdopen(fd, "w") as f: json.dump(s, f)
os.replace(tmp, sf)
PY
}
state_init()         { _state_op init "${1:-}" "${2:-}" "${3:-}"; }
state_meta()         { _state_op meta "${1:-}" "${2:-}"; }
state_phase()        { _state_op phase "$1" "$2"; }
state_action()       { _state_op action "$1" "${2:-}" "${3:-}"; }   # provider url [code]
state_action_clear() { _state_op action_clear; }
state_result_set()   { _state_op result_set "$1" "${2:-}"; }
state_ready()        { _state_op ready; }

# Overall cap on an interactive browser-login step (tailscale / gh / cloudflared
# / claude). The run_dance watcher loops ~1800s only to SURFACE the auth URL; the
# auth command itself had no timeout, so an operator who never finishes the
# browser step would block the phase — and the whole bootstrap — forever (the
# live symptom: a stage stuck "running" with no end). On expiry the phase fails
# loud instead. Override via WEYLAND_LOGIN_TIMEOUT.
RUN_DANCE_TIMEOUT="${WEYLAND_LOGIN_TIMEOUT:-900}"   # 15 min

# ----------------------------------------------------------------------
# run_dance — wrap an interactive auth command for the live dashboard
# ----------------------------------------------------------------------
# Usage: run_dance <phase> <provider> <url_regex> -- <cmd...>
# Marks the phase running + raises the provider's auth card immediately, runs
# the command under a pty (so CLIs that gate their URL on a tty still print it),
# tees output to $STATE_DIR/<phase>.log, and a background watcher publishes the
# auth URL (+ device code) to the action as soon as they appear. On success it
# clears the card and marks the phase done; on failure marks it error. Returns
# the command's exit code so the caller can `|| die` as before.
run_dance() {
  local phase="$1" provider="$2" url_re="$3"; shift 3
  [ "${1:-}" = "--" ] && shift
  local cmd="$*"
  local logf="${STATE_DIR}/${phase}.log"
  : > "$logf" 2>/dev/null || true

  state_phase "$phase" running
  state_action "$provider"     # card appears now (provider ritual copy lives in the dashboard JS)

  # Watcher: surface the URL + code the moment they appear (up to ~30 min).
  (
    n=0
    while [ "$n" -lt 1800 ]; do
      if [ -s "$logf" ]; then
        # The log is a `script` pty typescript: CLIs emit ANSI escapes / CRs /
        # spinners, which make grep treat the file as BINARY and silently miss
        # the URL (the original capture bug). Strip control bytes first, then
        # grep the clean text (-a as a belt-and-braces).
        clean="$(sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e 's/\x1b[()][0AB]//g' -e 's/\x1b[=>]//g' "$logf" 2>/dev/null | tr -d '\r' || true)"
        u="$(printf '%s' "$clean" | grep -aoE "$url_re" | head -n1 || true)"
        if [ -n "$u" ]; then
          c="$(printf '%s' "$clean" | grep -aoE '[A-Z0-9]{4}-[A-Z0-9]{4}' | head -n1 || true)"
          state_action "$provider" "$u" "$c"
          break
        fi
      fi
      sleep 1; n=$((n + 1))
    done
  ) &
  local watcher=$!

  # Paste-back channel: some logins (Claude) print a URL AND then wait for the
  # operator to paste a code back. The dashboard collects it (POST /authcode ->
  # $STATE_DIR/authcode); this feeder pipes it into the login's stdin via a FIFO.
  local codef="${STATE_DIR}/authcode" fifo="" feeder="" stdin_src=/dev/null
  rm -f "$codef" 2>/dev/null || true
  if [ "$provider" = "anthropic" ]; then
    fifo="${STATE_DIR}/${phase}.in"
    rm -f "$fifo" 2>/dev/null || true
    if mkfifo "$fifo" 2>/dev/null; then
      stdin_src="$fifo"
      (
        exec 3>"$fifo"     # unblocks once the login opens the FIFO for reading
        fn=0
        while [ "$fn" -lt 1800 ]; do
          if [ -s "$codef" ]; then
            cc="$(cat "$codef" 2>/dev/null)"; rm -f "$codef" 2>/dev/null || true
            [ -n "$cc" ] && printf '%s\n' "$cc" >&3
          fi
          sleep 1; fn=$((fn + 1))
        done
      ) &
      feeder=$!
    else
      fifo=""
    fi
  fi

  local rc=0
  # timeout caps the WHOLE login: SIGTERM at $cap, SIGKILL 10s later if it clings
  # (an unfinished `cloudflared tunnel login` etc. ignores TERM). 124 = timed out.
  local cap="${RUN_DANCE_TIMEOUT:-900}"
  if command -v script >/dev/null 2>&1; then
    # util-linux script: pty-backed, records to logf, -e returns child's status.
    # -f (--flush) is CRITICAL: without it `script` buffers its typescript file
    # writes, so a CLI that prints its auth URL (~100 bytes) and then BLOCKS
    # waiting for the operator never fills the buffer — the log stays empty and
    # the watcher never captures the URL (the live "empty Tailscale button" bug,
    # reproduced on inkypi). -f flushes after every write so the URL lands in the
    # log within a poll and the dashboard button gets its href.
    timeout -k 10 "$cap" script -qfec "$cmd" "$logf" < "$stdin_src" || rc=$?
  else
    timeout -k 10 "$cap" bash -c "$cmd" < "$stdin_src" 2>&1 | tee "$logf" || true
    rc=${PIPESTATUS[0]}
  fi

  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  if [ -n "$feeder" ]; then kill "$feeder" 2>/dev/null || true; wait "$feeder" 2>/dev/null || true; fi
  [ -n "$fifo" ] && rm -f "$fifo" 2>/dev/null || true
  rm -f "$codef" 2>/dev/null || true
  state_action_clear
  if [ "$rc" -eq 0 ]; then
    state_phase "$phase" done
  else
    state_phase "$phase" error
    # 124 (TERM at $cap) / 137 (KILL after -k): the operator never finished the
    # browser step. Say so plainly so the dashboard's red stall message is useful.
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      warn "${provider} login not completed in time (${cap}s) — re-run the bootstrap to retry"
    fi
  fi
  return "$rc"
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
    elif [ "$name" = "$current" ] && [ "$current_state" = "stalled" ]; then
      marker=$'\033[0;33m!\033[0m'    # stalled — amber (forge gone cold)
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
  # In dashboard mode the terminal is silent — the browser is the only surface.
  [ -n "${WEYLAND_DASH_ACTIVE:-}" ] && return 0
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
# Live dashboard lifecycle (the forge-fire setup wizard)
# ----------------------------------------------------------------------
# Starts dashboard.py BEFORE phase_preflight so the browser cockpit is alive
# for the whole bootstrap. Best-effort: any failure leaves the pinned terminal
# checklist as the working fallback. Skipped entirely under WEYLAND_PLAIN_CHECKLIST.
WEYLAND_NONCE="${WEYLAND_NONCE:-}"   # may be inherited on a start-over relaunch
WEYLAND_DASH_URL=""
WEYLAND_DASH_ACTIVE=""
phase_dashboard_start() {
  if [ -n "${WEYLAND_PLAIN_CHECKLIST:-}" ]; then
    log "dashboard skipped (WEYLAND_PLAIN_CHECKLIST) — terminal checklist only"
    return 0
  fi
  command -v python3 >/dev/null 2>&1 || { warn "python3 missing; dashboard skipped"; return 0; }

  # Start-over relaunch: the operator clicked "Start over" in the wizard, so the
  # already-running dashboard reset the state and re-exec'd us with its own nonce
  # (WEYLAND_REUSE_DASHBOARD=1). It still owns $STATE_DIR/dashboard.pid and is
  # serving on the same port, so we must NOT start a second server — just adopt
  # the live one and fall straight through to phase_identity (which will wait on
  # the browser again). Mirrors the tail of the normal branch below.
  if [ -n "${WEYLAND_REUSE_DASHBOARD:-}" ] && [ -n "$WEYLAND_NONCE" ]; then
    local ri
    ri="$(hostname -I 2>/dev/null | awk '{print $1}')"
    WEYLAND_DASH_URL="http://${ri:-<this-pi-ip>}:${WEYLAND_DASH_PORT:-8080}?k=${WEYLAND_NONCE}"
    WEYLAND_DASH_ACTIVE=1
    echo "$$" > "$STATE_DIR/bootstrap.pid" 2>/dev/null || true
    exec >>"$STATE_DIR/bootstrap.log" 2>&1
    log "reusing live dashboard on :${WEYLAND_DASH_PORT:-8080} (start-over relaunch)"
    return 0
  fi

  local local_ip dash weyland_dir
  local_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  WEYLAND_NONCE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))' 2>/dev/null || true)"
  [ -n "$WEYLAND_NONCE" ] || WEYLAND_NONCE="k$(date +%s 2>/dev/null)$$"
  WEYLAND_DASH_URL="http://${local_ip:-<this-pi-ip>}:8080?k=${WEYLAND_NONCE}"

  # The URL is the VERY FIRST thing the bootstrap prints — before any setup
  # output or the checklist — so it can never scroll away.
  _announce_wizard

  # $STATE_DIR must be writable by us: state.json is written atomically
  # (tmp + os.replace), which needs directory write.
  sudo mkdir -p "$STATE_DIR" 2>/dev/null || true
  sudo chown "$(id -un):$(id -gn)" "$STATE_DIR" 2>/dev/null || true
  sudo chmod 0755 "$STATE_DIR" 2>/dev/null || true
  state_init "" "" "$local_ip"

  weyland_dir="$(resolve_weyland_dir 2>/dev/null)" || true
  dash="${weyland_dir}/bootstrap/dashboard.py"
  if [ ! -f "$dash" ]; then
    warn "dashboard.py not found (${dash}); dashboard skipped (terminal fallback)"
    return 0
  fi

  nohup python3 "$dash" "$STATE_DIR" "$WEYLAND_NONCE" >"$STATE_DIR/dashboard.log" 2>&1 &
  echo $! > "$STATE_DIR/dashboard.pid"
  echo "$$" > "$STATE_DIR/bootstrap.pid" 2>/dev/null || true   # so the wizard's "Start over" can stop us
  WEYLAND_DASH_ACTIVE=1

  # Terminal goes silent from here: phases, checklist and logs all live in the
  # browser now. The rest of the run is redirected to a log for debugging; the
  # operator never needs to watch this terminal again. (Plain mode / a missing
  # dashboard keeps the terminal checklist instead — we return before this.)
  exec >>"$STATE_DIR/bootstrap.log" 2>&1
  log "dashboard live on :8080 (pid $(cat "$STATE_DIR/dashboard.pid" 2>/dev/null))"
}

# Print the wizard URL — the one and only terminal output (dashboard mode).
_announce_wizard() {
  [ -n "${WEYLAND_DASH_URL:-}" ] || return 0
  printf '\n\n  \033[1;38;5;208m⚒  WEYLAND SUMMONS YOU, MY LORD\033[0m\n\n      \033[1;97m%s\033[0m\n\n  open this URL to begin the rite of binding.\n\n' \
    "$WEYLAND_DASH_URL" >&2
}

# True when the live dashboard is running (browser-driven identity available).
_dashboard_active() { [ -f "$STATE_DIR/dashboard.pid" ] && [ -n "${WEYLAND_NONCE:-}" ]; }

phase_dashboard_stop() {
  local pidf="$STATE_DIR/dashboard.pid" pid
  [ -f "$pidf" ] || return 0
  pid="$(cat "$pidf" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pidf" 2>/dev/null || true
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

  # Re-run: every phase is idempotent, so just continue — never block on a
  # prompt (the operator may never be at this terminal). Log it for context.
  if [ -f "$STATE_DIR/env" ]; then
    load_state
    [ -n "${PI_NAME:-}" ] && log "re-run detected (already named '${PI_NAME}') — continuing."
  fi

  log "pre-flight OK"
}

# ----------------------------------------------------------------------
# Phase 1 — Identity (who is this Pi?)
# ----------------------------------------------------------------------
# Identity input — terminal prompts (fallback path / WEYLAND_PLAIN_CHECKLIST).
# Sets globals PI_NAME DOMAIN IDENT_PC_HOST IDENT_WAKE_TOK. The name regex:
# lowercase alnum + hyphens, 2-32 chars, no leading/trailing hyphen.
_identity_from_terminal() {
  local default_name="$1" name dom
  while :; do
    if [ -n "$default_name" ]; then
      read -r -p "What should this Pi be called? [${default_name}] " name
      name="${name:-$default_name}"
    else
      read -r -p "What should this Pi be called? " name
    fi
    [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]] && break
    warn "invalid name. lowercase letters, digits, hyphens. 2-32 chars. no leading/trailing hyphen."
  done
  PI_NAME="$name"
  read -r -p "Domain for this Pi's MCP endpoint? [${PI_NAME}.${DEFAULT_DOMAIN_ROOT}] " dom
  DOMAIN="${dom:-${PI_NAME}.${DEFAULT_DOMAIN_ROOT}}"
  read -r -p "PC Tailscale hostname for wake (e.g. ju-laptop.tail875649.ts.net — blank to skip): " IDENT_PC_HOST
  [ -n "$IDENT_PC_HOST" ] && read -r -p "PC wake token (X-Wake-Token): " IDENT_WAKE_TOK
}

# Identity input — the live wizard. Polls for identity.json (written by
# dashboard.py on POST /identity), then reads the values. Falls back to terminal
# prompts after a long wait or if the wizard returns an invalid name.
_identity_from_browser() {
  local default_name="$1" f="$STATE_DIR/identity.json" waited=0
  log "answer the identity questions in the wizard page — no need to touch this terminal."
  while [ ! -f "$f" ]; do
    sleep 2; waited=$((waited + 2))
    if [ "$waited" -ge 1800 ]; then
      warn "no name received from the wizard in 30 min — prompting in the terminal."
      _identity_from_terminal "$default_name"
      return 0
    fi
  done
  local _vals=()
  mapfile -t _vals < <(python3 - "$f" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
for k in ("pi_name", "domain", "pc_wake", "wake_token"):
    print(str(d.get(k, "") or "").strip())
# new_password: verbatim (spaces may be intentional). ssh fields: single line.
print(str(d.get("new_password", "") or ""))
print(str(d.get("ssh_mode", "none") or "none").strip())
print(str(d.get("ssh_pub_key", "") or "").strip())
PY
  )
  PI_NAME="${_vals[0]:-}"; DOMAIN="${_vals[1]:-}"
  IDENT_PC_HOST="${_vals[2]:-}"; IDENT_WAKE_TOK="${_vals[3]:-}"
  IDENT_NEW_PASSWORD="${_vals[4]:-}"; IDENT_SSH_MODE="${_vals[5]:-none}"; IDENT_SSH_PUB_KEY="${_vals[6]:-}"
  if ! [[ "$PI_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
    warn "the wizard returned an invalid name ('${PI_NAME}') — prompting in the terminal."
    _identity_from_terminal "$default_name"
  else
    log "received from the wizard: PI_NAME=${PI_NAME}"
  fi
}

# Import an SSH public key into the service user's authorized_keys (dedup, right
# perms). VALIDATES the key looks real first; no-op for an unrecognised one.
# Password auth is left UNCHANGED on purpose — SSH prefers the key when present
# and the operator keeps a password fallback on machines without it.
_import_ssh_key() {
  local key="$1"
  case "$key" in
    ssh-ed25519\ *|ssh-rsa\ *|ssh-dss\ *|ecdsa-sha2-*\ *|sk-ssh-ed25519@openssh.com\ *|sk-ecdsa-sha2-*\ *) : ;;
    *) warn "imported SSH key is not a recognised public key — skipping"; return 0 ;;
  esac
  local ak="${HOME}/.ssh/authorized_keys"
  mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
  touch "$ak" && chmod 600 "$ak"
  if grep -qxF "$key" "$ak" 2>/dev/null; then
    log "SSH key already present in authorized_keys"
  else
    printf '%s\n' "$key" >> "$ak" && log "SSH public key imported to authorized_keys"
  fi
}

phase_identity() {
  log "Phase 1: identity"

  # Skip if already set (re-run case).
  load_state
  if [ -n "${PI_NAME:-}" ] && [ -n "${DOMAIN:-}" ]; then
    log "identity already set: PI_NAME=${PI_NAME} DOMAIN=${DOMAIN}"
    state_meta "$PI_NAME" "$DOMAIN"
    return 0
  fi

  # Default name = the Pi's hostname if it's valid and not the stock
  # 'raspberrypi'; otherwise the operator must provide one.
  local current_host default_name=""
  current_host="$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  if [[ "$current_host" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]] \
     && [ "$current_host" != "raspberrypi" ]; then
    default_name="$current_host"
  fi

  # Gather identity from the browser when the wizard is live, else the terminal.
  IDENT_PC_HOST=""; IDENT_WAKE_TOK=""; IDENT_NEW_PASSWORD=""; IDENT_SSH_MODE="none"; IDENT_SSH_PUB_KEY=""
  if _dashboard_active; then
    _identity_from_browser "$default_name"
  else
    _identity_from_terminal "$default_name"
  fi

  PI_NAME="${PI_NAME:-$default_name}"
  [ -n "$PI_NAME" ] || die "no minion name provided"
  DOMAIN="${DOMAIN:-${PI_NAME}.${DEFAULT_DOMAIN_ROOT}}"
  save_state PI_NAME "$PI_NAME"
  save_state DOMAIN "$DOMAIN"

  # System hostname to match PI_NAME (mDNS .local); patch /etc/hosts 127.0.1.1.
  if [ "$(hostname)" != "$PI_NAME" ]; then
    sudo hostnamectl set-hostname "$PI_NAME" || warn "could not set hostname"
    sudo sed -i "s/127.0.1.1.*/127.0.1.1\t${PI_NAME}/" /etc/hosts || true
    log "hostname set to ${PI_NAME}"
  fi

  # PC AHK wake channel (optional). Tailscale (MagicDNS) hostname, never a raw
  # IP. Written to /etc/weyland/wake.env by install-wake.sh; token never committed.
  if [ -n "${IDENT_PC_HOST:-}" ]; then
    PC_WAKE_URL="http://${IDENT_PC_HOST}:7777"; save_state PC_WAKE_URL "$PC_WAKE_URL"
    WAKE_TOKEN="${IDENT_WAKE_TOK:-}"; save_state WAKE_TOKEN "$WAKE_TOKEN"
    log "PC wake channel: ${PC_WAKE_URL}"
  else
    save_state PC_WAKE_URL ""; save_state WAKE_TOKEN ""
    log "PC wake channel skipped (Pushcut-only)"
  fi

  # Optional: change the service user's password (from the wizard's password
  # field). Blank = leave it unchanged. printf (not echo) so backslashes etc.
  # in the password are taken literally.
  if [ -n "${IDENT_NEW_PASSWORD:-}" ]; then
    if printf '%s:%s\n' "$(id -un)" "$IDENT_NEW_PASSWORD" | sudo chpasswd 2>/dev/null; then
      log "admin password changed (via wizard)"
    else
      warn "could not change admin password"
    fi
  fi

  # Optional: import an SSH public key (existing or browser-generated). Password
  # auth is intentionally left enabled (see _import_ssh_key).
  if [ "${IDENT_SSH_MODE:-none}" != "none" ] && [ -n "${IDENT_SSH_PUB_KEY:-}" ]; then
    _import_ssh_key "$IDENT_SSH_PUB_KEY"
  fi

  # Scrub the plaintext password out of identity.json now that it's applied, so
  # it doesn't linger on disk (the public key is left — it isn't a secret).
  if [ -n "${IDENT_NEW_PASSWORD:-}" ] && [ -f "$STATE_DIR/identity.json" ]; then
    python3 - "$STATE_DIR/identity.json" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    sys.exit(0)
d.pop("new_password", None)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p) or ".")
with os.fdopen(fd, "w") as f:
    json.dump(d, f)
os.replace(tmp, p)
PY
    IDENT_NEW_PASSWORD=""
  fi

  state_meta "$PI_NAME" "$DOMAIN"   # name the minion on the live dashboard
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
    run_dance tailscale tailscale 'https://login\.tailscale\.com/a/[A-Za-z0-9]+' -- \
      'sudo tailscale up --ssh' \
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
      gh auth setup-git --hostname github.com >/dev/null 2>&1 || gh auth setup-git >/dev/null 2>&1 || warn "gh auth setup-git failed; git may prompt on clone/push"
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

  # Wrapped for the live dashboard: gh prints the device code + URL, then waits
  # for the operator's browser. The leading `printf '\n'` answers gh's "Press
  # Enter to open in browser" prompt (no one is at the Pi's terminal); the
  # device code + URL are already in the log by then for the dashboard to show.
  run_dance github_auth github 'https://github\.com/login/device' -- \
    'printf "\n" | gh auth login --hostname github.com --git-protocol https --web' \
    || die "gh auth login failed"

  # Verify.
  local current_user
  current_user="$(gh api user --jq .login)" \
    || die "gh auth completed but 'gh api user' failed"
  if [ "$current_user" != "$OWNER" ]; then
    die "gh authed as '${current_user}', not '${OWNER}'. Re-run after fixing."
  fi
  # Wire git itself to gh's token so clones/pushes of PRIVATE repos (the per-Pi
  # repo here, and CC's pushes later) authenticate with no prompt. Without this,
  # git asks for a username and — with no terminal — the clone hangs forever.
  gh auth setup-git --hostname github.com >/dev/null 2>&1 || gh auth setup-git >/dev/null 2>&1 || warn "gh auth setup-git failed; git may prompt on clone/push"
  log "gh authed as ${OWNER}; git wired to gh credentials"
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

  # Make sure git can authenticate to GitHub before any clone/push below (and
  # for CC's pushes later). Run every time — covers a resumed run where the
  # GitHub-auth phase was already 'done' and thus skipped.
  gh auth setup-git --hostname github.com >/dev/null 2>&1 || gh auth setup-git >/dev/null 2>&1 || true

  # Clone or pull into /opt/<pi-name>-pi.
  sudo mkdir -p "$(dirname "$local_dir")"
  if [ ! -d "$local_dir/.git" ]; then
    log "cloning ${repo_slug} to ${local_dir}"
    sudo chown "$(id -u):$(id -g)" "$(dirname "$local_dir")" || true
    GIT_TERMINAL_PROMPT=0 timeout 120 gh repo clone "$repo_slug" "$local_dir" \
      || die "clone of ${repo_slug} failed or timed out — is git authenticated? (gh auth setup-git)"
  else
    log "${local_dir} already cloned; pulling latest"
    if ! GIT_TERMINAL_PROMPT=0 timeout 30 git -C "$local_dir" pull --ff-only 2>&1; then
      warn "pull failed or timed out for ${local_dir}; continuing with existing checkout"
    fi
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
    GIT_TERMINAL_PROMPT=0 timeout 60 git -C "$local_dir" push -u origin "$(git -C "$local_dir" rev-parse --abbrev-ref HEAD)" \
      || warn "push failed or timed out; check connectivity"
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
    run_dance tunnel cloudflare 'https://dash\.cloudflare\.com/argotunnel[^[:space:]]*' -- \
      'cloudflared tunnel login' \
      || die "cloudflared tunnel login failed"
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
  # Re-run safety: a freshly created tunnel drops its creds in ~/.cloudflared,
  # but on a re-run that file may be gone (already moved to /etc/cloudflared, or
  # $HOME cleaned) while the tunnel still exists. Prefer the home copy; fall back
  # to the already-installed one; only die if neither is present.
  local creds_file="$HOME/.cloudflared/${tunnel_id}.json"
  if [ -f "$creds_file" ]; then
    sudo cp "$creds_file" "${tunnel_dir}/${tunnel_id}.json"
  elif [ -f "${tunnel_dir}/${tunnel_id}.json" ]; then
    log "tunnel creds already installed at ${tunnel_dir}/${tunnel_id}.json; reusing"
  else
    die "tunnel credentials file missing (looked in ${creds_file} and ${tunnel_dir}); delete the tunnel in Cloudflare and re-run to recreate it"
  fi
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
# Install the weyland status line (bootstrap/cc-status.sh → ~/.claude/cc-status.sh)
# and wire it into Claude Code's settings.json. The merge is additive — it never
# clobbers existing keys (the cc-notify Notification hook, theme, etc.). The
# status line shows context-window usage from 50% up; cc-tmux-watcher greps that
# same "ctx NN%" out of the pane for its 60/70/80/90% Pushcut alerts. Idempotent.
_configure_cc_statusline() {
  local weyland_dir src dst settings
  weyland_dir="$(resolve_weyland_dir)"
  src="${weyland_dir}/bootstrap/cc-status.sh"
  dst="${HOME}/.claude/cc-status.sh"
  settings="${HOME}/.claude/settings.json"
  if [ ! -f "$src" ]; then
    warn "cc-status.sh not found (${src}); status line skipped"
    return 0
  fi
  mkdir -p "${HOME}/.claude"
  install -m 0755 "$src" "$dst"
  WEYLAND_CC_STATUS="$dst" WEYLAND_CC_SETTINGS="$settings" python3 - <<'PY'
import json, os, tempfile
settings = os.environ["WEYLAND_CC_SETTINGS"]
cmd = os.environ["WEYLAND_CC_STATUS"]
try:
    with open(settings) as f:
        s = json.load(f)
    if not isinstance(s, dict):
        s = {}
except Exception:
    s = {}
s["statusLine"] = {"type": "command", "command": cmd, "padding": 1}
d = os.path.dirname(settings) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d)
with os.fdopen(fd, "w") as f:
    json.dump(s, f, indent=2)
os.replace(tmp, settings)
PY
  log "status line wired into settings.json (${dst})"
}

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
    run_dance claude_code anthropic 'https://claude\.com/cai/oauth/authorize[^[:space:]]*|https://[A-Za-z0-9.-]*(anthropic\.com|claude\.ai)[^[:space:]]*' -- \
      'claude auth login' \
      || die "claude auth login failed"
  else
    log "Claude Code already signed in"
  fi

  # Step 2.5: weyland status line (context % from 50% up) — set BEFORE the tmux
  # CC session launches so Claude Code picks it up at startup.
  _configure_cc_statusline

  # Step 3: launch CC inside a long-lived tmux session named after the Pi.
  # RECREATE it here (rather than skip-if-exists) so the session always starts
  # AFTER the sign-in above and inherits the credentials now on disk. A session
  # lingering from BEFORE sign-in — e.g. one the boot service started after a
  # mid-setup reboot — would otherwise sit logged-out at its own login screen,
  # forcing a second manual sign-in and stalling every later step that drives CC.
  # Killing + relaunching guarantees the wizard's single sign-in covers CC too.
  if tmux has-session -t "$PI_NAME" 2>/dev/null; then
    log "recreating tmux session '${PI_NAME}' so CC inherits the sign-in"
    tmux kill-session -t "$PI_NAME" 2>/dev/null || true
  else
    log "starting tmux session '${PI_NAME}' running CC"
  fi
  # Run claude directly — no `| tee`. Piping CC's stdout breaks its
  # interactive TTY ("Input must be provided either through stdin or as a
  # prompt argument when using --print"). CC keeps its own logs; the tmux
  # session is the live view.
  # Launch via a LOGIN shell (bash -lc) so ~/.profile puts ~/.local/bin — where
  # the claude installer drops the binary — on PATH. A bare command would run on
  # tmux's minimal non-login PATH and die instantly ("claude: not found").
  tmux new-session -d -s "$PI_NAME" -c "${PI_DIR:-$HOME}" \
    "bash -lc 'exec claude --dangerously-skip-permissions'"
  # Note: --dangerously-skip-permissions is the "trust the minion" mode
  # matching the connector's philosophy. The user accepts the risk.

  # Step 4: arrange for the tmux session to survive reboot.
  # Written every run (not just when absent) so an existing Pi picks up unit
  # fixes — e.g. the bash -lc PATH fix — on a re-run. Safe to rewrite + reload
  # live: the unit is oneshot + RemainAfterExit with KillMode=process and no
  # ExecStop, so this never touches the running tmux session/CC (and we
  # deliberately don't restart it here).
  local restart_unit="/etc/systemd/system/weyland-cc.service"
  log "writing weyland-cc.service (recreates the CC tmux session on boot)"
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
# Recreate via a LOGIN shell (bash -lc) so ~/.profile adds ~/.local/bin to PATH;
# the bare "claude" ran on /bin/sh's minimal PATH, died instantly, and the tmux
# server exited — so this unit could never actually restart CC after a crash.
ExecStart=/bin/sh -c '/usr/bin/tmux has-session -t ${PI_NAME} 2>/dev/null || /usr/bin/tmux new-session -d -s ${PI_NAME} -c "${PI_DIR:-$HOME}" "bash -lc '\''exec claude --dangerously-skip-permissions'\''"'
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
    state_result_set bearer "$token"   # stash for the dashboard (never logged)
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

  load_state   # for PI_DIR (per-Pi repo path)
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

  # Sync the fleet map (FLEET.md) into the per-Pi repo so a Claude operating
  # this minion has it locally. Best-effort and non-fatal; commit only if it
  # changed. (Non-secret reference; per-Pi repos are private.)
  if [ -f "${tmp}/FLEET.md" ] && [ -n "${PI_DIR:-}" ] && [ -d "${PI_DIR}/.git" ]; then
    if ! cmp -s "${tmp}/FLEET.md" "${PI_DIR}/FLEET.md" 2>/dev/null; then
      cp "${tmp}/FLEET.md" "${PI_DIR}/FLEET.md"
      if git -C "$PI_DIR" add FLEET.md \
         && git -C "$PI_DIR" -c user.email=weyland@localhost -c user.name=weyland \
              commit -q -m "docs: sync FLEET.md from vault" \
         && git -C "$PI_DIR" push -q 2>/dev/null; then
        log "FLEET.md synced to per-Pi repo"
      else
        warn "FLEET.md copied to ${PI_DIR} but commit/push skipped"
      fi
    fi
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

  # Publish the end-state to the live dashboard, then flip it to "the rite is
  # complete". (Mirrors the terminal heredoc below, which stays as the fallback
  # for WEYLAND_PLAIN_CHECKLIST / no-browser operators.)
  local proj
  proj="$(cat <<PROJEOF
You are working on a single Raspberry Pi minion in Julian's fleet.

This project's MCP connector talks to one specific Pi. Read these files from
/opt/${PI_NAME:-minion}-pi/ in order: README.md, IDENTITY.md, CURRENT_STATE.md,
MODULES.md, FLEET.md. Follow the README's communication rules and wake drill.
When you finish a task, fire Pushcut to Julian's phone.
PROJEOF
)"
  state_result_set mcp_url        "https://${DOMAIN:-this-pi}/mcp"
  state_result_set consent_tunnel "https://${DOMAIN:-this-pi}/weyland-consent"
  state_result_set consent_local  "http://${LOCAL_IP}:5002/weyland-consent"
  state_result_set client_id      "weyland-mcp-claude-ai"
  state_result_set repo           "https://github.com/${OWNER}/${PI_NAME:-minion}-pi"
  state_result_set bearer         "${WEYLAND_BEARER_TOKEN:-}"
  state_result_set project_instructions "$proj"
  state_ready

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
# Teardown / signal handling
# ----------------------------------------------------------------------
# Restore the terminal + stop the dashboard. Used on every exit.
_cleanup() {
  _checklist_teardown
  phase_dashboard_stop
  rm -f "$STATE_DIR/bootstrap.pid" 2>/dev/null || true
}
# Ctrl-C / SIGTERM / SSH hangup: clean up, kill any background children
# (dashboard, run_dance watchers, tee, …) and DIE. The bootstrap must never
# keep running in the background after the operator interrupts it.
_on_signal() {
  trap - INT TERM HUP EXIT     # disarm to avoid re-entry
  printf '\n' >&2
  warn "interrupted — tearing down and killing background processes."
  _cleanup
  pkill -P $$ 2>/dev/null || true
  exit 130
}

# Ensure the service user has passwordless sudo, persisted in
# /etc/sudoers.d/010-weyland-nopasswd. Raspberry Pi OS already grants the first
# user NOPASSWD, so the common case is silent (no prompt — the wizard URL stays
# the first and only output). Only a Pi WITHOUT passwordless sudo gets one
# clearly-labelled prompt, up front. Idempotent; validated with visudo so a
# malformed file can never lock sudo out. Needed regardless of mode: the
# background dashboard's `sudo -n` writes (e.g. /save-pat) depend on it.
_ensure_nopasswd_sudo() {
  local u f tmp line
  u="$(id -un)"
  f="/etc/sudoers.d/010-weyland-nopasswd"
  line="${u} ALL=(ALL) NOPASSWD:ALL"
  if ! sudo -n true 2>/dev/null; then
    printf '\n  one-time: enter this Pi'"'"'s sudo password to begin the rite...\n' >&2
    sudo -v || { warn "sudo unavailable — cannot proceed"; return 1; }
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$line" > "$tmp"
  if sudo visudo -cf "$tmp" >/dev/null 2>&1; then
    sudo install -m 0440 -o root -g root "$tmp" "$f" 2>/dev/null || true
  fi
  rm -f "$tmp"
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
main() {
  # Restore the terminal on normal exit; die cleanly (killing the dashboard +
  # children) on interrupt/term/hangup — never run away in the background.
  trap _cleanup EXIT
  trap _on_signal INT TERM HUP

  # Passwordless sudo first (silent when already granted, which is the norm) so
  # nothing prompts after the URL. Then the dashboard prints the URL and (in
  # dashboard mode) silences the terminal. Nothing prints before the URL.
  _ensure_nopasswd_sudo
  load_state
  phase_dashboard_start
  sudo rm -f "$STATE_DIR/run-progress" 2>/dev/null || true
  # render_checklist drives the pinned terminal fallback (plain / no-dashboard
  # mode only — it no-ops in dashboard mode); state_phase drives the dashboard.
  render_checklist preflight running;        state_phase preflight running
  phase_preflight
  render_checklist preflight done;           state_phase preflight done
  render_checklist identity running;         state_phase identity running
  phase_identity
  render_checklist identity done;            state_phase identity done
  render_checklist packages running;         state_phase packages running
  phase_packages
  render_checklist packages done;            state_phase packages done
  render_checklist tailscale running;        state_phase tailscale running
  phase_tailscale
  render_checklist tailscale done;           state_phase tailscale done
  render_checklist github_auth running;      state_phase github_auth running
  phase_github_auth
  render_checklist github_auth done;         state_phase github_auth done
  render_checklist per_pi_repo running;      state_phase per_pi_repo running
  phase_per_pi_repo
  render_checklist per_pi_repo done;         state_phase per_pi_repo done
  render_checklist tunnel running;           state_phase tunnel running
  phase_tunnel
  render_checklist tunnel done;              state_phase tunnel done
  render_checklist claude_code running;      state_phase claude_code running
  phase_claude_code
  render_checklist claude_code done;         state_phase claude_code done
  render_checklist connector running;        state_phase connector running
  phase_connector
  render_checklist connector done;           state_phase connector done
  render_checklist vault running;            state_phase vault running
  phase_vault
  render_checklist vault done;               state_phase vault done
  render_checklist selfdoc running;          state_phase selfdoc running
  phase_selfdoc
  render_checklist selfdoc done; render_checklist summary done
  state_phase selfdoc done;                  state_phase summary running
  # Restore the terminal before printing the summary so it lands on a clean,
  # full screen; the frozen checklist above shows every phase complete.
  _checklist_teardown
  phase_summary
  state_phase summary done
}

main "$@"
