"""Bounded, FIFO admission for sandboxes that are running or may be running."""

import asyncio
from collections import deque


class SandboxBusyError(RuntimeError):
    """The request cannot obtain a sandbox slot within the configured limits."""


class RuntimeCapacity:
    def __init__(self, limit=3, max_queue=10, wait_timeout=15, occupied=()):
        self.limit = limit
        self.max_queue = max_queue
        self.wait_timeout = wait_timeout
        self.occupied = set(occupied)
        self._condition = asyncio.Condition()
        self._waiters = deque()

    @property
    def waiting(self):
        return len(self._waiters)

    async def acquire(self, session_id):
        """Reserve before create/connect; keep the reservation until confirmed stopped."""
        async with self._condition:
            if session_id in self.occupied:
                return
            if not self._waiters and len(self.occupied) < self.limit:
                self.occupied.add(session_id)
                return
            if len(self._waiters) >= self.max_queue:
                raise SandboxBusyError("Sandbox queue is full. Please try again later.")

            ticket = object()
            self._waiters.append(ticket)
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self._waiters[0] is ticket
                        and len(self.occupied) < self.limit
                    ),
                    timeout=self.wait_timeout,
                )
                self.occupied.add(session_id)
            except asyncio.TimeoutError as exc:
                raise SandboxBusyError(
                    "Sandbox queue wait timed out. Please try again later."
                ) from exc
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    async def release(self, session_id):
        async with self._condition:
            self.occupied.discard(session_id)
            self._condition.notify_all()

    async def configure(self, limit, max_queue, wait_timeout):
        async with self._condition:
            self.limit = limit
            self.max_queue = max_queue
            self.wait_timeout = wait_timeout
            self._condition.notify_all()
