"""CLI entry: `weyland-mcp`."""
from __future__ import annotations

import sys

from .server import run


def main() -> int:
    try:
        run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"weyland-mcp: fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
