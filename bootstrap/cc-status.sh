#!/usr/bin/env bash
# weyland Claude Code status line — context-window usage.
#
# Claude Code pipes a JSON blob on stdin (see `statusLine` in settings.json) and
# renders whatever single line we print at the bottom of the pane. We show the
# context-window usage as a percentage at ALL times so the level is visible at
# a glance; the watcher still only ALERTS once it crosses 60/70/80%.
#
# The percentage comes straight from `context_window.used_percentage` (already
# computed by Claude Code, input+cache tokens only). If a CC version omits it we
# fall back to the token breakdown; if neither is present we print nothing.
#
# The literal token `ctx NN%` is also what cc-tmux-watcher greps out of the pane
# to fire its 60/70/80/90% Pushcut alerts — keep that substring stable.
#
# jq is NOT a dependency on minions (it may be absent); python3 always is. The
# program is passed via -c so the script's stdin stays the JSON Claude Code piped
# in. The python below deliberately contains no single quotes (it is single-
# quoted for the shell).
python3 -c '
import json
import sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)

cw = d.get("context_window") or {}
pct = cw.get("used_percentage")

# Fallback: compute from the token breakdown the way Claude Code does
# (input + cache tokens, NOT output) when used_percentage is absent/null.
if pct is None:
    cu = cw.get("current_usage") or {}
    size = cw.get("context_window_size") or 0
    used = (cu.get("input_tokens", 0)
            + cu.get("cache_creation_input_tokens", 0)
            + cu.get("cache_read_input_tokens", 0))
    if size:
        pct = used / size * 100.0

if pct is None:
    sys.exit(0)
try:
    pct = float(pct)
except (TypeError, ValueError):
    sys.exit(0)

# Always shown so the context level is visible in the pane at a glance; the
# watcher still only ALERTS at 60/70/80%, this is just the on-screen readout.
p = int(pct)  # floor to a whole number
if p > 100:
    p = 100

# A colour dot that escalates with the fill, so it reads at a glance. The dot is
# cosmetic; the plain-ASCII "ctx NN%" is the part the watcher greps, so it must
# survive even if the terminal cannot render the glyph.
if p >= 90:
    dot = "\U0001F534"      # red
elif p >= 80:
    dot = "\U0001F7E0"      # orange
elif p >= 70:
    dot = "\U0001F7E1"      # yellow
else:
    dot = "\U0001F7E2"      # green

print(dot + " ctx " + str(p) + "%")
'
