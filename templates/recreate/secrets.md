# <pi> — secrets & sessions (POINTERS ONLY — never commit values)
List what must be re-supplied at rebuild and where it comes from. NO actual secrets in this repo.
- **<path>** — <what it is>. Source: weyland vault. <how to restore / regenerate>
- **/etc/weyland/wake.env** — PC wake channel. `PC_WAKE_URL` is the fleet PC listener (e.g. `http://<pc>.<tailnet>.ts.net:7777`, same on every minion); `WAKE_TOKEN` is the shared fleet token (vault). If either is blank after a rebuild, the in-chat `[HAL 9000 STANDING BY]` wake won't fire (Pushcut-only) — restore both.
