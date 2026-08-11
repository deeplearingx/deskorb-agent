"""Compatibility entry point for the public Bing FastAPI acceptance scenario.

The shared probe emits privacy-safe metrics only; it never prints page content
or the model's final answer.
"""
from __future__ import annotations

from e2e_public_browser_probe import main as public_browser_main


def main() -> int:
    return public_browser_main(["--live", "--scenario", "bing-fastapi"])


if __name__ == "__main__":
    raise SystemExit(main())
