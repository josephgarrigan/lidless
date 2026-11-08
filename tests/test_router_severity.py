"""Router severity and incident-worthiness: OD-3 (lidless#3) and the is_error stopgap (lidless#8).

Runs the real router against real policy files in a temp dir, with the dispatch functions
monkeypatched to recorders so nothing leaves the machine and `dispatched_to` still reports what
*would* have been sent.

The gate policies are built on `test_decision_gate.policy()["decision_gate"]` on purpose: a thin
gate policy declines before it reaches the severity question (`state_mode_not_declared`,
`remediation_not_enabled_by_policy`), and a test that uses one measures its own fixtures rather
than the router.

    python3 tests/test_router_severity.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from windmill.flows import tenant_router as tr  # noqa: E402
import test_decision_gate as tdg  # noqa: E402  (stub client + answer shapes, so the answers are real)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def event(*, is_error=True, severity="error", tenant="alpha") -> dict:
    return {
        "tenant_id": tenant, "org_id": "alpha-eng", "severity": severity,
        "is_error": is_error,
        "tenant": {"id": tenant, "environment": "production"},
        "event_meta": {"is_error": is_error, "severity": severity,
                       "status_code": 500 if is_error else 200,
                       "stream": "stderr" if is_error else "stdout",
                       "raw_message": "fatal error: runtime: out of memory" if is_error
                                      else "cache warm complete: 18432 keys",
                       "timestamp": "2026-09-23T10:00:00Z"},
        "host_meta": {"container": "batch_exporter", "hostname": "debian-node-01"},
    }


def policies(tmp: Path, *, gate: dict, remediation: bool = True) -> Path:
    """One tenant that opts into things, plus the platform fallback the ledger looks up."""
    d = tmp / "tenants"
    d.mkdir(parents=True, exist_ok=True)
    (d / "platform.yaml").write_text(yaml.safe_dump({
        "tenant_id": "platform", "silenced": False,
        "routing": {"default_sink": "matrix", "matrix_room_id": "!platform:test"},
        "decision_gate": {"enabled": False, "mode": "shadow"},
    }))
    (d / "alpha.yaml").write_text(yaml.safe_dump({
        "tenant_id": "alpha", "silenced": False,
        "routing": {"default_sink": "matrix", "matrix_room_id": "!alpha:test",
                    "slack_webhook_url": "https://slack.invalid/alpha"},
        # Routing differs by severity, so *which* severity routed is observable in dispatched_to.
        "severity_overrides": {"critical": ["matrix", "slack"], "error": ["matrix"]},
        # L-009: a missing cap fails closed in enforce mode. Declare one so this
        # fixture still means "reported critical + gate agreement restarts",
        # not "budget was assumed unlimited".
        "auto_remediation": {"enabled": remediation, "target_action": "restart_container",
                             "max_restarts_per_hour": 3},
        "decision_gate": gate,
    }))
    return d


def use(directory: Path) -> None:
    tr.POLICY_DIR = str(directory)
    tr._policy_index = None
    tr.build_policy_index(force=True)


def route(ev, *, client=None):
    return tr.main(ev, gate_client=client, gate_schema=tdg.SCHEMA)


def main() -> int:
    tr.dispatch_matrix_anecdote = lambda *a, **k: None    # no network; dispatched_to still reports
    tr.dispatch_slack_anecdote = lambda *a, **k: None

    base = tdg.policy()["decision_gate"]

    def gate(**over) -> dict:
        g = dict(base)
        g.update(over)
        return g

    observe = gate(enabled=True, mode="shadow", shadow=True, allowed_severities=["critical", "error"])
    authority = gate(enabled=True, mode="enforce", shadow=False, allowed_severities=["critical", "error"])

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        benign = event(is_error=False, severity="MEDIUM")
        real = event(is_error=True, severity="error")

        print("\n#8 stopgap (option A): benign events stop dispatching where a tenant opts in")
        use(policies(tmp / "a", gate=gate(enabled=False, mode="shadow", is_error_stopgap=True)))
        r = route(benign)
        check("a benign event is not dispatched", r["status"] == "not_an_incident", str(r))
        check("and nothing was sent", not r.get("dispatched_to"), str(r.get("dispatched_to")))
        check("the suppression names its reason", r.get("suppressed_by") == "is_error_stopgap", str(r))
        check("the reported severity is still recorded", r.get("reported_severity") == "MEDIUM", str(r))
        r2 = route(real)
        check("a real error still dispatches", r2["status"] == "routed_with_anecdote"
              and r2["dispatched_to"] == ["matrix_anecdote_sent"], str(r2))

        print("\n#8 without the stopgap: the defect as filed")
        use(policies(tmp / "b", gate=gate(enabled=False, mode="shadow")))
        r = route(benign)
        check("benign events dispatch when nobody opted in — the defect, not intended behaviour",
              r["status"] == "routed_with_anecdote" and bool(r["dispatched_to"]), str(r))

        print("\nOD-3: in a shadow period the gate cannot change the dispatch set (L-007)")
        use(policies(tmp / "c", gate=observe))
        r = route(real, client=tdg.StubClient(tdg.answers(severity="critical", sev_conf=0.95)))
        check("routing follows the REPORTED severity", r["routing_severity"] == "error", str(r))
        check("so dispatch is unchanged: error routes to matrix only",
              r["dispatched_to"] == ["matrix_anecdote_sent"], str(r["dispatched_to"]))
        check("while the gate's own answer is recorded",
              r["shadow_decision"]["gate_severity"] == "critical", str(r.get("shadow_decision")))
        check("and the envelope says which severity routed",
              r["shadow_decision"]["routing_followed"] == "reported", str(r.get("shadow_decision")))

        print("\nOD-3: with authority granted, the gate's severity is the routing predicate")
        use(policies(tmp / "d", gate=authority))
        r = route(real, client=tdg.StubClient(tdg.answers(severity="critical")))
        check("routing follows the GATE's severity", r["routing_severity"] == "critical", str(r))
        check("which changes the dispatch set (critical reaches slack)",
              r["dispatched_to"] == ["matrix_anecdote_sent", "slack_anecdote_sent"], str(r["dispatched_to"]))
        check("the reported severity is still on the response", r["reported_severity"] == "error", str(r))
        check("and the envelope records the divergence",
              r["shadow_decision"]["routing_followed"] == "gate", str(r.get("shadow_decision")))

        print("\nOD-3 + #8 option B: not_an_incident suppresses dispatch where the gate has authority")
        use(policies(tmp / "e", gate=authority))
        r = route(benign, client=tdg.StubClient(tdg.answers(severity="not_an_incident", sev_conf=0.9)))
        check("nothing is dispatched", r["status"] == "not_an_incident", str(r))
        check("the gate is named as what suppressed it",
              r.get("suppressed_by") == "decision_gate", str(r))
        check("and the gate's answer is preserved even though the verdict declined",
              r.get("gate_severity") == "not_an_incident", str(r))

        print("\nthe gate may withhold an action, never grant one")
        use(policies(tmp / "f", gate=authority, remediation=True))
        r = route(event(is_error=True, severity="error"), client=tdg.StubClient(tdg.answers(severity="critical")))
        check("a gate that says critical does not thereby authorise remediation",
              not any("triggered_remediation" in d for d in r["dispatched_to"]), str(r["dispatched_to"]))
        # Empty dir is a measured 0 from the production loader, not an injected count.
        saved_budget = os.environ.pop("GATE_BUDGET_DIR", None)
        empty_budget = tmp / "gate-budget"
        empty_budget.mkdir()
        os.environ["GATE_BUDGET_DIR"] = str(empty_budget)
        try:
            r = route(event(is_error=True, severity="critical"), client=tdg.StubClient(tdg.answers(severity="critical")))
        finally:
            if saved_budget is None:
                os.environ.pop("GATE_BUDGET_DIR", None)
            else:
                os.environ["GATE_BUDGET_DIR"] = saved_budget
        check("while a reported critical still does",
              any("triggered_remediation" in d for d in r["dispatched_to"]), str(r["dispatched_to"]))

        print("\nOD-3's preservation requirement: reported_severity rides in the decision record")
        use(policies(tmp / "g", gate=observe))
        r = route(real, client=tdg.StubClient(tdg.answers(severity="critical")))
        check("a decision was recorded end to end",
              r["shadow_decision"].get("decision_id") is not None, str(r.get("shadow_decision")))
        d = tdg.dg.Decision()
        d.reported_severity = "error"
        check("Decision carries the field", d.reported_severity == "error")
        check("it is in envelope()", "reported_severity" in d.envelope())
        check("and in to_dict(), so the ledger keeps both severities",
              "reported_severity" in d.to_dict())

    print(f"\n{'all router severity checks passed' if not FAILURES else str(len(FAILURES)) + ' FAILED'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())