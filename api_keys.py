"""API-key rotation and stable, non-secret sandbox account identifiers."""

import asyncio
import hashlib
import re
import time


class KeyInCooldownError(RuntimeError):
    """A queued creation must skip this Key without issuing an RPC."""


class ApiKeyPool:
    """Select an account only on *creation*; existing sandboxes keep their owner."""

    def __init__(self, config):
        legacy = config.get("e2b_api_key")
        legacy = legacy.strip() if isinstance(legacy, str) else ""
        raw = config.get("e2b_api_keys") or []
        if isinstance(raw, str):
            entries = re.split(r"[,\r\n]+", raw)
        elif isinstance(raw, (list, tuple)):
            entries = raw
        else:
            entries = []
        values = ([legacy] if legacy else []) + [
            item.strip() for item in entries if isinstance(item, str) and item.strip()
        ]
        self._keys = {}
        self._order = []
        for value in values:
            identifier = self.identifier(value)
            if identifier not in self._keys:
                self._keys[identifier] = value
                self._order.append(identifier)
        self.legacy_id = self.identifier(legacy) if legacy else ""
        self._cursor = 0
        self._lock = asyncio.Lock()
        self._cooldown_until = {}

    def ensure_any_available(self):
        """Check admission without consuming the next account selection."""
        if not self._order:
            raise RuntimeError("E2B API Key is missing.")
        now = time.monotonic()
        if not any(self._cooldown_until.get(key_id, 0) <= now for key_id in self._order):
            raise RuntimeError("All configured E2B API Keys are in cooldown.")

    @staticmethod
    def identifier(key):
        return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    @property
    def has_keys(self):
        return bool(self._order)

    def resolve(self, key_id=None):
        """Never silently use another account for a bound, existing sandbox."""
        if key_id:
            if key_id not in self._keys:
                raise RuntimeError(
                    "The API Key bound to this sandbox is no longer configured. "
                    "Restore the original Key or explicitly destroy the sandbox "
                    "using its original account."
                )
            return self._keys[key_id]
        if self.legacy_id:
            return self._keys[self.legacy_id]
        if not self._order:
            raise RuntimeError("E2B API Key is missing.")
        raise RuntimeError(
            "This legacy sandbox has no recorded API Key owner. "
            "Restore the original e2b_api_key to manage it safely."
        )

    async def candidates(self):
        """Round-robin order for one new sandbox, skipping cooling-down Keys."""
        if not self._order:
            raise RuntimeError("E2B API Key is missing.")
        async with self._lock:
            start = self._cursor % len(self._order)
            ordered = self._order[start:] + self._order[:start]
            now = time.monotonic()
            active = [key_id for key_id in ordered
                      if self._cooldown_until.get(key_id, 0) <= now]
            if active:
                self._cursor = (self._order.index(active[0]) + 1) % len(self._order)
        if not active:
            raise RuntimeError("All configured E2B API Keys are in cooldown.")
        return [(key_id, self._keys[key_id]) for key_id in active]

    def ensure_available(self, key_id):
        """Recheck immediately before CREATE, after capacity/lock/rate waits."""
        if self._cooldown_until.get(key_id, 0) > time.monotonic():
            raise KeyInCooldownError("This E2B API Key entered cooldown while waiting.")

    @staticmethod
    def is_definitive_account_rejection(exc, status):
        return status in (401, 402, 403, 429) or (
            type(exc).__name__ in ("AuthenticationException", "RateLimitException")
        )

    def redact(self, value):
        text = str(value)
        # A shorter configured secret must not expose the suffix of a longer one.
        for secret in sorted(self._keys.values(), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def reject(self, key_id, status):
        # Avoid hammering accounts with exhausted quotas or explicit rate limits.
        cooldown = 60 if status == 429 else 300
        self._cooldown_until[key_id] = time.monotonic() + cooldown
