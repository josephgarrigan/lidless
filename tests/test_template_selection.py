#!/usr/bin/env python3
"""L-012: Semantic Template Selection unit tests.

Verifies:
  1. Typed fault_class drives template selection, breaking first-match-wins ordering.
  2. Ambiguous multi-pattern messages resolve to the semantic fault_class rather than whichever regex appeared first.
  3. 'unclassified' produces a distinct novel fault headline and guidance, never a generic 500 narrative.
  4. Offline / gate unavailable falls back to regex matching.
  5. Executive verbosity works across semantic selections.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows import anecdote_engine as ae

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def make_event(raw_message: str, container: str = "payment_gateway", tenant: str = "fintech") -> dict:
    return {
        "tenant_id": tenant,
        "event_meta": {
            "status_code": 500,
            "raw_message": raw_message,
            "timestamp": "2026-10-31T01:00:00Z",
        },
        "host_meta": {"container": container, "hostname": "node-01"},
    }


def main() -> int:
    # -------------------------------------------------------------------------
    print("1. Ambiguous multi-pattern ordering resolution:")
    # This message contains:
    #   - "could not connect to server" (DB_CONNECTION_EXHAUSTION)
    #   - "token signature invalid" (AUTH_SECURITY_REJECTION)
    ambiguous_msg = "could not connect to server: token signature invalid"
    ev_ambiguous = make_event(ambiguous_msg)

    # Without gate (regex first-match-wins): DB pattern comes first
    anec_regex = ae.synthesize_anecdote(ev_ambiguous)
    check("regex fallback picks first matching DB pattern",
          anec_regex["anecdote_id"] == "DB_CONNECTION_EXHAUSTION")

    # With gate semantic fault_class: overrides ordering and selects AUTH
    anec_auth = ae.synthesize_anecdote(ev_ambiguous, fault_class="auth_security_rejection")
    check("semantic fault_class selects AUTH_SECURITY_REJECTION",
          anec_auth["anecdote_id"] == "AUTH_SECURITY_REJECTION")
    check("headline reflects AUTH pattern", "Authentication / Security" in anec_auth["headline"])

    # Decision object injection works identically
    mock_dec = SimpleNamespace(fault_class="auth_security_rejection", evaluated=True, action="none", reason_code="gate_disabled")
    anec_dec = ae.synthesize_anecdote(ev_ambiguous, decision=mock_dec)
    check("decision object fault_class drives template selection",
          anec_dec["anecdote_id"] == "AUTH_SECURITY_REJECTION")

    # -------------------------------------------------------------------------
    print("\n2. Unclassified novel fault handling:")
    ev_novel = make_event("disk quota exceeded on volume /mnt/storage")

    anec_unclass = ae.synthesize_anecdote(ev_novel, fault_class="unclassified")
    check("unclassified fault id is UNCLASSIFIED_RUNTIME_FAULT",
          anec_unclass["anecdote_id"] == "UNCLASSIFIED_RUNTIME_FAULT")
    check("headline announces unrecognized runtime fault",
          "Unrecognized Runtime Fault" in anec_unclass["headline"])
    check("narrative cites unclassified runtime event",
          "unclassified runtime event" in anec_unclass["narrative"])
    check("action_required is False for unclassified novel events",
          anec_unclass["action_required"] is False)

    # -------------------------------------------------------------------------
    print("\n3. Full taxonomy coverage under semantic selection:")
    taxonomy_cases = {
        "db_connection_exhaustion": "DB_CONNECTION_EXHAUSTION",
        "oom_memory_ceiling": "OOM_MEMORY_CEILING",
        "auth_security_rejection": "AUTH_SECURITY_REJECTION",
        "upstream_timeout": "UPSTREAM_TIMEOUT",
        "runtime_panic": "RUNTIME_PANIC",
        "bridge_sync_fault": "BRIDGE_SYNC_FAULT",
    }
    dummy_event = make_event("arbitrary application log text")

    for fclass, expected_id in taxonomy_cases.items():
        res = ae.synthesize_anecdote(dummy_event, fault_class=fclass)
        check(f"fault_class '{fclass}' maps to '{expected_id}'", res["anecdote_id"] == expected_id)

    # -------------------------------------------------------------------------
    print("\n4. Executive verbosity under semantic selection:")
    exec_anec = ae.synthesize_anecdote(ev_ambiguous, verbosity="executive", fault_class="upstream_timeout")
    check("executive narrative generated for semantic selection",
          "upstream dependency delay" in exec_anec["narrative"].lower())

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall semantic template selection checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
