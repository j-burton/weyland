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
