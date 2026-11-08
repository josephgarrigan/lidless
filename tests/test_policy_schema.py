#!/usr/bin/env python3
"""L-006 tests: the tenant policy schema extension for the decision gate.

Checks that every shipped tenant policy validates, that the extension does not
break existing keys, that the gate is off everywhere, and - the point of the
extension - that enabling the gate cannot authorize a remediation the tenant has
not already permitted in `auto_remediation`.

Usage:
    python3 tests/test_policy_schema.py
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windmill.flows import decision_gate as dg  # noqa: E402

SCHEMA_PATH = ROOT / "rules" / "schema" / "tenant-policy.schema.json"
TENANT_DIR = ROOT / "rules" / "tenants"
SCHEMA = json.loads(SCHEMA_PATH.read_text())
QUESTIONS = {"fault_class": None, "severity": None, "is_transient": None,
             "restart_would_help": None, "restart_discards_state": None}
GATE_SCHEMA = {"version": "v1", "questions": QUESTIONS}

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{'' if condition else ' - ' + detail}")
    if not condition:
        FAILURES.append(label)


def valid(policy: dict) -> bool:
    try:
        jsonschema.validate(policy, SCHEMA)
        return True
    except jsonschema.ValidationError:
        return False


def rejects(label: str, mutate) -> None:
    policy = copy.deepcopy(BASE_FINTECH)
    mutate(policy)
    check(label, not valid(policy), "schema accepted an invalid policy")


def gate_reason(policy: dict) -> str:
    """Run the real gate and report why it declined — no network, no SDK."""
    event = {"tenant_id": policy.get("tenant_id"), "org_id": "x", "severity": "critical",
             "is_error": True, "tenant": {"environment": "production"},
             "event_meta": {"is_error": True, "severity": "critical", "status_code": 500,
                            "stream": "stderr", "raw_message": "fatal error: out of memory",
                            "timestamp": "2026-09-01T23:46:00Z"},
             "host_meta": {"container": "payment_gateway", "hostname": "node-01"}}
    decision = dg.evaluate(event, policy, schema=GATE_SCHEMA, window=1)
    assert decision.action == "none", f"gate produced an action: {decision.action}"
    return decision.reason_code


BASE_FINTECH = yaml.safe_load((TENANT_DIR / "fintech.yaml").read_text())


def main() -> int:
    tenants = {p.stem: yaml.safe_load(p.read_text()) for p in sorted(TENANT_DIR.glob("*.yaml"))}

    print("shipped policies:")
    for name, policy in tenants.items():
        try:
            jsonschema.validate(policy, SCHEMA)
            check(f"{name}.yaml validates", True)
        except jsonschema.ValidationError as exc:
            check(f"{name}.yaml validates", False, f"{exc.message} at {list(exc.path)}")
    check("three tenants shipped", len(tenants) == 3, str(sorted(tenants)))

    print("\nexisting keys are untouched:")
    for name, policy in tenants.items():
        stripped = copy.deepcopy(policy)
        stripped.pop("decision_gate", None)
        check(f"{name}: valid without decision_gate (the block is optional)", valid(stripped))
        for key in ("tenant_id", "organization", "tier", "routing", "severity_overrides",
                    "auto_remediation", "silenced", "anecdote_verbosity"):
            check(f"{name}: {key} still present", key in stripped, "missing")

    print("\nthe gate is off everywhere:")
    for name, policy in tenants.items():
        gate = policy.get("decision_gate", {})
        check(f"{name}: enabled is false", gate.get("enabled") is False, str(gate.get("enabled")))
        check(f"{name}: mode is shadow", gate.get("mode") == "shadow", str(gate.get("mode")))
        check(f"{name}: model is pinned, not an alias",
              not str(gate.get("model", "")).endswith("-latest"), str(gate.get("model")))
        check(f"{name}: state_mode is declared",
              gate.get("state_mode") in ("sanitized_content", "structured_only"),
              str(gate.get("state_mode")))
        check(f"{name}: thresholds are per action",
              all(k in ("restart_container", "isolate_network", "scale_worker")
                  for k in (gate.get("thresholds") or {})), str(list(gate.get("thresholds") or {})))
    check("both egress modes are exercised by the fleet",
          {p["decision_gate"]["state_mode"] for p in tenants.values()}
          == {"sanitized_content", "structured_only"})

    print("\nvalidation rejects:")
    rejects("alias model", lambda p: p["decision_gate"].update(model="jev-latest"))
    rejects("unknown mode", lambda p: p["decision_gate"].update(mode="continuous"))
    rejects("unknown state_mode", lambda p: p["decision_gate"].update(state_mode="everything"))
    rejects("unversioned questions_version",
            lambda p: p["decision_gate"].update(questions_version="1"))
    rejects("decision_gate without enabled", lambda p: p["decision_gate"].pop("enabled"))
    rejects("unknown decision_gate property",
            lambda p: p["decision_gate"].update(auto_approve=True))
    rejects("empty allowed_severities", lambda p: p["decision_gate"].update(allowed_severities=[]))
    rejects("invented severity",
            lambda p: p["decision_gate"].update(allowed_severities=["fatal"]))
    rejects("threshold key that is not an action",
            lambda p: p["decision_gate"]["thresholds"].update(delete_everything={"min_severity_confidence": 0.5}))
    rejects("unknown threshold field",
            lambda p: p["decision_gate"]["thresholds"]["restart_container"].update(min_confidence=0.5))
    rejects("threshold above 1",
            lambda p: p["decision_gate"]["thresholds"]["restart_container"].update(min_restart_would_help=1.5))
    rejects("threshold below 0",
            lambda p: p["decision_gate"]["thresholds"]["restart_container"].update(max_restart_discards_state=-0.1))
    rejects("empty threshold block for an action",
            lambda p: p["decision_gate"]["thresholds"].update(restart_container={}))
    rejects("negative restarts per hour",
            lambda p: p["auto_remediation"].update(max_restarts_per_hour=-1))

    print("\n'enforce' with enabled false validates, and is inert:")
    policy = copy.deepcopy(BASE_FINTECH)
    policy["decision_gate"].update(mode="enforce")
    check("mode enforce with enabled false is accepted", valid(policy))
    check("and the gate still declines to evaluate",
          gate_reason(policy) == "gate_disabled", gate_reason(policy))

    print("\nthe gate cannot authorize what policy has not permitted:")
    for name, policy in tenants.items():
        check(f"{name}: with the gate off, reason is gate_disabled",
              gate_reason(policy) == "gate_disabled", gate_reason(policy))

        forced = copy.deepcopy(policy)
        forced["decision_gate"]["enabled"] = True          # simulate opting in
        reason = gate_reason(forced)
        if (forced.get("auto_remediation") or {}).get("enabled"):
            check(f"{name}: remediation enabled by policy, so the gate may decide",
                  reason != "remediation_not_enabled_by_policy", reason)
        else:
            check(f"{name}: gate cannot enable remediation policy has disabled",
                  reason == "remediation_not_enabled_by_policy", reason)

    print("\nno credentials in tenant policy:")
    secret_keys = re.compile(r"(password|secret|api_key|access_token|private_key|bearer)", re.I)
    for name, policy in tenants.items():
        text = (TENANT_DIR / f"{name}.yaml").read_text()
        hits = [k for k in _all_keys(policy) if secret_keys.search(k)]
        check(f"{name}: no credential-shaped keys", not hits, str(hits))
        check(f"{name}: no PEM material", "-----BEGIN" not in text)
        check(f"{name}: webhooks are placeholders",
              "hooks.slack.com/services/T00/B00" in text or "slack_webhook_url" not in text)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all policy-schema checks passed")
    return 0


def _all_keys(obj, prefix="") -> list:
    keys: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(k)
            keys.extend(_all_keys(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_all_keys(item, prefix))
    return keys


if __name__ == "__main__":
    raise SystemExit(main())
