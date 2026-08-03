"""Browser task-space ownership for DeskOrb's browser backends.

The browser implementation may change, but a user task needs one stable rule:
the agent and user never drive the same space at the same time.  This module
keeps that rule local and independent of an MCP server's behaviour.
"""
from __future__ import annotations

import secrets
import time
import hashlib
from dataclasses import dataclass
from enum import Enum


class BrowserOwner(str, Enum):
    AGENT = "agent"
    USER = "user"


class BrowserSpaceState(str, Enum):
    ACTIVE = "active"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BROKEN = "broken"


@dataclass(frozen=True)
class BrowserTaskSpace:
    space_id: str
    task_id: str
    backend: str
    owner: BrowserOwner
    state: BrowserSpaceState
    created_at: float
    updated_at: float
    checkpoint: str | None = None
    last_observed_at: float | None = None


class BrowserTaskSpaces:
    """In-memory browser task spaces with explicit user-to-agent takeover."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._spaces: dict[str, BrowserTaskSpace] = {}
        self._by_task: dict[str, str] = {}

    def create(self, task_id: str, backend: str = "isolated-playwright") -> BrowserTaskSpace:
        existing = self.for_task(task_id)
        if existing and existing.state in {BrowserSpaceState.ACTIVE, BrowserSpaceState.WAITING_HUMAN}:
            return existing
        now = self._clock()
        space = BrowserTaskSpace("B" + secrets.token_hex(4).upper(), str(task_id), str(backend),
                                 BrowserOwner.AGENT, BrowserSpaceState.ACTIVE, now, now)
        self._spaces[space.space_id] = space
        self._by_task[space.task_id] = space.space_id
        return space

    def for_task(self, task_id: str | None) -> BrowserTaskSpace | None:
        space_id = self._by_task.get(str(task_id or ""))
        return self._spaces.get(space_id) if space_id else None

    def hand_off(self, task_id: str | None) -> BrowserTaskSpace | None:
        space = self.for_task(task_id)
        if space is None or space.state not in {BrowserSpaceState.ACTIVE, BrowserSpaceState.WAITING_HUMAN}:
            return None
        return self._replace(space, owner=BrowserOwner.USER, state=BrowserSpaceState.WAITING_HUMAN)

    def take_over(self, task_id: str | None, *, user_confirmed: bool) -> BrowserTaskSpace | None:
        space = self.for_task(task_id)
        if space is None:
            return None
        if space.owner is BrowserOwner.USER and not user_confirmed:
            raise PermissionError("The user controls this browser task space. Explicit confirmation is required to continue.")
        if space.state in {BrowserSpaceState.COMPLETED, BrowserSpaceState.CANCELLED}:
            raise RuntimeError("This browser task space is closed. Start a new task instead.")
        return self._replace(space, owner=BrowserOwner.AGENT, state=BrowserSpaceState.ACTIVE)

    def require_agent_control(self, task_id: str | None) -> None:
        space = self.for_task(task_id)
        if space is None:
            return
        if space.owner is not BrowserOwner.AGENT or space.state is not BrowserSpaceState.ACTIVE:
            raise PermissionError("Browser task space is under user control. Wait for the user to confirm continuation.")

    def close(self, task_id: str | None, *, cancelled: bool = False) -> BrowserTaskSpace | None:
        space = self.for_task(task_id)
        if space is None:
            return None
        state = BrowserSpaceState.CANCELLED if cancelled else BrowserSpaceState.COMPLETED
        return self._replace(space, state=state)

    def save_checkpoint(self, task_id: str | None, fingerprint: str) -> BrowserTaskSpace | None:
        """Store only a short digest of a trusted page observation."""
        space = self.for_task(task_id)
        if space is None:
            return None
        digest = hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()[:16]
        return self._replace(space, checkpoint=digest, last_observed_at=self._clock())

    def checkpoint_matches(self, task_id: str | None, fingerprint: str) -> bool:
        space = self.for_task(task_id)
        if space is None or not space.checkpoint:
            return False
        digest = hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()[:16]
        return digest == space.checkpoint

    def mark_broken(self, task_id: str | None) -> BrowserTaskSpace | None:
        space = self.for_task(task_id)
        if space is None or space.state in {BrowserSpaceState.COMPLETED, BrowserSpaceState.CANCELLED}:
            return None
        return self._replace(space, state=BrowserSpaceState.BROKEN)

    def _replace(self, space: BrowserTaskSpace, *, owner: BrowserOwner | None = None,
                 state: BrowserSpaceState | None = None, checkpoint: str | None = None,
                 last_observed_at: float | None = None) -> BrowserTaskSpace:
        replacement = BrowserTaskSpace(space.space_id, space.task_id, space.backend,
                                       owner or space.owner, state or space.state,
                                       space.created_at, self._clock(),
                                       space.checkpoint if checkpoint is None else checkpoint,
                                       space.last_observed_at if last_observed_at is None else last_observed_at)
        self._spaces[replacement.space_id] = replacement
        return replacement
