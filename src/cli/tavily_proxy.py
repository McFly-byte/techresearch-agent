"""Local Tavily key-pool proxy bootstrap helpers.

This module only copies already-configured secrets into the ignored key file.
It never prints key values.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from core.config import Settings, project_root

DEFAULT_PROXY_DIR = project_root() / "vendor" / "tavily_search_sub_api"


def sync_keys(target: Path, settings: Settings | None = None) -> int:
    """Atomically write the configured unique Tavily keys to *target*."""
    configured = (settings or Settings()).tavily_key_pool()
    if not configured:
        raise RuntimeError("TAVILY_API_KEY/TAVILY_API_KEYS is empty")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(configured) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return len(configured)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local Tavily key-pool proxy.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser(
        "sync-keys", help="Copy .env Tavily keys to the proxy's ignored key.txt."
    )
    sync_parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_PROXY_DIR / "key.txt",
    )
    args = parser.parse_args()
    if args.command == "sync-keys":
        count = sync_keys(args.target)
        print(f"Tavily proxy key file synchronized: configured_key_count={count}")


if __name__ == "__main__":
    main()
