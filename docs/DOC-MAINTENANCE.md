# Doc Maintenance Manual

**Read this before changing or recording anything about a Pi or the fleet, and
run the Definition of Done (§5) before calling any such work "done."** It is the
single source for *which docs exist, which are central vs copied, and what to
update when*. It exists because the doc set drifted — FLEET.md went stale and
copies disagreed — for want of one place that says what-to-touch-and-where.

Companion docs (this manual points at them; it does not duplicate them):
- `DESIGN.md` — *why* the fleet and doc structure are the way they are.
- `OPERATING.md` — how to run weyland / per-Pi sessions.
- per-Pi `README.md` — a chat's orientation on a specific Pi.

## 1. The doc map — where everything lives

### weyland (public repo `j-burton/weyland`) — masters & engine
| Path | What | Canonical here? |
|---|---|---|
| `bootstrap/` | install.sh (bootstrap), reset.sh (wipe), dashboard.py (wizard), cc-status.sh | yes |
| `connector/` | weyland-mcp source | yes |
| `templates/` | **masters** of the per-Pi standard docs: README, IDENTITY, CURRENT_STATE, MODULES, HARDWARE, handoffs/, recreate/ | yes (bootstrap rolls them out) |
| `docs/` | weyland's own docs, incl. this file | yes |

`templates/` deliberately has **no FLEET.md** — the fleet map is vault-canonical
because it carries internal IPs and weyland is public.

### vault (private repo `j-burton/weyland-secrets`) — secrets & fleet map
| File | What | Canonical here? |
|---|---|---|
| `secrets.env` | every fleet secret (KEY=value) | yes — single copy |
| `README.md` | glossary for secrets.env + access notes | yes — single copy |
| `FLEET.md` | the fleet map: roster, topology/IPs, services, wake | **yes** — but currently **also copied into every per-Pi repo** (the drift trap) |

### per-Pi repo (private, one per Pi — e.g. `j-burton/sunflower-homebridge`)
| File | What | Source of truth |
|---|---|---|
| `IDENTITY.md` | name, KIND (minion/golem), purpose, domain, MCP URL | **this Pi** (unique) |
| `CURRENT_STATE.md` | running / broken / in-flight / changelog | **this Pi** (unique) |
| `MODULES.md` | per-service inventory | **this Pi** (unique) |
| `HARDWARE.md` | hardware spec | **this Pi** (unique) |
| `handoffs/`, `recreate/` | task docs, rebuild bundle | **this Pi** (unique) |
| `README.md` | orientation manual | **copy** of weyland `templates/README.md` |
| `FLEET.md` | fleet map | **copy** of vault `FLEET.md` |

## 2. Two kinds of doc — and the rule that matters

- **Per-Pi unique** (IDENTITY / CURRENT_STATE / MODULES / HARDWARE / handoffs / recreate):
  describe one box. Touch only the Pi that changed. **Never propagate** — copying
  one Pi's state onto the others is actively wrong.
- **Fleet-standard, duplicated** (README, FLEET.md): identical everywhere.
  **Editing one copy and stopping is the bug.** Edit the canonical, then push the
  same change into *every* per-Pi copy.
  - README canonical → weyland `templates/README.md`.
  - FLEET.md canonical → vault `FLEET.md`.
- **Central single-copy** (secrets.env, vault README, weyland docs/bootstrap/connector/templates):
  one place, no propagation.

**Live facts are never stored in docs.** A doc may state a Pi's hardware *ceiling*
(durable); never its *current* free RAM / load / disk (stale within minutes).
Headroom is **measured on demand**, not read from a doc.

## 3. Triggers → what to update

| You changed… | Update… |
|---|---|
| a service / config on a Pi | that Pi's `CURRENT_STATE.md` + `MODULES.md` |
| a Pi's hardware | that Pi's `HARDWARE.md` |
| a Pi's role / kind / purpose / identity | that Pi's `IDENTITY.md` **+** the fleet map entry |
| added / removed / repurposed a Pi | the fleet map (→ all copies) + create/retire its per-Pi repo |
| the orientation manual (README) | weyland `templates/README.md` → **propagate to every per-Pi README** |
| weyland behaviour (bootstrap / connector / wake) | weyland `docs/` (DESIGN, OPERATING, NEW_PI, connector README, templates README) — per OPERATING.md |
| a fleet secret (add / rotate) | vault `secrets.env` + vault `README.md` glossary |
| renamed a repo | GitHub rename + that Pi's `mcp.env` (`WEYLAND_PI_REPO`/`DIR`) + local clone dir + git remote + fleet map entry (+ bootstrap naming if scheme-wide) |

## 4. Propagation — rolling out a duplicated doc

1. Edit the **canonical** copy first (README → weyland/templates; FLEET.md → vault).
2. Push the *same* change into **every** per-Pi copy. Miss one and they disagree again.
3. With N Pis that's N× the same mechanical edit — fast, but easy to skip the last one.
   The Definition of Done is the backstop.

## 5. Definition of Done — run before you say "done"

- [ ] Which Pi(s) did this touch? Their `CURRENT_STATE` / `MODULES` (+ `HARDWARE` / `IDENTITY` if relevant) updated?
- [ ] Did the fleet change (Pi added/removed/repurposed, topology, a convention)? Fleet map updated **and every copy**?
- [ ] Did a fleet-standard doc or weyland behaviour change? Canonical edited in weyland **and** weyland's own docs updated?
- [ ] Did a secret change? `secrets.env` + glossary updated?
- [ ] Committed **and pushed** in every place it lives? Verified (git status clean / `whoami` / re-read)?
- [ ] Anything you couldn't update now → recorded in a `handoffs/` doc so it isn't lost.

## 6. Target design (AGREED — BUILT 2026-06-03)

The duplication in §1 (FLEET.md copied to every Pi) is the root of both the drift
and the per-Pi update cost. Agreed direction:

- **Central, per-site registries.** Replace the single FLEET.md-copied-everywhere
  with one central registry **per site** (Anchorage, NZ), living in the vault —
  single copy each, no per-Pi copies. Each holds a *brief, durable* summary per Pi:
  name, KIND, role, domain/MCP/tailnet, hardware ceiling, what it is committed to
  running, last-verified date — plus that site's network/services.
- **Placement workflow.** "Install X at site S" → open S's registry → narrow
  candidates by role + ceiling → **live-probe** the finalist for actual current
  headroom → recommend (use Pi N, or "spin up a new one"). The doc narrows; the
  probe decides.
- Optional one-line top index naming the sites and pointing at each registry.
- Result: a fleet change becomes **one edit**, with no copies to drift, regardless
  of fleet size.

Wiring this manual + the registries into the per-Pi README (so every chat lands on
them) is now done — a pointer lives in `templates/README.md`, and the per-Pi FLEET.md copies are redirect stubs.

## 7. Repo naming (adopted; existing migration DEFERRED)

- Scheme: **`<site>-<device>`**. Sites: `lakeshore` (Anchorage), `sunflower` (NZ).
  Prefix, so a site's repos cluster together in the listing.
- **Migrated:** `homebridge-pi` → `sunflower-homebridge` (2026-06-03).
- **Deferred** until docs + weyland are stable: `inkypi-pi`→`lakeshore-inkypi`,
  `unifiviewer-pi`→`lakeshore-unifiviewer`, `huginn-pi`→`lakeshore-huginn`, and
  updating the bootstrap's name logic so new Pis are born named this way.
- **`argos` excluded** — mission-critical, hands-off unless Julian says otherwise.

## 8. This manual

Canonical: `weyland/docs/DOC-MAINTENANCE.md` (single copy). Wired into the per-Pi
README **landing sequence** (top of file, with a fetch command) so every chat reads
it on landing — added 2026-06-03 after chats twice missed it, and the corrected
README propagated to every per-Pi repo.

## Status (2026-06-03)
- **Done:** NZ golem `homebridge` built; its per-Pi repo created and cloned; repo
  renamed to the site scheme (`sunflower-homebridge`); this manual written.
- **Done 2026-06-03:** per-site registries BUILT in the vault (`sites/lakeshore.md`,
  `sites/sunflower.md`); FLEET.md reduced to the index + fleet-wide sections; `huginn`
  + `homebridge` added to the roster; the per-Pi FLEET.md copies retired to redirect
  stubs (argos excluded); born-wired pointer added to `templates/README.md`.
- **Done 2026-06-03:** manual wired into the README landing sequence (was a single
  buried line at L501, and missing entirely from deployed copies) + fetch command;
  corrected README propagated to every per-Pi repo; new Pis inherit it via the
  bootstrap template. Fixes the "chat didn't know the manual existed" bug.
- **Still deferred (until weyland stable):** rename the Lakeshore repos
  (`inkypi-pi`→`lakeshore-inkypi`, etc.) + update bootstrap naming logic.
