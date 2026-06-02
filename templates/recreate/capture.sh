#!/usr/bin/env bash
# weyland recreate/capture.sh — re-runnable. Refreshes THIS Pi's recreate bundle
# from the live system so it can't rot. Uniform across the fleet (lives in weyland).
# Per-Pi data: capture.list (which custom files to grab) + optional capture-extra.sh
# (app-specific reference dumps, e.g. Home Assistant device registry).
# NEVER captures secret values — secret-looking paths are skipped and left as pointers.
set -uo pipefail
cd "$(dirname "$0")" || exit 1            # -> recreate/
PI="$(hostname)"; TS="$(date -Iseconds)"
mkdir -p provisioning reference

echo "[capture] $PI @ $TS"

# 1) SOFTWARE MANIFEST (auto-discovered) -> MANIFEST.auto.md (curated view stays in MANIFEST.md)
{
  echo "# $PI — auto-captured software manifest"
  echo "_generated $TS by capture.sh; curated/patterned view lives in MANIFEST.md_"
  echo; echo "## OS"; . /etc/os-release 2>/dev/null; echo "- ${PRETTY_NAME:-unknown}"
  echo; echo "## apt (manually installed)"; apt-mark showmanual 2>/dev/null | sort | sed 's/^/- /'
  echo; echo "## pip"; { pip3 list --format=freeze 2>/dev/null; pip list --format=freeze 2>/dev/null; } | sort -u | sed 's/^/- /'
  echo; echo "## docker"
  if command -v docker >/dev/null 2>&1; then
    docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | sed 's/^/- img /'
    docker ps -a --format '{{.Image}} ({{.Names}})' 2>/dev/null | sed 's/^/- ctr /'
  else echo "- (no docker)"; fi
  echo; echo "## enabled systemd units"
  systemctl list-unit-files --state=enabled 2>/dev/null | awk 'NR>1 && $1 ~ /\.(service|timer)$/ {print "- "$1}'
} > MANIFEST.auto.md
echo "[capture] wrote MANIFEST.auto.md"

# 2) PROVISIONING — copy curated custom files from capture.list ("src [destname]" per line)
SECRET_RX='(\.env$|\.key$|\.pem$|id_rsa|_creds(\.|$)|\.unifi_creds|secret|passwd|password|/\.netrc|session\.json$|cookies\.json$|\.token(\.|$)|api[_-]?key)'
if [ -f capture.list ]; then
  while read -r src dest _; do
    case "${src:-}" in ''|\#*) continue;; esac
    if echo "$src" | grep -qiE "$SECRET_RX"; then echo "[capture] SKIP secret-looking (pointer only): $src"; continue; fi
    [ -z "${dest:-}" ] && dest="$(basename "$src")"
    if [ -e "$src" ]; then cp -a "$src" "provisioning/$dest" && echo "[capture] + $src -> provisioning/$dest"
    else echo "[capture] MISSING (listed, not on disk): $src"; fi
  done < capture.list
else
  echo "[capture] no capture.list yet — add one (see template) to grab custom files"
fi

# 3) APP-SPECIFIC reference dumps (optional per-Pi hook; must itself avoid writing secrets)
if [ -x capture-extra.sh ]; then echo "[capture] running capture-extra.sh"; ./capture-extra.sh; fi

echo "[capture] done @ $TS — review 'git status' before committing"
