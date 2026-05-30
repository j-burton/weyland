# Operating weyland

How chat-Claude runs weyland sessions.

## When you're in a fresh chat aimed at weyland itself

(i.e. Julian opened a chat in a project pointed at the weyland repo,
not at a specific minion)

1. Read this file, plus DESIGN.md and NEW_PI.md.
2. Ask Julian what he wants to do.

Common requests:

- **"I want a new minion"** → read NEW_PI.md; copy the one-liner; tell
  Julian where to SSH in and what to paste.
- **"Bug in the bootstrap"** → clone the repo locally on whatever
  minion has the connector + git auth; make the fix; commit + push.
- **"Add a feature to weyland"** → same as the bug case but with more
  thought. Don't push speculative features. Stick to what's needed.

## When you're in a chat aimed at a specific minion

(i.e. Julian opened a chat in a project pointed at a per-Pi repo like
`coffee-pi`)

That minion's own README.md is your orientation. This file is for
weyland-level work, not per-minion work.

## Your tools for driving a Pi (the three-tool model)

Every per-Pi chat has THREE distinct tools. The per-Pi README spells
this out for a fresh chat-Claude; the short version, for reference:

1. **CONNECTOR (MCP)** — your own hands on the Pi. Direct verbs:
   `read_file`/`write_file`/`list_dir`/`glob`, `run_command`/`run_shell`
   (full sudo), `systemctl_*`/`install_unit`, `git_*`, and the tmux
   verbs. Default-allow — use it directly for routine actions, no
   asking.
2. **CHANNEL** — TWO-WAY comms with the CC running on the Pi (in a
   tmux session named after the Pi). SEND with
   `tmux_send_keys(session=<pi>, keys=..., enter=true)`; READ the pane
   back with `tmux_capture_pane` or `tmux capture-pane -t <pi> -p -S -N`
   via the shell verb. For long instructions, `write_file` a handoff
   doc then send `read <path> and execute`. Prefer delegating a whole
   task in one instruction over hand-driving every step — it's far
   faster.
3. **WAKE SYSTEM** — watcher + `cc-notify` ping chat-Claude with
   `[HAL 9000 STANDING BY]` when CC finishes, and escalate to Julian's
   phone if CC stalls. Controlled by `/etc/weyland/wake-mode` (on/off).

**Standing rule:** whenever you start driving a Pi's tmux/CC, offer
Julian one copy-paste command to attach read-only —
`tmux attach -t <pi> -r` if he's on the Pi, or
`ssh -t admin@<pi-addr> 'tmux attach -t <pi> -r'` from his PC. Detach
is `Ctrl-b` then `d`.

## Re-arm the watcher after every CC task

When you dispatch a task to the CC on a minion, after CC finishes,
re-arm the wake system on that minion in the same turn. Flip the
mode off then on. This applies to every minion you drive.

## Don't pre-empt Julian's stopping decisions

Never tell Julian to stop, pause, or take a break. He decides when
to stop. If you reach a natural stopping point and want to ask if
he'd like to continue, that's fine — but don't suggest stopping.

## The PAT trick

Some sessions involve scaffolding work where chat-Claude drives CC on
one Pi (say, Argos) to make commits to another repo (say, weyland).
The pattern is:

1. Generate a fine-grained PAT on GitHub, 7-day expiry.
2. Embed it in the clone URL: `https://<TOKEN>@github.com/owner/repo.git`.
3. CC clones, makes commits, pushes — auth happens via the embedded token.
4. When done, delete the working tree (which removes `.git/config` and
   thus the token).
5. Revoke the PAT on GitHub.

The token is in plaintext on the Pi only as long as the working tree
exists. Acceptable for short-lived scaffolding work.
