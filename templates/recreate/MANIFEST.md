# <pi> — recreate manifest (curated)
Target OS: <fill>. Hardware: see HARDWARE.md. Auto-discovered list: MANIFEST.auto.md (run capture.sh).

| software | pattern | source / version | purpose |
|---|---|---|---|
| weyland base layer | P1 | weyland bootstrap (install.sh) | connector, CC, wake, tunnel, tailscale |
| <app> | <P?> | <apt / pip / repo+commit> | <what it does> |

Patterns: **P1** reinstall from upstream · **P2** reinstall + reapply captured files (provisioning/) · **P3** stateful app + native backup + reference inventory · **P4** secret/session = pointer (secrets.md) + re-auth by hand.
