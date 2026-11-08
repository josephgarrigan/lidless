# ==============================================================================
# Windmill Flow: decision_cache (In-Memory Decision Cache & Hysteresis Manager)
# ==============================================================================
# L-011. Neutralizes measured non-determinism where it would cause visible
# flapping on identical derived states within a TTL window.
#
# Constraints & Invariants:
#   1. Memory Bounded: Strict LRU eviction capped at `max_entries` (default 1000)
#      to respect the <100 MB RAM budget of the triad stack.
#   2. Keyed on Derived State: Keyed on (tenant_id, container, state_fingerprint),
#      never the raw un-sanitized event envelope.
#   3. Never Masks New Faults: Cache keys and hysteresis tracking are scoped by
#      container and fault class, so a new fault class on a flapping container
#      is never masked or suppressed.
#   4. Observable: Cache hits and misses are observable in Decision.cache_hit.
#   5. Hysteresis: An action already taken for a (tenant, container, fault_class)
#      inside the window is not re-taken merely because a subsequent call moved
#      a probability across the line.

from __future__ import annotations

import copy
import time
from collections import OrderedDict
from typing import Any, Optional, Tuple


class DecisionCache:
    """Bounded in-memory LRU cache and hysteresis engine for the decision gate."""

    def __init__(
        self,
        ttl_seconds: float = 60.0,
        max_entries: int = 1000,
        hysteresis_window_seconds: float = 300.0,
        time_fn: Any = None,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self.hysteresis_window_seconds = float(hysteresis_window_seconds)
        self._time = time_fn or time.time
        # Map: (tenant_id, container, fingerprint) -> (timestamp, Decision)
        self._cache: OrderedDict[Tuple[str, str, str], Tuple[float, Any]] = OrderedDict()
        # Map: (tenant_id, container, fault_class) -> last_action_timestamp
        self._action_history: OrderedDict[Tuple[str, str, str], float] = OrderedDict()

    def get(self, tenant_id: str, container: str, fingerprint: str) -> Optional[Any]:
        """Retrieve a cached decision if present and unexpired."""
        key = (tenant_id, container, fingerprint)
        if key not in self._cache:
            return None

        inserted_at, decision = self._cache[key]
        now = self._time()
        if now - inserted_at > self.ttl_seconds:
            del self._cache[key]
            return None

        # Promote to most recently used
        self._cache.move_to_end(key)
        cached = copy.deepcopy(decision)
        cached.cache_hit = True
        return cached

    def put(self, tenant_id: str, container: str, fingerprint: str, decision: Any) -> None:
        """Store a decision in the cache, evicting the oldest if capacity is exceeded."""
        key = (tenant_id, container, fingerprint)
        now = self._time()

        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (now, copy.deepcopy(decision))

        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

    def record_action(self, tenant_id: str, container: str, fault_class: str) -> None:
        """Record the timestamp of an authorized remediation action taken."""
        key = (tenant_id, container, fault_class or "unknown")
        now = self._time()
        if key in self._action_history:
            self._action_history.move_to_end(key)
        self._action_history[key] = now

        while len(self._action_history) > self.max_entries:
            self._action_history.popitem(last=False)

    def is_action_suppressed_by_hysteresis(
        self, tenant_id: str, container: str, fault_class: str
    ) -> bool:
        """Check if remediation is in the cooldown window for this (tenant, container, fault_class)."""
        key = (tenant_id, container, fault_class or "unknown")
        if key not in self._action_history:
            return False

        last_action_at = self._action_history[key]
        now = self._time()
        if now - last_action_at > self.hysteresis_window_seconds:
            del self._action_history[key]
            return False

        return True

    def clear(self) -> None:
        """Clear cache and action history."""
        self._cache.clear()
        self._action_history.clear()

    @property
    def size(self) -> int:
        """Current number of active cache entries."""
        return len(self._cache)

    @property
    def action_history_size(self) -> int:
        """Current number of tracked action histories."""
        return len(self._action_history)


# Module-level default instance
GLOBAL_CACHE = DecisionCache()
