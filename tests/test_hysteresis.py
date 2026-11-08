#!/usr/bin/env python3
"""L-011: Hysteresis and Decision Cache unit tests.

Verifies:
  1. Replaying identical events hits the cache, preserves decision_id, and marks cache_hit=True.
  2. Cache expiry forces a fresh call after TTL.
  3. Strict LRU capacity bound (<100MB RAM budget protection).
  4. Hysteresis suppresses repeated actions within the cooldown window.
  5. Hysteresis and caching never mask a new fault class on the same container.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_decision_gate as tdg
from windmill.flows import decision_gate as dg
from windmill.flows.decision_cache import DecisionCache

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


class CountingClient:
    def __init__(self, answer_generator=None):
        self.calls = 0
        self.generator = answer_generator or (lambda: tdg.answers())

    def system_one(self, state=None, questions=None, model=None):
        self.calls += 1
        answers = self.generator()
        return SimpleNamespace(
            model="jev-1.13.0",
            answers=answers,
            usage=SimpleNamespace(input_tokens=700, output_tokens=150),
        )


def make_event(raw_message: str, container: str = "payment_gateway", tenant: str = "fintech") -> dict:
    return {
        "tenant_id": tenant,
        "org_id": "acme",
        "severity": "critical",
        "is_error": True,
        "tenant": {"id": tenant, "environment": "production"},
        "event_meta": {
            "is_error": True,
            "severity": "critical",
            "status_code": 500,
            "stream": "stderr",
            "raw_message": raw_message,
            "timestamp": "2026-10-31T01:00:00Z",
        },
        "host_meta": {"container": container, "hostname": "node-01"},
    }


def main() -> int:
    policy = tdg.policy()
    # Make sure gate is enabled and remediation is permitted
    policy["auto_remediation"]["enabled"] = True
    policy["decision_gate"]["enabled"] = True
    policy["decision_gate"]["mode"] = "enforce"

    simulated_now = 1000.0
    cache = DecisionCache(
        ttl_seconds=60.0,
        max_entries=3,
        hysteresis_window_seconds=300.0,
        time_fn=lambda: simulated_now,
    )

    ev1 = make_event("fatal error: runtime: out of memory", container="worker-1")
    ev2 = make_event("fatal error: runtime: out of memory", container="worker-1")

    # -------------------------------------------------------------------------
    print("1. Replaying identical events:")
    client1 = CountingClient()
    d1 = dg.evaluate(ev1, policy, client=client1, schema=tdg.SCHEMA, cache=cache)
    check("first evaluation calls client", client1.calls == 1, f"calls={client1.calls}")
    check("first decision marks cache_hit False", d1.cache_hit is False)

    d2 = dg.evaluate(ev2, policy, client=client1, schema=tdg.SCHEMA, cache=cache)
    check("second evaluation hits cache without extra call", client1.calls == 1, f"calls={client1.calls}")
    check("second decision marks cache_hit True", d2.cache_hit is True)
    check("decisions have identical decision_id", d1.decision_id == d2.decision_id)
    check("decisions have identical action", d1.action == d2.action)
    check("decisions have identical reason_code", d1.reason_code == d2.reason_code)

    # -------------------------------------------------------------------------
    print("\n2. Cache expiry after TTL:")
    simulated_now += 61.0  # past 60s TTL
    d3 = dg.evaluate(ev2, policy, client=client1, schema=tdg.SCHEMA, cache=cache)
    check("call made after TTL increments call count", client1.calls == 2, f"calls={client1.calls}")
    check("post-expiry decision marks cache_hit False", d3.cache_hit is False)
    check("post-expiry decision_id matches structural contract", d3.decision_id == d1.decision_id)

    # -------------------------------------------------------------------------
    print("\n3. Strict LRU capacity bound (<100MB RAM budget):")
    cache.clear()
    simulated_now = 2000.0
    client_lru = CountingClient()

    e_a = make_event("error A", container="c1")
    e_b = make_event("error B", container="c2")
    e_c = make_event("error C", container="c3")
    e_d = make_event("error D", container="c4")  # 4th item, should evict c1

    dg.evaluate(e_a, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)
    dg.evaluate(e_b, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)
    dg.evaluate(e_c, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)
    check("cache reaches max entries", cache.size == 3, f"size={cache.size}")

    # Access e_a so e_b becomes LRU
    dg.evaluate(e_a, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)

    # Insert 4th
    dg.evaluate(e_d, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)
    check("cache size remains bounded at max_entries", cache.size == 3, f"size={cache.size}")

    # e_b should have been evicted; e_a should still be cached
    calls_before = client_lru.calls
    dg.evaluate(e_a, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)
    check("most recently used item e_a was retained in cache", client_lru.calls == calls_before)

    dg.evaluate(e_b, policy, client=client_lru, schema=tdg.SCHEMA, cache=cache)
    check("evicted item e_b produced a fresh client call", client_lru.calls == calls_before + 1)

    # -------------------------------------------------------------------------
    print("\n4. New fault class on same container is never masked:")
    cache.clear()
    simulated_now = 3000.0
    client_diff = CountingClient(lambda: tdg.answers(choice="oom_memory_ceiling"))
    client_db = CountingClient(lambda: tdg.answers(choice="db_connection_exhaustion"))

    e_oom = make_event("fatal error: runtime: out of memory", container="svc-1")
    e_db = make_event("pq: too many connections", container="svc-1")

    d_oom = dg.evaluate(e_oom, policy, client=client_diff, schema=tdg.SCHEMA, cache=cache)
    check("oom call evaluated", client_diff.calls == 1)

    d_db = dg.evaluate(e_db, policy, client=client_db, schema=tdg.SCHEMA, cache=cache)
    check("different fault class on same container made fresh call", client_db.calls == 1)
    check("both entries stored in cache", cache.size == 2)

    # -------------------------------------------------------------------------
    print("\n5. Hysteresis cooldown suppresses repeated remediation:")
    simulated_now = 4000.0
    auth1 = dg.authorize_remediation(
        d_oom,
        policy,
        reported_severity="critical",
        restarts_this_hour=0,
        cache=cache,
        container="svc-1",
    )
    check("initial remediation is authorized", auth1["fire"] is True and auth1["reason"] == "authorized")

    # Record that action was executed
    cache.record_action("fintech", "svc-1", d_oom.fault_class)

    # Immediate second event for the same container and fault class:
    auth2 = dg.authorize_remediation(
        d_oom,
        policy,
        reported_severity="critical",
        restarts_this_hour=1,
        cache=cache,
        container="svc-1",
    )
    check("second remediation within window is suppressed by hysteresis",
          auth2["fire"] is False and auth2["reason"] == "hysteresis_suppressed")
    check("hysteresis_suppressed flag is set in response", auth2.get("hysteresis_suppressed") is True)

    # A DIFFERENT fault class on the same container is NOT suppressed by hysteresis
    auth_diff = dg.authorize_remediation(
        d_db,
        policy,
        reported_severity="critical",
        restarts_this_hour=1,
        cache=cache,
        container="svc-1",
    )
    check("different fault class is NOT suppressed by hysteresis",
          auth_diff["fire"] is True and auth_diff["reason"] == "authorized")

    # After hysteresis window (300s) expires:
    simulated_now += 301.0
    auth3 = dg.authorize_remediation(
        d_oom,
        policy,
        reported_severity="critical",
        restarts_this_hour=2,
        cache=cache,
        container="svc-1",
    )
    check("remediation authorized again after hysteresis window expires",
          auth3["fire"] is True and auth3["reason"] == "authorized")

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall hysteresis and decision-cache checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
