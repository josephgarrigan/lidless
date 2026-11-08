#!/usr/bin/env python3
"""task 186: lidless is the primary input. The gate is the authority.

main() keeps today's path: enabled:false still restarts on a reported
critical. primary_input does not. A tenant that has not granted the gate
authority is withheld — no dispatch, no restart. When the gate does have
authority, the receipt carries provenance (decision id, pinned model,
question-set version) so a caller can say why without opening the ledger.

Shipped rules/tenants/*.yaml are not the place this adoption is stored.
fintech stays shadow. The platform tenant is not flipped here.

    python3 tests/test_primary_input.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from windmill.flows import tenant_router as tr  # noqa: E402
import test_decision_gate as tdg  # noqa: E402
import test_enforce_semantics as enf  # noqa: E402

FAILURES: list[str] = []
SHIPPED = ROOT / "rules" / "tenants"


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def provenance_of(response: dict) -> dict:
    return response.get("provenance") or {}


def main() -> int:
    restarts: list = []

    def counting_restart(container, action):
        restarts.append((container, action))
        return "triggered_remediation_" + str(action) + "_on_" + str(container)

    tr.dispatch_matrix_anecdote = lambda *a, **k: None
    tr.dispatch_slack_anecdote = lambda *a, **k: None
    tr.perform_restart = counting_restart

    print("\nshipped policies are not the adoption")
    for name in ("platform.yaml", "fintech.yaml"):
        data = yaml.safe_load((SHIPPED / name).read_text()) or {}
        gate = data.get("decision_gate") or {}
        check(name + " stays disabled", gate.get("enabled") is False, str(gate.get("enabled")))
        check(name + " stays shadow", gate.get("mode") == "shadow", str(gate.get("mode")))

    print("\nno authority: withhold, do not take today's restart path")
    with __import__("tempfile").TemporaryDirectory() as t:
        tmp = Path(t)
        # fintech-shaped: remediation already permitted, gate not the authority.
        # main() would restart this. primary_input must not.
        enf.use(enf.write_policies(tmp, enabled=False, mode="shadow", remediation=True))
        restarts.clear()
        response = tr.primary_input(
            enf.event(severity="critical"),
            gate_client=tdg.StubClient(tdg.answers()),
            gate_schema=tdg.SCHEMA,
            restarts_this_hour=0,
        )
        check("status withheld", response.get("status") == "withheld", str(response.get("status")))
        check("reason names the missing authority",
              response.get("reason") == "gate_not_authority", str(response.get("reason")))
        check("nothing dispatched", response.get("dispatched_to") == [], str(response.get("dispatched_to")))
        check("no restart", not restarts, str(restarts))
        check("no provenance invented", provenance_of(response) == {}, str(response.get("provenance")))

    print("\nenforce authority: receipt carries gate provenance")
    with __import__("tempfile").TemporaryDirectory() as t:
        tmp = Path(t)
        enf.use(enf.write_policies(
            tmp, enabled=True, mode="enforce", shadow=False, remediation=True,
        ))
        restarts.clear()
        client = tdg.StubClient(tdg.answers(sev_conf=0.4, help_=0.2, discards=0.9))
        response = tr.primary_input(
            enf.event(severity="critical", tenant="fintech"),
            gate_client=client,
            gate_schema=tdg.SCHEMA,
            restarts_this_hour=0,
        )
        prov = provenance_of(response)
        check("low confidence does not restart", not restarts, str(restarts))
        check("decision_id present", bool(prov.get("decision_id")), str(prov))
        check("model pinned", prov.get("model_version") == "jev-1.13.0", str(prov.get("model_version")))
        check("questions version recorded", prov.get("questions_version") == "v1",
              str(prov.get("questions_version")))
        check("reason_code recorded", bool(prov.get("reason_code")), str(prov.get("reason_code")))
        check("routing followed the gate", prov.get("routing_followed") == "gate",
              str(prov.get("routing_followed")))

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("all primary-input checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
