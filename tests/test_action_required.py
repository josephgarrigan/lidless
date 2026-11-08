#!/usr/bin/env python3
"""L-013: action_required derivation unit tests.

Verifies:
  1. action_required derived from gate judgments when gate is available.
  2. Gate authorization (thresholds_satisfied) yields action_required=True.
  3. Gate withholding (threshold_failed) yields action_required=False.
  4. Adjudicates historical inconsistencies for OOM_MEMORY_CEILING and AUTH_SECURITY_REJECTION.
  5. Degrades to pattern defaults when the gate is unavailable.
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
    print("1. Action required with gate thresholds satisfied:")
    ev_oom = make_event("fatal error: runtime: out of memory")
    dec_auth = SimpleNamespace(
        evaluated=True,
        fault_class="oom_memory_ceiling",
        action="restart_container",
        reason_code="thresholds_satisfied",
        raw_answers={"restart_would_help": {"noul": 0.90}, "restart_discards_state": {"noul": 0.05}},
    )
    anec = ae.synthesize_anecdote(ev_oom, decision=dec_auth)
    check("action_required is True when gate satisfies thresholds", anec["action_required"] is True)

    # -------------------------------------------------------------------------
    print("\n2. Action required when gate withholds action:")
    dec_withheld = SimpleNamespace(
        evaluated=True,
        fault_class="oom_memory_ceiling",
        action="none",
        reason_code="threshold_failed",
        raw_answers={"restart_would_help": {"noul": 0.40}, "restart_discards_state": {"noul": 0.75}},
    )
    anec_withheld = ae.synthesize_anecdote(ev_oom, decision=dec_withheld)
    check("action_required is False when gate withholds action due to high discards state",
          anec_withheld["action_required"] is False)

    # -------------------------------------------------------------------------
    print("\n3. Adjudication of OOM_MEMORY_CEILING inconsistency:")
    # Historically: pattern had action_required=False despite CRITICAL severity.
    # Without gate:
    anec_raw_oom = ae.synthesize_anecdote(ev_oom)
    check("legacy pattern default was False", anec_raw_oom["action_required"] is False)

    # With gate indicating safe restart:
    dec_safe_oom = SimpleNamespace(
        evaluated=True,
        fault_class="oom_memory_ceiling",
        action="restart_container",
        reason_code="thresholds_satisfied",
    )
    anec_safe_oom = ae.synthesize_anecdote(ev_oom, decision=dec_safe_oom)
    check("under gate with safe restart, action_required flips to True",
          anec_safe_oom["action_required"] is True)

    # -------------------------------------------------------------------------
    print("\n4. Adjudication of AUTH_SECURITY_REJECTION inconsistency:")
    # Historically: pattern had action_required=True despite MEDIUM severity.
    ev_auth = make_event("401 unauthorized: jwt expired")
    anec_raw_auth = ae.synthesize_anecdote(ev_auth)
    check("legacy pattern default was True", anec_raw_auth["action_required"] is True)

    # Under the gate: auth rejection fails threshold for restart remediation
    dec_auth_rejection = SimpleNamespace(
        evaluated=True,
        fault_class="auth_security_rejection",
        action="none",
        reason_code="severity_not_permitted",
    )
    anec_adjudicated_auth = ae.synthesize_anecdote(ev_auth, decision=dec_auth_rejection)
    check("under gate, action_required reflects that automated container restart is NOT required",
          anec_adjudicated_auth["action_required"] is False)

    # -------------------------------------------------------------------------
    print("\n5. Offline fallback degrades gracefully:")
    ev_db = make_event("pq: too many connections")
    anec_db = ae.synthesize_anecdote(ev_db, decision=None)
    check("without gate, degrades to DB pattern default (False)", anec_db["action_required"] is False)

    ev_panic = make_event("panic: nil dereference")
    anec_panic = ae.synthesize_anecdote(ev_panic, decision=None)
    check("without gate, degrades to panic pattern default (True)", anec_panic["action_required"] is True)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall action_required derivation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
