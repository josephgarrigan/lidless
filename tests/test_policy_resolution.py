#!/usr/bin/env python3
"""L-001 follow-up: policy resolution tests for the issue #6 fix.

Asserts that every shipped tenant policy resolves by its declared `tenant_id`,
that the index refuses to be ambiguous, and that an unknown tenant is a loud
fallback rather than a silent one.

Usage:
    python3 tests/test_policy_resolution.py
Exits 0 if all assertions hold.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POLICY_DIR = ROOT / "rules" / "tenants"
os.environ["TENANT_POLICY_DIR"] = str(POLICY_DIR)

from windmill.flows import tenant_router as tr  # noqa: E402

# POLICY_DIR is read from the environment at import time; point it explicitly so the
# tests exercise the shipped policies regardless of how the process was started.
tr.POLICY_DIR = str(POLICY_DIR)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  {status} {label}{(' - ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    print("shipped policies resolve by declared tenant_id:")
    index = tr.build_policy_index(force=True)
    declared = sorted(index)
    check("index contains all shipped tenants", declared == ["alpha-corp", "fintech", "platform"],
          f"got {declared}")

    for tenant in declared:
        path, resolved = tr.resolve_policy_source(tenant)
        check(f"{tenant} resolves", resolved, f"fell back to {os.path.basename(path)}")
        check(f"{tenant} filename matches tenant_id",
              os.path.basename(path) == f"{tenant}.yaml", os.path.basename(path))

    print("\npolicy contents are the ones the fixtures expect:")
    fintech = tr.load_tenant_policy("fintech")
    check("fintech auto_remediation.enabled is True", fintech["auto_remediation"]["enabled"] is True)
    check("fintech routes to its own sinks", "slack_webhook_url" in fintech["routing"])
    alpha = tr.load_tenant_policy("alpha-corp")
    check("alpha-corp is a distinct policy from platform",
          alpha["organization"] == "alpha-enterprises", alpha["organization"])
    check("alpha-corp routes to its own Matrix room",
          alpha["routing"]["matrix_room_id"] != tr.load_tenant_policy("platform")["routing"]["matrix_room_id"])

    print("\nunknown tenant is a loud fallback, and strict mode refuses it:")
    path, resolved = tr.resolve_policy_source("does-not-exist")
    check("unknown tenant reports resolved=False", resolved is False)
    check("unknown tenant falls back to the platform policy",
          os.path.basename(path) == "platform.yaml", os.path.basename(path))
    raised = False
    try:
        tr.load_tenant_policy("does-not-exist", strict=True)
    except tr.TenantPolicyError:
        raised = True
    check("strict=True raises TenantPolicyError", raised)

    print("\nambiguous index is a hard error:")
    with tempfile.TemporaryDirectory() as tmp:
        # Two files declaring the same tenant - the shape a runtime backflow write
        # would produce if a hand-authored file already used that tenant_id.
        body = 'tenant_id: "fintech"\norganization: "shadow"\nrouting:\n  default_sink: "slack"\n'
        (Path(tmp) / "fintech.yaml").write_text(body)
        (Path(tmp) / "duplicate.yaml").write_text(body)
        original = tr.POLICY_DIR
        tr.POLICY_DIR = tmp
        try:
            raised = False
            try:
                tr.build_policy_index(force=True)
            except tr.TenantPolicyError:
                raised = True
            check("duplicate tenant_id raises", raised)
        finally:
            tr.POLICY_DIR = original
            tr.build_policy_index(force=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all policy-resolution checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
