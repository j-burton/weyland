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
   tmux session named after the Pi). chat-Claude and CC do **not** share
   a conversation — you communicate by **relay**:
   1. **Write a handoff doc** — `write_file` the task into the per-Pi
      repo under `handoffs/` (durable, reviewable, survives context limits).
   2. **Send CC to read it** —
      `tmux_send_keys(session=<pi>, keys="Read <repo>/handoffs/<doc>.md and execute", enter=true)`.
   3. **Read the pane back** — `tmux_capture_pane` (or
      `tmux capture-pane -t <pi> -p -S -N` via the shell verb) to follow
      CC's progress and pick up its results or questions.
   For quick exchanges, skip the doc and send-keys a short prompt
   directly. Prefer delegating a whole task in one instruction over
   hand-driving every step — CC is local and far faster at multi-step
   work.
3. **WAKE SYSTEM** — watcher + `cc-notify` ping chat-Claude with
   `[HAL 9000 STANDING BY]` when CC finishes, and escalate to Julian's
   phone only on the final shot if CC stalls. Controlled by
   `/etc/weyland/wake-mode` (on/off). **Works from anywhere — not just the
   Pi's LAN:** the PC ping POSTs to Julian's PC by its Tailscale (MagicDNS)
   hostname, so the wake loop reaches him wherever he is.

**Standing rule:** whenever you start driving a Pi's tmux/CC, offer
Julian one copy-paste command to attach read-only —
`tmux attach -t <pi> -r` if he's on the Pi, or, **from anywhere**,
`ssh -t admin@<pi-tailscale-name> 'tmux attach -t <pi> -r'` using the
Pi's Tailscale MagicDNS name (works off-LAN, not just the local network).
Detach is `Ctrl-b` then `d`.

## Re-arm the watcher — ONLY after CC completes a task

When you dispatch a task to the CC on a minion, **after CC finishes**,
re-arm the wake system on that minion in the same turn (`restart_wake`,
or flip `wake-mode` off then on). This applies to every minion you drive.

**Restart the watcher only at that moment — never otherwise.** Do NOT
restart it in response to a ping, and not between instructions to CC while
a task is still running. Each restart resets the shot counter to 1, so a
mid-ladder restart throws away the escalation already in progress and the
ladder never reaches the final shot that pages Julian. Re-arm = "this task
is done, arm for the next one," nothing else.

## End of a long session — always read the pane

At the end of a long session, **always capture and read the CC pane to
confirm CC's final state, regardless of whether a ping arrived.** Pings can
be missed or suppressed (`wake-mode=off`, a watcher hiccup, debounce, or you
simply didn't act on the PC shots), so never treat "no ping" as "nothing to
see." A direct pane read is the only reliable end-of-session check.

## The vault — fleet secrets

Fleet-wide secrets (the Pushcut webhook, etc.) live in the private
`j-burton/weyland-secrets` repo and are fetched during the bootstrap's vault
phase via the PAT. Operators don't manage them per-Pi — adding a secret to
that repo's `secrets.env` distributes it to every future minion
automatically, and rotating one is a single edit there.

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
