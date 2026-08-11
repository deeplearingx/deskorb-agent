"""Compatibility data model for the legacy deterministic E2E control group.

The current production runtime routes providers through ``model_adapter``.
The historical 60-case control group still describes fallback targets with a
small value object, so keep that data shape isolated from production routing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FallbackTarget:
    name: str
    provider: str
    model: str
    base_url: str
    supports_tools: bool = True
